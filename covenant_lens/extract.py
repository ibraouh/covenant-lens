"""Read a credit agreement with Claude and pull out its financial maintenance covenants.

The model only reads and quotes. Every quote it returns is checked against the agreement text
in code (verify_quotes), and all ratio math happens later in covenants.py.
"""

from __future__ import annotations

import re
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

MODEL = "claude-opus-5-5"

Metric = Literal[
    "total_leverage",          # total debt / EBITDA
    "net_leverage",            # (debt - cash) / EBITDA
    "secured_leverage",        # secured debt / EBITDA
    "secured_net_leverage",
    "interest_coverage",       # EBITDA / interest expense
    "fixed_charge_coverage",
    "minimum_liquidity",
    "other",
]
AddBack = Literal["stock_comp", "impairments", "restructuring", "other"]


class Threshold(BaseModel):
    value: float = Field(description="The ratio limit, e.g. 4.5 for 4.50:1.00, or a dollar amount for liquidity")
    applies_from: str | None = Field(
        description="First fiscal quarter-end this level applies to (YYYY-MM-DD) if the agreement steps the limit "
        "up or down over time; null if it applies for the whole term"
    )


class Covenant(BaseModel):
    name: str = Field(description="The defined term, exactly as written, e.g. 'Consolidated Total Net Leverage Ratio'")
    metric: Metric
    direction: Literal["max", "min"] = Field(description="max: must stay at or below; min: must stay at or above")
    thresholds: list[Threshold]
    test_frequency: Literal["quarterly", "annually", "springing", "other"]
    springing_condition: str | None = Field(description="When the covenant is only tested under a condition, that condition")
    section: str = Field(description="Section number where the covenant appears, e.g. 'Section 7.11(a)'")
    quote: str = Field(description="The covenant sentence, copied word for word from the agreement")
    ebitda_addbacks: list[AddBack] = Field(
        description="Which of these the agreement's EBITDA definition adds back: non-cash stock compensation, "
        "impairment or other non-cash write-downs, restructuring charges. Use 'other' for anything else notable."
    )
    cash_netting_cap: float | None = Field(
        description="For net ratios: a dollar cap on the cash that may be netted against debt, or null if none"
    )
    cash_netting_cap_pct_ebitda: float | None = Field(
        description="For net ratios: a cap on netted cash expressed as a share of EBITDA (0.5 for 50%), or null if none"
    )


class CovenantExtraction(BaseModel):
    borrower: str
    agreement_date: str | None = Field(
        default=None, description="The date the original agreement is dated as of (YYYY-MM-DD)"
    )
    covenants: list[Covenant]
    notes: str = Field(description="Anything a credit analyst should know that the fields above don't capture")


SYSTEM = """You read credit agreements for a credit analyst.

Extract only the financial maintenance covenants: the ratios or minimums the borrower must meet when tested \
(usually in a section titled Financial Covenants). Do not include incurrence tests used only to permit \
debt, liens, investments or restricted payments.

You may be given the original agreement followed by later amendments, each in its own <document> tag, oldest \
first. Report the covenants as they stand after every amendment: where an amendment restates or changes a \
covenant, the amendment controls, and quote the amended text.

For each covenant:
- quote the covenant sentence word for word, so it can be found in the documents by exact match;
- record every step-up or step-down in the limit, with the quarter it starts applying;
- read the agreement's EBITDA definition to fill in which add-backs it allows;
- if the covenant only applies under a condition (for example revolver usage above a threshold), say so.

Do not compute any ratios. If the agreement has no financial maintenance covenants, return an empty list."""


def combine(documents: list[tuple[str, str]]) -> str:
    """Join (url, text) pairs, oldest first, each in its own tag."""
    return "\n\n".join(
        f'<document index="{i}" source="{url}">\n{text}\n</document>' for i, (url, text) in enumerate(documents, 1)
    )


