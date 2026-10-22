import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

InvoiceAction = Literal[
    "settle_invoice",
    "request_approval",
    "hold_invoice",
    "reject_duplicate",
    "open_exception",
]


class InvoiceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: InvoiceAction
    evidence_refs: list[str] = Field(min_length=3, max_length=3)
    rationale: str = Field(min_length=60, max_length=1500)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, refs: list[str]) -> list[str]:
        if len(set(refs)) != 3:
            raise ValueError("Evidence references must be distinct")

        if any(not re.fullmatch(r"\[[^\[\]]+\]", ref) for ref in refs):
            raise ValueError("Each evidence reference must be bracketed")

        return refs

    @model_validator(mode="after")
    def rationale_cites_evidence(self) -> "InvoiceDecision":
        cited_refs = {ref for ref in self.evidence_refs if ref in self.rationale}
        if len(cited_refs) < 2:
            raise ValueError("Rationale must cite at least two evidence references")

        return self
