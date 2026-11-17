from datetime import date

from covenant_lens.financials import QuarterFinancials, _ltm, _quarterly_flows, _resolve


def fact(start, end, val, filed="2026-01-01"):
    return {"start": start, "end": end, "val": val, "filed": filed}


def gaap(tag, facts):
    return {tag: {"units": {"USD": facts}}}


def test_q4_is_full_year_minus_nine_months():
    g = gaap("OperatingIncomeLoss", [
        fact("2025-01-01", "2025-03-31", 10), fact("2025-04-01", "2025-06-30", 20),
        fact("2025-07-01", "2025-09-30", 30),
        fact("2025-01-01", "2025-09-30", 60), fact("2025-01-01", "2025-12-31", 100),
    ])
    q = _quarterly_flows(g, "OperatingIncomeLoss")
    assert q[date(2025, 12, 31)] == 40
    assert q[date(2025, 9, 30)] == 30


def test_quarters_from_year_to_date_only():
    g = gaap("X", [fact("2025-01-01", "2025-03-31", 5), fact("2025-01-01", "2025-06-30", 12),
                   fact("2025-01-01", "2025-09-30", 20)])
    q = _quarterly_flows(g, "X")
    assert q == {date(2025, 3, 31): 5, date(2025, 6, 30): 7, date(2025, 9, 30): 8}


def test_restated_value_uses_latest_filing():
    g = gaap("X", [fact("2025-01-01", "2025-03-31", 5, filed="2025-05-01"),
                   fact("2025-01-01", "2025-03-31", 6, filed="2026-05-01")])
    assert _quarterly_flows(g, "X")[date(2025, 3, 31)] == 6


def test_ltm_needs_four_quarters_ending_on_the_date():
    q = {date(2025, 3, 31): 1, date(2025, 6, 30): 2, date(2025, 9, 30): 3, date(2025, 12, 31): 4}
    assert _ltm(q, date(2025, 12, 31)) == 10
    assert _ltm(q, date(2026, 3, 31)) is None
    assert _ltm({date(2025, 12, 31): 4}, date(2025, 12, 31), missing_is_zero=True) == 4


def test_ltm_handles_52_53_week_years():
    q = {date(2025, 1, 3): 1, date(2025, 4, 4): 1, date(2025, 7, 4): 1, date(2025, 10, 3): 1}
    assert _ltm(q, date(2025, 10, 3)) == 4


def test_resolve_prefers_source_with_more_recent_points():
    g = {**gaap("A", [fact("2024-01-01", "2024-12-31", 100)]),
         **gaap("B", [fact("2025-01-01", "2025-03-31", 1), fact("2025-04-01", "2025-06-30", 2)])}
    series, src = _resolve(g, ["A", "B"], flow=True, since=date(2025, 1, 1))
    assert src == "B" and len(series) == 2


def test_ebitda_applies_only_requested_addbacks():
    q = QuarterFinancials(date(2026, 9, 30), 100, 10, 20, 5, 4, 3, 2, 1)
    assert q.ebitda(set()) == 25
    assert q.ebitda({"stock_comp", "impairments", "restructuring"}) == 31
