"""What companies say about themselves, used as answer keys.

1. Reported covenant ratios. Many borrowers print their actual covenant ratios, and the limits in force, in
   the liquidity section of each 10-Q and 10-K. Those numbers come from the compliance certificate the
   company sends its lenders, so they are the closest public thing to ground truth. They grade both halves
   of the tool: the limit checks what Claude read in the agreement, and the actual ratio checks the math.
2. Adjusted EBITDA from earnings releases. Companies' own adjusted EBITDA usually sits much closer to
   covenant EBITDA than anything rebuilt from GAAP line items, so it gives a second, better estimate.

Claude only reads excerpts (a few thousand tokens per filing). Every number it returns must come with a
verbatim quote that is found in the excerpt and contains the number, or it is dropped.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from . import edgar
from .extract import Metric, normalize
from .llm import Job

# ---------- reported covenant ratios ----------

RATIO_PATTERN = r"(leverage|coverage|fixed[- ]charge)\s+ratio"


class ReportedRatio(BaseModel):
    covenant_name: str = Field(description="The ratio's name as the filing gives it")
    metric: Metric
    period_end: str = Field(description="Date the ratio is measured at, YYYY-MM-DD")
    actual: float | None = Field(description="The company's actual ratio at that date, e.g. 2.76; null if the "
                                             "filing gives only the requirement or only says it is in compliance")
    limit: float | None = Field(description="The covenant requirement in force at that date, e.g. 4.0; null if not stated")
    direction: Literal["max", "min"] | None
    facility: str | None = Field(description="Which credit agreement or facility the covenant belongs to, as named")
    quote: str = Field(description="The sentence or table row that shows the numbers, copied exactly from the excerpt")


class ReportedRatios(BaseModel):
    ratios: list[ReportedRatio]
    notes: str = Field(description="Anything that affects how these numbers should be read, briefly")


RATIO_SYSTEM = """You read excerpts from a company's 10-Q or 10-K: the parts that mention leverage, coverage or \
fixed charge ratios. Report the financial covenant ratios the company discloses for its credit agreement \
(bank revolving credit facilities and term loans).

Include a ratio only when the filing ties it to a covenant in a credit agreement. Leave out:
- leverage targets, capital allocation policies, and rating-agency metrics;
- incurrence tests in bond indentures, and ratios that only set an interest rate margin or a fee;
- non-GAAP leverage the company reports as a performance measure without tying it to a covenant;
- ratios for unconsolidated joint ventures or other borrowers.

For each covenant ratio give the actual value and the limit at each date the excerpt states them, as separate \
entries per date. When a table has columns for the actual figure and the requirement, read the columns \
carefully. Copy the quote exactly as it appears in the excerpt, including odd spacing from tables, so it can be \
found by exact match. If the excerpt has no covenant ratios, return an empty list."""


def ratio_job(job_id: str, company: str, filing: dict, excerpt: str) -> Job:
    content = (f"Company: {company}\nFiling: {filing['form']} for the period ended {filing['period']} "
               f"(filed {filing['filed']})\n\n<excerpt>\n{excerpt}\n</excerpt>")
    return Job(id=job_id, schema=ReportedRatios, content=content, system=RATIO_SYSTEM, effort="medium",
               max_tokens=16000, meta={"url": filing["url"], "period": filing["period"]})


def ratio_excerpt(text: str) -> str:
    """The parts of a filing that state a ratio with a number next to it."""
    return edgar.windows(" ".join(text.split()), RATIO_PATTERN, before=900, after=900, limit=14000,
                         must=r"\d\.\d{1,2}")


def _number_in(value: float, quote: str) -> bool:
    """Is this value printed in the quote (as 2.76, 2.8, 4.00, 4:00, 4x...)?"""
    q = quote.replace(",", "")
    nums = [float(m.replace(":", ".")) for m in re.findall(r"\d+(?:[.:]\d+)?", q)]
    return any(abs(n - value) < 0.006 for n in nums)


def verified_ratios(parsed: ReportedRatios, excerpt: str) -> tuple[list[dict], list[dict]]:
    """Keep ratios whose quote is in the excerpt and whose numbers are in the quote. Returns (kept, dropped)."""
    hay = normalize(excerpt)
    kept, dropped = [], []
    for r in parsed.ratios:
        d = r.model_dump()
        problems = []
        if normalize(r.quote) not in hay:
            problems.append("quote not found in filing")
        for name in ("actual", "limit"):
            v = getattr(r, name)
            if v is not None and not _number_in(v, r.quote):
                problems.append(f"{name} {v} not in quote")
        try:
            date.fromisoformat(r.period_end)
        except ValueError:
            problems.append("bad date")
        (dropped if problems else kept).append(d | ({"problems": problems} if problems else {}))
    return kept, dropped


# ---------- adjusted EBITDA from earnings releases ----------

class EbitdaFigure(BaseModel):
    period_end: str = Field(description="Last day of the period, YYYY-MM-DD")
    months: int = Field(description="Length of the period: 3 for a quarter, 6, 9 or 12 for year to date")
    shown: str = Field(description="The number exactly as printed, e.g. '61.4' or '1,234' or '(12.0)'")
    unit: Literal["dollars", "thousands", "millions", "billions"]
    quote: str = Field(description="The table row or sentence it came from, copied exactly")


class EbitdaRelease(BaseModel):
    label: str | None = Field(description="The company's name for the measure, e.g. 'Adjusted EBITDA'; null if none")
    figures: list[EbitdaFigure]
    notes: str


EBITDA_SYSTEM = """You read excerpts from a company's quarterly earnings release. Report its adjusted EBITDA (the \
company's own non-GAAP EBITDA measure, whatever it calls it: Adjusted EBITDA, Consolidated Adjusted EBITDA, \
Segment EBITDA in total, and so on) for every period the excerpt shows: the quarter just ended, the same quarter \
a year earlier, and any year-to-date or full-year totals. Use the reconciliation table when there is one.

