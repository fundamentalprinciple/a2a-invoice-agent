import json
import uuid
from typing import Any

from openai import APIError
from pydantic import ValidationError

from app.config import get_settings
from app.decision import BatchDecisions
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
- rationale: 60-1500 characters citing at least two evidence references

Use the paragraph that determines the action. Ignore cover sheets,
irrelevant text, historical examples, and training decoys. Never invent
facts or references.

Choose settle_invoice only when the invoice is valid, reconciled, and
within autonomous authority. Choose request_approval when commercially
valid but outside delegated authority. Choose hold_invoice when payment
must pause for verification. Choose reject_duplicate when the same
commercial invoice was already paid. Choose open_exception when material
records conflict.

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



async def decide_batch(batch: dict[str, Any]) -> dict[str, Any]:
    """Request and validate one AI decision for every package in a batch."""
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

    settings = get_settings()

    try:
        response = await get_llm_client().chat.completions.parse(
            model=settings.aipipe_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _canonical_json(batch)},
            ],
            response_format=BatchDecisions,
        )
    except APIError as exc:
        raise DecisionServiceError("Invoice decision model request failed") from exc
    except ValidationError as exc:
        raise DecisionServiceError("Model returned an invalid batch response") from exc

    if not response.choices:
        raise DecisionServiceError("Model returned no choices")

    message = response.choices[0].message

    if message.refusal:
        raise DecisionServiceError("Model refused to evaluate the invoice batch")

    if message.parsed is None:
        raise DecisionServiceError("Model returned no valid structured decisions")

    try:
        decisions = BatchDecisions.model_validate(message.parsed)
    except (ValidationError, TypeError) as exc:
        raise DecisionServiceError("Model returned an invalid batch response") from exc

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
        available_strings = _collect_strings(package)

        if any(
            not any(ref in text for text in available_strings)
            for ref in decision.evidence_refs
        ):
            raise DecisionServiceError(
                f"Decision for {decision.package_id} contains unsupported evidence"
            )

        proposals.append(
            {
                "packageId": decision.package_id,
                "actionId": uuid.uuid4().hex,
                "action": decision.action,
                "facts": decision.facts.model_dump(by_alias=True),
                "evidenceRefs": decision.evidence_refs,
                "rationale": decision.rationale,
            }
        )

    return {
        "batchId": batch_id,
        "proposals": proposals,
    }

