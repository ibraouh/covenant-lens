"""Find a borrower's current credit agreement and every amendment to it, from EDGAR alone.

Companies file credit agreements and amendments as exhibits to 8-Ks, 10-Qs and 10-Ks, years apart. To know
what a covenant says today you need the right base agreement (the latest original or amended-and-restated
version of the main bank facility) plus every later amendment to it, in order. This module:

1. searches EDGAR for exhibits that look like loan documents for the company;
2. has Claude read only the cover page of each (title, date, what it amends);
3. resolves the chain in code.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from . import edgar
from .llm import Job

MAX_CHARS = 1_400_000  # about 500K tokens: keeps one agreement chain comfortably inside one request


class Cover(BaseModel):
    is_loan_document: bool = Field(description="A credit or loan agreement, or an amendment, restatement or joinder to one")
    kind: Literal["original", "amended_and_restated", "amendment", "other"] = Field(
        description="original: a new credit agreement; amended_and_restated: a full restatement; amendment: changes "
        "an existing agreement (including incremental and refinancing amendments); other: guarantee, security "
        "agreement, indenture, note purchase agreement, or anything else")
    title: str
    dated: str | None = Field(description="The date this document is dated as of, YYYY-MM-DD")
    amends_dated: str | None = Field(description="For amendments: the date of the credit agreement it amends, YYYY-MM-DD")
    primary_bank_facility: bool = Field(description="The company's main revolving credit and/or term loan agreement "
                                                     "with banks, not an equipment, receivables, vendor, or bridge facility")
    includes_conformed_copy: bool = Field(description="The amendment attaches or restates the full amended agreement")
    borrower: str


COVER_SYSTEM = """You read the first pages of a document a company filed as an exhibit with the SEC, and say what \
kind of document it is. Dates must be the "dated as of" dates written in the document, not filing dates."""


def candidates(cik: str, until: str) -> list[dict]:
    """Exhibits that mention a credit agreement, an administrative agent and lenders."""
    hits = edgar.search('"administrative agent" "lenders" "credit agreement"', forms="8-K,10-Q,10-K",
                        start="2016-01-01", end=until, ciks=cik, max_pages=3)
    out, seen = [], set()
    for h in hits:
        src = h["_source"]
        url = edgar.hit_url(h)
        if not src.get("file_type", "").startswith(("EX-10", "EX-4")) or url in seen:
            continue
        if not url.lower().endswith((".htm", ".html", ".txt")):
            continue
        seen.add(url)
        out.append({"url": url, "filed": src["file_date"], "exhibit": src["file_type"]})
    return sorted(out, key=lambda c: c["filed"])


def cover_job(job_id: str, text: str, meta: dict) -> Job:
    return Job(id=job_id, schema=Cover, content=f"<first_pages>\n{text[:6000]}\n</first_pages>",
               system=COVER_SYSTEM, effort="low", max_tokens=4000, meta=meta)


def _d(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def resolve(docs: list[dict]) -> tuple[list[dict], list[str]]:
    """Pick the base agreement and its amendments, oldest first. Each doc: url, filed, chars, cover (dict).

    Returns (chain, notes). An amendment that attaches a conformed copy of the whole agreement becomes the new
    base, so older documents can be left out without losing anything.
    """
    notes: list[str] = []
    loan = [d for d in docs if d["cover"]["is_loan_document"] and d["cover"]["kind"] != "other"
            and _d(d["cover"]["dated"])]
    bases = [d for d in loan if d["cover"]["kind"] in ("original", "amended_and_restated")]
    restating = [d for d in loan if d["cover"]["kind"] == "amendment" and d["cover"]["includes_conformed_copy"]]
    # The main bank facility first: its original, or an amendment that restates it in full when the original
    # is too old to find. A side facility (mortgage warehouse, equipment line) only if nothing else exists.
    primary = [d for d in bases + restating if d["cover"]["primary_bank_facility"]] or bases
    if not primary:
        return [], ["no base credit agreement found"]
    # newest by date; the same document filed twice (8-K, then again with a 10-Q) keeps its first filing
    base = max(primary, key=lambda d: (_d(d["cover"]["dated"]), -date.fromisoformat(d["filed"]).toordinal(), d["url"]))
    if base["cover"]["kind"] == "amendment":  # a restating amendment: its later siblings amend the same agreement
        notes.append(f"{base['cover']['title']} restates the full agreement; used as the base")
        base_date = _d(base["cover"]["amends_dated"])
        later = sorted((d for d in loan if d["cover"]["kind"] == "amendment" and _d(d["cover"]["amends_dated"]) == base_date
                        and _d(d["cover"]["dated"]) > _d(base["cover"]["dated"])), key=lambda d: (_d(d["cover"]["dated"]), d["filed"]))
        return [base] + later, notes
    base_date = _d(base["cover"]["dated"])
    amendments = sorted(
        (d for d in loan if d["cover"]["kind"] == "amendment" and _d(d["cover"]["amends_dated"]) == base_date
         and _d(d["cover"]["dated"]) >= base_date and d["url"] != base["url"]),
        key=lambda d: (_d(d["cover"]["dated"]), d["filed"]))
    chain = [base] + amendments
    conformed = [i for i, d in enumerate(chain) if i and d["cover"]["includes_conformed_copy"]]
    if conformed:
        last = conformed[-1]
        notes.append(f"{chain[last]['cover']['title']} restates the full agreement; earlier documents left out")
        chain = chain[last:]
    while sum(d["chars"] for d in chain) > MAX_CHARS and len(chain) > 1:
        dropped = chain.pop(1)
        notes.append(f"left out {dropped['cover']['title']} ({dropped['cover']['dated']}) to stay within size limits")
    if sum(d["chars"] for d in chain) > MAX_CHARS:
        return [], ["agreement too long for one request"]
    return chain, notes