Give each number exactly as printed with its unit (the table header usually says "in thousands" or "in \
millions"), and copy the row or sentence it came from exactly. Report company-wide totals only, not segments. \
If the company reports no adjusted EBITDA, return an empty list."""

EBITDA_PATTERN = r"adjusted\s+ebitda|consolidated\s+ebitda"


def ebitda_job(job_id: str, company: str, release: dict, excerpt: str) -> Job:
    content = (f"Company: {company}\nEarnings release filed {release['filed']}\n\n<excerpt>\n{excerpt}\n</excerpt>")
    return Job(id=job_id, schema=EbitdaRelease, content=content, system=EBITDA_SYSTEM, effort="medium",
               max_tokens=16000, meta={"url": release["url"], "filed": release["filed"]})


def ebitda_excerpt(text: str) -> str:
    return edgar.windows(" ".join(text.split()), EBITDA_PATTERN, before=300, after=1500, limit=12000,
                         must=r"\d")


UNIT = {"dollars": 1, "thousands": 1e3, "millions": 1e6, "billions": 1e9}


def to_number(shown: str) -> float | None:
    s = shown.strip().replace("$", "").replace(",", "").replace(" ", "")
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = s.strip("()-")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def verified_ebitda(parsed: EbitdaRelease, excerpt: str) -> list[dict]:
    """Quarterly figures whose quote is in the excerpt and contains the printed number, in dollars."""
    hay = normalize(excerpt)
    out = []
    for f in parsed.figures:
        v = to_number(f.shown)
        digits = f.shown.strip("$() ").replace(" ", "")
        if v is None or f.months != 3 or normalize(f.quote) not in hay or digits not in f.quote.replace(" ", ""):
            continue
        try:
            end = date.fromisoformat(f.period_end)
        except ValueError:
            continue
        out.append({"period_end": end.isoformat(), "value": v * UNIT[f.unit], "quote": f.quote})
    return out


def earnings_releases(cik: str, since: str, until: str) -> list[dict]:
    """8-K Item 2.02 press releases (EX-99.x) that mention adjusted EBITDA, newest first."""
    hits = edgar.search('"Adjusted EBITDA"', forms="8-K", start=since, end=until, ciks=cik, max_pages=2)
    out, seen = [], set()
    for h in hits:
        src = h["_source"]
        if not src.get("file_type", "").startswith("EX-99") or "2.02" not in (src.get("items") or []):
            continue
        if src["adsh"] in seen:
            continue
        seen.add(src["adsh"])
        out.append({"filed": src["file_date"], "url": edgar.hit_url(h)})
    return sorted(out, key=lambda r: r["filed"], reverse=True)
