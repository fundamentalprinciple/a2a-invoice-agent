import json
import uuid
from typing import Any

from openai import APIError
from pydantic import ValidationError

from app.config import get_settings
from app.decision import (
    BatchDecisions,
    InvoiceDecision,
    ensure_rationale_quality,
    normalize_evidence_refs,
)
from app.decision_cache import (
    get_cached_decisions,
    package_content_hash,
    store_cached_decision,
)
from app.llm_client import get_llm_client

SYSTEM_PROMPT = """
You are an invoice-review agent. Evaluate every package in the supplied
batch using only its provided facts, documents, and policy evidence.

For each package, return:
- packageId: its exact supplied package ID
- action: exactly one of settle_invoice, request_approval,
  hold_invoice, reject_duplicate, open_exception
- facts: vendorName, invoiceNumber, amountMinor, currency
- evidenceRefs: exactly three distinct, bracketed references copied
  verbatim from the supplied documents
- rationale: 60-1500 characters that names the chosen action using its
  exact action string (for example request_approval) and cites at least
  two evidence references

Action definitions:
- settle_invoice: the invoice is valid, fully reconciled, and its amount
  is at or below the policy's autonomous approval limit.
- request_approval: the invoice is commercially valid but its amount or
  policy conditions put it outside delegated authority.
- hold_invoice: payment pauses until a stated verification (for example
  missing proof of delivery or an open reconciliation) completes.
- reject_duplicate: the same commercial invoice (same vendor, same
  invoiceNumber, same amount) was already paid. A different invoice is
  never a duplicate.
- open_exception: material records conflict and need an exception workflow.

For each package, identify the single paragraph that determines the
action. Take evidenceRefs from that paragraph only. Do not use the cover
sheet, archive examples, training decoys, or references whose paragraph
describes a different action. If the deciding paragraph offers more than
three bracketed references, choose the three most decisive ones.

Never invent facts or references. Copy every reference and fact exactly.

Return exactly one decision for every supplied package. Do not include
actionId; the server generates it.
"""


