"""Test each extracted covenant against the company's quarterly numbers. Plain arithmetic, no AI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .extract import Covenant
from .financials import QuarterFinancials

CLOSE_CALL = 0.15  # flag when the ratio is within 15% of its limit


@dataclass
class Result:
    period_end: date
    covenant: str
    value: float | None
    limit: float | None
    status: str          # "pass", "close", "over limit", or "can't compute" (all estimates)
    headroom: float | None
    detail: str = ""


def limit_for(cov: Covenant, period_end: date) -> float | None:
    """The threshold in force for this quarter, following any step-ups or step-downs."""
    applicable = [t for t in cov.thresholds if t.applies_from is None or date.fromisoformat(t.applies_from) <= period_end]
    if not applicable:
        return None
    dated = [t for t in applicable if t.applies_from]
    if dated:
        return max(dated, key=lambda t: t.applies_from).value
    return applicable[0].value


def _value(cov: Covenant, q: QuarterFinancials, ebitda: float | None = None) -> tuple[float | None, str]:
    """The covenant ratio at this quarter. `ebitda` overrides the estimate built from XBRL line items."""
    ebitda = ebitda if ebitda is not None else q.ebitda(set(cov.ebitda_addbacks))
    if ebitda is None or ebitda <= 0:
        return None, "EBITDA unavailable or not positive"
    if cov.metric == "total_leverage":
        return (q.debt / ebitda, "") if q.debt is not None else (None, "debt not reported")
    if cov.metric == "net_leverage":
        if q.debt is None:
            return None, "debt not reported"
        cash = q.cash or 0.0
        if cov.cash_netting_cap is not None:
            cash = min(cash, cov.cash_netting_cap)
        if cov.cash_netting_cap_pct_ebitda is not None:
            cash = min(cash, cov.cash_netting_cap_pct_ebitda * ebitda)
        return (q.debt - cash) / ebitda, ""
    if cov.metric == "interest_coverage":
        if not q.ltm_interest:
            return None, "interest expense not reported"
        return ebitda / q.ltm_interest, ""
    return None, f"{cov.metric.replace('_', ' ')} needs data public filings don't break out"


def check(cov: Covenant, q: QuarterFinancials, ebitda: float | None = None) -> Result:
    limit = limit_for(cov, q.period_end)
    value, why = _value(cov, q, ebitda)
    if value is None or limit is None:
        return Result(q.period_end, cov.name, value, limit, "can't compute", None, why or "no limit in force")
    headroom = (limit - value) / limit if cov.direction == "max" else (value - limit) / limit
    if headroom < 0:
        status = "over limit"
    elif headroom < CLOSE_CALL:
        status = "close"
    else:
        status = "pass"
    note = "springing: only tested if its condition is met" if cov.test_frequency == "springing" else ""
    return Result(q.period_end, cov.name, value, limit, status, headroom, note)


def headroom(value: float, limit: float, direction: str) -> float:
    """Share of the limit still unused: positive is room left, negative is over the limit."""
    return (limit - value) / limit if direction == "max" else (value - limit) / limit


def ebitda_cushion(value: float, limit: float, direction: str) -> float:
    """How far EBITDA can fall before the covenant trips, holding debt, cash and interest steady.

    Leverage is debt over EBITDA, so it reaches the limit when EBITDA falls to value/limit of today's level.
    Coverage is EBITDA over interest, so it reaches the limit when EBITDA falls to limit/value of today's.
    (Exact unless netted cash is capped as a share of EBITDA, where it is a close approximation.)
    """
    if value <= 0 or limit <= 0:
        return float("nan")
    return 1 - value / limit if direction == "max" else 1 - limit / value


def upcoming_steps(cov: Covenant, after: date) -> list[tuple[date, float]]:
    """Scheduled changes to the limit after a given quarter-end, in order."""
    return sorted((date.fromisoformat(t.applies_from), t.value) for t in cov.thresholds
                  if t.applies_from and date.fromisoformat(t.applies_from) > after)
