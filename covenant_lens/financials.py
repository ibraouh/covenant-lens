"""Turn a company's XBRL facts into quarter-end debt, cash and trailing-twelve-month (LTM) figures.

Companies report income-statement numbers year-to-date in 10-Qs and for the full year in 10-Ks,
so a quarter's own number often isn't reported directly. We rebuild each quarter from those
year-to-date figures, then add up the last four quarters. All of this is plain arithmetic: no AI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import edgar

# Each line item tries these sources in order. A tuple means "add these tags together".
DEBT = [
    "LongTermDebt",
    "DebtAndCapitalLeaseObligations",
    "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
    ("LongTermDebtNoncurrent", "LongTermDebtCurrent"),
    ("LongTermDebtNoncurrent", "DebtCurrent"),
    ("LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsCurrent"),
]
CASH = ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]
OPERATING_INCOME = ["OperatingIncomeLoss"]
D_AND_A = [
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAndAmortization",
    ("Depreciation", "AmortizationOfIntangibleAssets"),
]
INTEREST = ["InterestExpenseNonoperating", "InterestExpense", "InterestExpenseDebt"]
STOCK_COMP = ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]
# One-off charges that covenant EBITDA definitions commonly add back. They show up only in the
# quarters they happen, so a missing quarter counts as zero.
IMPAIRMENTS = [
    "GoodwillAndIntangibleAssetImpairment",
    "AssetImpairmentCharges",
    "GoodwillImpairmentLoss",
    "ImpairmentOfLongLivedAssetsHeldForUse",
]
RESTRUCTURING = ["RestructuringCharges", "SeveranceCosts1"]


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _facts(gaap: dict, tag: str) -> list[dict]:
    """USD facts for one tag, keeping the most recently filed value for each period."""
    units = gaap.get(tag, {}).get("units", {})
    best: dict[tuple, dict] = {}
    for f in units.get("USD", []):
        key = (f.get("start"), f["end"])
        if key not in best or f.get("filed", "") > best[key].get("filed", ""):
            best[key] = f
    return list(best.values())


def _quarterly_flows(gaap: dict, tag: str) -> dict[date, float]:
    """Each fiscal quarter's own value, keyed by quarter-end date."""
    facts = [f for f in _facts(gaap, tag) if f.get("start")]
    quarters: dict[date, float] = {}
    for f in facts:  # quarters reported directly (about three months long)
        if 80 <= (_d(f["end"]) - _d(f["start"])).days <= 100:
            quarters[_d(f["end"])] = f["val"]
    by_start: dict[str, list[dict]] = {}
    for f in facts:
        by_start.setdefault(f["start"], []).append(f)
    for group in by_start.values():  # quarters implied by two year-to-date figures (incl. Q4 = year - 9 months)
        group.sort(key=lambda f: f["end"])
        for prev, cur in zip(group, group[1:], strict=False):
            end = _d(cur["end"])
            if 80 <= (end - _d(prev["end"])).days <= 100 and end not in quarters:
                quarters[end] = cur["val"] - prev["val"]
    return quarters


def _instants(gaap: dict, tag: str) -> dict[date, float]:
    return {_d(f["end"]): f["val"] for f in _facts(gaap, tag) if not f.get("start")}


def _resolve(gaap: dict, sources: list, flow: bool, since: date) -> tuple[dict[date, float], str]:
    """Pick the source with the most recent data points (earlier sources win ties)."""
    getter = _quarterly_flows if flow else _instants
    best: tuple[int, dict[date, float], str] = (0, {}, "not reported")
    for src in sources:
        tags = src if isinstance(src, tuple) else (src,)
        series = [getter(gaap, t) for t in tags]
        if not all(series):
            continue
        common = set.intersection(*(set(s) for s in series))
        combined = {d: sum(s[d] for s in series) for d in common}
        recent = sum(1 for d in combined if d >= since)
        if recent > best[0]:
            best = (recent, combined, " + ".join(tags))
    return best[1], best[2]


def _ltm(quarters: dict[date, float], end: date, missing_is_zero: bool = False) -> float | None:
    """Sum of the four quarters ending on `end`. None if a quarter is missing (unless missing_is_zero)."""
    window = sorted(d for d in quarters if 0 <= (end - d).days < 360)
    if missing_is_zero:
        return sum(quarters[d] for d in window)
    if len(window) != 4 or window[-1] != end:
        return None
    return sum(quarters[d] for d in window)


@dataclass
class QuarterFinancials:
    period_end: date
    debt: float | None
    cash: float | None
    ltm_operating_income: float | None
    ltm_d_and_a: float | None
    ltm_interest: float | None
    ltm_stock_comp: float | None
    ltm_impairments: float
    ltm_restructuring: float

    def ebitda(self, addbacks: set[str]) -> float | None:
        """Operating income + D&A, plus whichever add-backs the credit agreement allows.

        addbacks may contain "stock_comp", "impairments", "restructuring".
        """
        if self.ltm_operating_income is None or self.ltm_d_and_a is None:
            return None
        total = self.ltm_operating_income + self.ltm_d_and_a
        if "stock_comp" in addbacks:
            total += self.ltm_stock_comp or 0.0
        if "impairments" in addbacks:
            total += self.ltm_impairments
        if "restructuring" in addbacks:
            total += self.ltm_restructuring
        return total


@dataclass
class CompanyFinancials:
    name: str
    quarters: list[QuarterFinancials]
    sources: dict[str, str] = field(default_factory=dict)


def load(cik: str, since: str = "2023-01-01") -> CompanyFinancials:
    facts = edgar.company_facts(cik)
    gaap = facts["facts"].get("us-gaap", {})
    start = _d(since)
    debt, debt_src = _resolve(gaap, DEBT, flow=False, since=start)
    cash, cash_src = _resolve(gaap, CASH, flow=False, since=start)
    oi, oi_src = _resolve(gaap, OPERATING_INCOME, flow=True, since=start)
    da, da_src = _resolve(gaap, D_AND_A, flow=True, since=start)
    interest, int_src = _resolve(gaap, INTEREST, flow=True, since=start)
    sbc, sbc_src = _resolve(gaap, STOCK_COMP, flow=True, since=start)
    imp, imp_src = _resolve(gaap, IMPAIRMENTS, flow=True, since=start)
    rst, rst_src = _resolve(gaap, RESTRUCTURING, flow=True, since=start)
    period_ends = sorted(d for d in debt if d >= start)
    quarters = [
        QuarterFinancials(
            period_end=end,
            debt=debt.get(end),
            cash=cash.get(end),
            ltm_operating_income=_ltm(oi, end),
            ltm_d_and_a=_ltm(da, end),
            ltm_interest=_ltm(interest, end),
            ltm_stock_comp=_ltm(sbc, end),
            ltm_impairments=_ltm(imp, end, missing_is_zero=True) or 0.0,
            ltm_restructuring=_ltm(rst, end, missing_is_zero=True) or 0.0,
        )
        for end in period_ends
    ]
    return CompanyFinancials(
        name=facts.get("entityName", cik),
        quarters=quarters,
        sources={"debt": debt_src, "cash": cash_src, "operating income": oi_src, "D&A": da_src,
                 "interest": int_src, "stock comp": sbc_src, "impairments": imp_src,
                 "restructuring": rst_src},
    )