class DecisionServiceError(Exception):
    """Raised when the model cannot produce valid batch decisions."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _collect_strings(value: Any) -> set[str]:
    """Collect all strings recursively from the supplied package."""
    if isinstance(value, str):
        return {value}

    if isinstance(value, dict):
        result: set[str] = set()
        for item in value.values():
            result.update(_collect_strings(item))
        return result

    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_collect_strings(item))
        return result

    return set()


def _validate_package_ids(
    batch: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    batch_id = batch.get("batchId")
    packages = batch.get("packages")

    if not isinstance(batch_id, str) or not batch_id:
        raise DecisionServiceError("Batch must contain a non-empty batchId")

    if not isinstance(packages, list) or not packages:
        raise DecisionServiceError("Batch must contain a non-empty packages list")

    package_ids = [
        package.get("packageId")
        for package in packages
        if isinstance(package, dict)
    ]
    if len(package_ids) != len(packages):
        raise DecisionServiceError("Every package must be an object")

    if any(not isinstance(package_id, str) or not package_id for package_id in package_ids):
        raise DecisionServiceError("Every package must contain a packageId")

    if len(set(package_ids)) != len(package_ids):
        raise DecisionServiceError("Package IDs must be unique within a batch")

    return packages, package_ids


def _validate_evidence(decision: InvoiceDecision, package: dict[str, Any]) -> None:
    available_strings = _collect_strings(package)
    if any(
        not any(ref in text for text in available_strings)
        for ref in decision.evidence_refs
    ):
        raise DecisionServiceError(
            f"Decision for {decision.package_id} contains unsupported evidence"
        )


def _proposal_from_decision(
    package: dict[str, Any],
    decision: InvoiceDecision,
) -> dict[str, Any]:
    return {
        "packageId": decision.package_id,
        "actionId": uuid.uuid4().hex,
        "action": decision.action,
        "facts": decision.facts.model_dump(by_alias=True),
        "evidenceRefs": decision.evidence_refs,
        "rationale": decision.rationale,
    }


def _parse_batch_from_content(content: Any, batch_id: str) -> BatchDecisions:
    if not isinstance(content, str) or not content.strip():
        raise DecisionServiceError("Model returned no structured decisions")

    try:
        raw = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DecisionServiceError("Model returned invalid JSON") from exc

    if not isinstance(raw, dict):
        raise DecisionServiceError("Model returned an invalid batch response")

    if raw.get("batchId") != batch_id:
        raise DecisionServiceError("Model returned a mismatched batchId")

    raw_decisions = raw.get("decisions")
    if not isinstance(raw_decisions, list) or not raw_decisions:
        raise DecisionServiceError("Model returned no structured decisions")

    repaired: list[dict[str, Any]] = []
    for item in raw_decisions:
        if not isinstance(item, dict):
            raise DecisionServiceError("Model returned an invalid batch response")

        package_id = item.get("packageId")
        action = item.get("action")
        facts = item.get("facts")
        evidence_refs = item.get("evidenceRefs")
        rationale = item.get("rationale")

        if not isinstance(package_id, str) or not package_id:
            raise DecisionServiceError("Model returned an invalid batch response")
        if not isinstance(action, str) or not action:
            raise DecisionServiceError("Model returned an invalid batch response")
        if not isinstance(facts, dict):
            raise DecisionServiceError("Model returned an invalid batch response")
        if not isinstance(evidence_refs, list) or len(evidence_refs) != 3:
            raise DecisionServiceError("Model returned invalid evidence references")
        if not isinstance(rationale, str):
            rationale = ""

        try:
            refs = normalize_evidence_refs(evidence_refs)
        except ValueError as exc:
            raise DecisionServiceError(
                "Model returned invalid evidence references"
            ) from exc

        repaired.append(
            {
                "packageId": package_id,
                "action": action,
                "facts": facts,
                "evidenceRefs": refs,
                "rationale": ensure_rationale_quality(rationale, refs, action),
            }
        )

    try:
        return BatchDecisions.model_validate(
            {"batchId": batch_id, "decisions": repaired}
        )
    except ValidationError as exc:
        raise DecisionServiceError("Model returned an invalid batch response") from exc


async def _decide_uncached(
    batch: dict[str, Any],
    packages: list[dict[str, Any]],
    package_ids: list[str],
    *,
    cache_decisions: bool,
) -> list[dict[str, Any]]:
    batch_id = batch["batchId"]
    policy_revision = batch.get("policyRevision")
    settings = get_settings()

    try:
        response = await get_llm_client().chat.completions.create(
            model=settings.aipipe_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _canonical_json(batch)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "BatchDecisions",
                    "schema": BatchDecisions.model_json_schema(),
                    "strict": True,
                },
            },
        )
    except APIError as exc:
        raise DecisionServiceError("Invoice decision model request failed") from exc

    if not response.choices:
        raise DecisionServiceError("Model returned no choices")

    message = response.choices[0].message

    if message.refusal:
        raise DecisionServiceError("Model refused to evaluate the invoice batch")

    decisions = _parse_batch_from_content(message.content, batch_id)

    if decisions.batch_id != batch_id:
        raise DecisionServiceError("Model returned a mismatched batchId")

    expected_ids = set(package_ids)
    actual_ids = {decision.package_id for decision in decisions.decisions}

    if actual_ids != expected_ids:
        raise DecisionServiceError(
            "Model must return exactly one decision per supplied package"
        )

    packages_by_id = {
        package["packageId"]: package
        for package in packages
    }

    proposals = []
    for decision in decisions.decisions:
        package = packages_by_id[decision.package_id]
        _validate_evidence(decision, package)
        proposals.append(_proposal_from_decision(package, decision))

        if cache_decisions:
            store_cached_decision(
                package,
                {
                    "action": decision.action,
                    "facts": decision.facts.model_dump(by_alias=True),
                    "evidenceRefs": decision.evidence_refs,
                    "rationale": decision.rationale,
                },
                policy_revision,
            )

    return proposals


async def decide_batch(
    batch: dict[str, Any],
    use_cache: bool = False,
) -> dict[str, Any]:
    """Request and validate one AI decision for every package in a batch.

    When ``use_cache`` is enabled, packages whose decision-relevant content
    has already been evaluated reuse the stored decision, and the model is
    only asked to evaluate the previously unseen packages.
    """
    batch_id = batch.get("batchId")
    packages, package_ids = _validate_package_ids(batch)

    if not use_cache:
        proposals = await _decide_uncached(
            batch,
            packages,
            package_ids,
            cache_decisions=False,
        )
        return {"batchId": batch_id, "proposals": proposals}

    policy_revision = batch.get("policyRevision")
    package_hashes = [
        package_content_hash(package, policy_revision)
        for package in packages
    ]
    cached = get_cached_decisions(set(package_hashes))

    uncached_packages = [
        package
        for package, package_hash in zip(packages, package_hashes)
        if package_hash not in cached
    ]

    proposals_by_id: dict[str, dict[str, Any]] = {}
    if uncached_packages:
        sub_batch = {
            key: value
            for key, value in batch.items()
            if key != "packages"
        }
        sub_batch["packages"] = uncached_packages
        uncached_ids = [package["packageId"] for package in uncached_packages]
        uncached_proposals = await _decide_uncached(
            sub_batch,
            uncached_packages,
            uncached_ids,
            cache_decisions=True,
        )
        proposals_by_id = {
            proposal["packageId"]: proposal
            for proposal in uncached_proposals
        }

    proposals = []
    for package, package_hash in zip(packages, package_hashes):
        package_id = package["packageId"]
        if package_hash in cached:
            decision_data = cached[package_hash]
            try:
                decision = InvoiceDecision.model_validate(
                    {"packageId": package_id, **decision_data}
                )
            except ValidationError as exc:
                raise DecisionServiceError(
                    "Cached package decision is invalid"
                ) from exc

            _validate_evidence(decision, package)
            proposals.append(_proposal_from_decision(package, decision))
        else:
            proposal = proposals_by_id.get(package_id)
            if proposal is None:
                raise DecisionServiceError(
                    "Model did not return a decision for every package"
                )
            proposals.append(proposal)

    return {"batchId": batch_id, "proposals": proposals}
