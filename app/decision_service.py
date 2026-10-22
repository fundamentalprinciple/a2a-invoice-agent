import json
from typing import Any

from openai import APIError

from app.config import get_settings
from app.decision import InvoiceDecision
from app.llm_client import get_llm_client

SYSTEM_PROMPT = """
You are an invoice-review agent.

Evaluate the supplied invoice package using only the facts and evidence
provided. Do not invent facts, evidence references, policies, or approvals.

Choose exactly one action:
- settle_invoice
- request_approval
- hold_invoice
- reject_duplicate
- open_exception

Choose the action best supported by the package. Do not settle an invoice
when the evidence indicates a duplicate, missing information, a policy
problem, or a need for approval or escalation.

Return exactly three distinct, bracketed evidence references that support
the decision. They must come from the supplied evidence, specifically
the paragraph that determines the action.

Write a rationale of 60–1500 characters that cites at least two of those
references. Be specific and factual.
"""


class DecisionServiceError(Exception):
    """Raised when the model cannot produce a valid invoice decision."""


async def decide_invoice(package: dict[str, Any]) -> InvoiceDecision:
    settings = get_settings()

    try:
        response = await get_llm_client().chat.completions.parse(
            model=settings.aipipe_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        package,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                },
            ],
            response_format=InvoiceDecision,
        )
    except APIError as exc:
        raise DecisionServiceError("Invoice decision model request failed") from exc

    if not response.choices:
        raise DecisionServiceError("Model returned no choices")

    message = response.choices[0].message

    if message.refusal:
        raise DecisionServiceError("Model refused to evaluate the invoice")

    if message.parsed is None:
        raise DecisionServiceError("Model returned no valid structured decision")

    return message.parsed
