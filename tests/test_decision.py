import pytest
from pydantic import ValidationError

from app.decision import InvoiceDecision

REFS = ["[invoice:INV-42]", "[vendor:V-7]", "[policy:P-3]"]
RATIONALE = (
    "The invoice matches the vendor record and policy requirements. "
    "[invoice:INV-42] and [vendor:V-7] support this decision."
)


def test_valid_decision():
    decision = InvoiceDecision(
        action="request_approval",
        evidence_refs=REFS,
        rationale=RATIONALE,
    )
    assert decision.action == "request_approval"


def test_rejects_wrong_number_of_evidence_refs():
    with pytest.raises(ValidationError):
        InvoiceDecision(
            action="hold_invoice",
            evidence_refs=REFS[:2],
            rationale=RATIONALE,
        )


def test_rejects_duplicate_evidence_refs():
    with pytest.raises(ValidationError):
        InvoiceDecision(
            action="hold_invoice",
            evidence_refs=[REFS[0], REFS[0], REFS[2]],
            rationale=RATIONALE,
        )


def test_rejects_rationale_without_two_citations():
    with pytest.raises(ValidationError):
        InvoiceDecision(
            action="hold_invoice",
            evidence_refs=REFS,
            rationale=(
                "This rationale is sufficiently long but cites only "
                "[invoice:INV-42] as evidence."
            ),
        )


def test_rejects_unknown_action():
    with pytest.raises(ValidationError):
        InvoiceDecision(
            action="pay_everything",
            evidence_refs=REFS,
            rationale=RATIONALE,
        )