def extract(combined_text: str, client: anthropic.Anthropic | None = None) -> tuple[CovenantExtraction, dict]:
    """One streamed request per agreement (long input plus thinking can take minutes). Returns the result and token usage."""
    client = client or anthropic.Anthropic()
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=64000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "high"},
        system=SYSTEM,
        messages=[{"role": "user", "content": combined_text}],
        output_format=CovenantExtraction,
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined to extract covenants: {response.stop_details}")
    if response.parsed_output is None:
        raise RuntimeError(f"No structured output returned (stop_reason={response.stop_reason})")
    usage = {"model": response.model, "input_tokens": response.usage.input_tokens,
             "output_tokens": response.usage.output_tokens, "stop_reason": response.stop_reason}
    return response.parsed_output, usage


def normalize(s: str) -> str:
    """Compare text the way a reader would: same quote marks, spacing and case. HTML-to-text conversion leaves
    stray spaces inside quote marks and parentheses ("“ Total Leverage Ratio ”"), so those are closed up too;
    the same rules apply to both sides of every comparison."""
    s = re.sub(r"\s+", " ", s.replace("\u00a0", " ")).strip().lower()
    s = re.sub(r"([“‘])\s+", r"\1", s)  # space just inside an opening curly quote
    s = re.sub(r"\s+([”’])", r"\1", s)  # space just inside a closing curly quote
    s = s.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    s = re.sub(r"\(\s+", "(", s)
    return re.sub(r"\s+([,.;:)])", r"\1", s)


def verify_quotes(extraction: CovenantExtraction, agreement_text: str) -> dict[str, bool]:
    """For each covenant, is its quote really in the agreement? (whitespace and quote marks normalized)"""
    haystack = normalize(agreement_text)
    return {c.name: normalize(c.quote) in haystack for c in extraction.covenants}


# ---------- the full reading used by the pipeline: covenants plus the document terms lenders worry about ----------

TermKind = Literal["j_crew_blocker", "serta_protection", "chewy_protection", "unrestricted_sub_cap", "addback_cap",
                   "equity_cure"]


class DocTerm(BaseModel):
    kind: TermKind
    present: Literal["yes", "no", "partial"]
    section: str | None
    quote: str | None = Field(description="The operative sentence, word for word; required when present is yes or partial")
    summary: str = Field(description="One or two plain sentences: what the agreement does on this point")


class AgreementReading(CovenantExtraction):
    facility: str = Field(description="One line: the facilities (revolver, term loans), their sizes, and maturity")
    terms: list[DocTerm] = Field(description="Exactly one entry for each of the six term kinds")
    addback_cap_pct: float | None = Field(
        description="Cap on add-backs for projected cost savings, synergies and restructuring, as a share of EBITDA "
        "(0.25 for 25%); null if there is no cap or no such add-back")


TERMS_GUIDE = """Then assess six document terms that private credit lenders check after recent liability management \
transactions. For each, say yes (the agreement has it), no (it doesn't), or partial, quote the operative sentence \
when the answer is yes or partial, and summarize in plain words:
- j_crew_blocker: a ban on transferring or exclusively licensing material intellectual property to unrestricted \
subsidiaries, or on designating a subsidiary that owns it as unrestricted.
- serta_protection: subordinating the loans or their liens to other debt (or a non-pro-rata exchange that does so) \
needs the consent of each affected lender.
- chewy_protection: a guarantor is not released just because it stops being wholly owned.
- unrestricted_sub_cap: a hard cap on investments in, or designation of, unrestricted subsidiaries.
- addback_cap: EBITDA add-backs for projected cost savings, synergies or restructuring are capped.
- equity_cure: the borrower may cure a financial covenant default with new equity (say how often).
Small or investment-grade style agreements often lack these terms entirely; "no" is a normal answer."""

READ_SYSTEM = SYSTEM + "\n\n" + TERMS_GUIDE


def reading_job(job_id: str, combined_text: str, model: str = "claude-opus-5-5", meta: dict | None = None):
    from .llm import Job

    return Job(id=job_id, schema=AgreementReading, content=combined_text, system=READ_SYSTEM, model=model,
               effort="high", max_tokens=48000, meta=meta or {})


def verify_terms(reading: AgreementReading, agreement_text: str) -> dict[str, bool | None]:
    """For each document term, is its quote really in the agreement? None when no quote was needed."""
    hay = normalize(agreement_text)
    return {t.kind: (normalize(t.quote) in hay if t.quote else (None if t.present == "no" else False))
            for t in reading.terms}
