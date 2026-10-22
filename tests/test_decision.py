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
        packageId="package-001",
        action="request_approval",
        facts={
            "vendorName": "Acme Supplies",
            "invoiceNumber": "INV-2026-001",
            "amountMinor": 125000,
            "currency": "INR",
        },
        evidenceRefs=REFS,
        rationale=RATIONALE,
    )

    assert decision.action == "request_approval"
    assert decision.package_id == "package-001"
    assert decision.facts.amount_minor == 125000
    assert decision.evidence_refs == REFS


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
