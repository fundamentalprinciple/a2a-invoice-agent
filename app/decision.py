import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

InvoiceAction = Literal[
    "settle_invoice",
    "request_approval",
    "hold_invoice",
    "reject_duplicate",
    "open_exception",
]


def normalize_evidence_refs(refs: list[str]) -> list[str]:
    """Return the bracketed reference token from each supplied reference.

    Models sometimes copy an entire sentence instead of the bracketed token
    alone (for example ``"[invoice:1] Invoice INV-1 ..."``). The API schema
    asks for bracketed references, so this function extracts the first
    bracket-delimited token from each entry.
    """
    normalized: list[str] = []
    for ref in refs:
        if re.fullmatch(r"\[[^\[\]]+\]", ref):
            token = ref
        else:
            match = re.search(r"\[[^\[\]]+\]", ref)
            if match is None:
                raise ValueError("Each evidence reference must be bracketed")
            token = match.group(0)
        normalized.append(token)

    if len(set(normalized)) != 3:
        raise ValueError("Evidence references must be distinct")

    return normalized


def ensure_rationale_quality(
    rationale: str,
    evidence_refs: list[str],
    action: str,
) -> str:
    """Make sure a rationale names the action and cites two references."""
    text = (rationale or "").strip()

    if action not in text:
        text = f"{action}: {text}" if text else f"{action}."

    present = [ref for ref in evidence_refs if ref in text]
    suffix = ""
    if len(present) < 2:
        missing = [ref for ref in evidence_refs if ref not in present]
        add = missing[: 2 - len(present)]
        suffix = " Evidence: " + ", ".join(add) + "."
        text = text.rstrip() + suffix

    if len(text) > 1500:
        if suffix and text.endswith(suffix):
            text = text[: 1500 - len(suffix)] + suffix
        else:
            text = text[:1500]

    return text


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        populate_by_name=True,
    )


class InvoiceFacts(StrictModel):
    vendor_name: str = Field(alias="vendorName", min_length=1)
    invoice_number: str = Field(alias="invoiceNumber", min_length=1)
    amount_minor: int = Field(alias="amountMinor", ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class InvoiceDecision(StrictModel):
    package_id: str = Field(alias="packageId", min_length=1)
    action: InvoiceAction
    facts: InvoiceFacts
    evidence_refs: list[str] = Field(
        alias="evidenceRefs",
        min_length=3,
        max_length=3,
    )
    rationale: str = Field(min_length=60, max_length=1500)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, refs: list[str]) -> list[str]:
        return normalize_evidence_refs(refs)

    @model_validator(mode="after")
    def rationale_cites_evidence(self) -> "InvoiceDecision":
        if self.action not in self.rationale:
            raise ValueError("Rationale must name the selected action")

        cited = {ref for ref in self.evidence_refs if ref in self.rationale}
        if len(cited) < 2:
            raise ValueError(
                "Rationale must cite at least two evidence references"
            )

        return self


class BatchDecisions(StrictModel):
    batch_id: str = Field(alias="batchId", min_length=1)
    decisions: list[InvoiceDecision] = Field(min_length=1)

    @model_validator(mode="after")
    def package_ids_are_unique(self) -> "BatchDecisions":
        ids = [decision.package_id for decision in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("Package IDs must be unique within a batch")

        return self

