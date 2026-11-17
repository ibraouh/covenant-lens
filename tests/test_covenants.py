import math
from datetime import date

from covenant_lens.covenants import check, ebitda_cushion, headroom, limit_for, upcoming_steps
from covenant_lens.extract import Covenant, Threshold
from covenant_lens.financials import QuarterFinancials


def cov(metric="net_leverage", direction="max", thresholds=((4.25, None), (4.0, "2027-12-01")), **kw):
    base = dict(name="Test Ratio", metric=metric, direction=direction,
                thresholds=[Threshold(value=v, applies_from=d) for v, d in thresholds],
                test_frequency="quarterly", springing_condition=None, section="6.12", quote="q",
                ebitda_addbacks=["stock_comp"], cash_netting_cap=None, cash_netting_cap_pct_ebitda=None)
    return Covenant(**(base | kw))


def quarter(end=date(2026, 9, 30), debt=400.0, cash=50.0, oi=80.0, da=20.0, interest=25.0, sbc=0.0):
    return QuarterFinancials(end, debt, cash, oi, da, interest, sbc, 0.0, 0.0)


def test_step_downs_apply_from_their_date():
    c = cov()
    assert limit_for(c, date(2026, 9, 30)) == 4.25
    assert limit_for(c, date(2027, 12, 31)) == 4.0
    assert upcoming_steps(c, date(2026, 9, 30)) == [(date(2027, 12, 1), 4.0)]


def test_no_limit_before_first_dated_threshold():
    c = cov(thresholds=((3.0, "2026-06-30"),))
    assert limit_for(c, date(2026, 3, 31)) is None


def test_net_leverage_and_dollar_cash_cap():
    assert check(cov(), quarter()).value == (400 - 50) / 100
    capped = cov(cash_netting_cap=20.0)
    assert check(capped, quarter()).value == (400 - 20) / 100


def test_cash_cap_as_share_of_ebitda():
    c = cov(cash_netting_cap_pct_ebitda=0.25)
    assert check(c, quarter(cash=500.0)).value == (400 - 25) / 100


def test_ebitda_override_and_status():
    r = check(cov(), quarter(), ebitda=70.0)
    assert math.isclose(r.value, 5.0) and r.status == "over limit"
    assert check(cov(), quarter()).status == "pass"              # 3.50x vs 4.25x: 18% headroom
    assert check(cov(), quarter(), ebitda=90.0).status == "close"  # 3.89x: within 15% of the limit


def test_coverage_direction():
    c = cov(metric="interest_coverage", direction="min", thresholds=((3.0, None),))
    r = check(c, quarter())
    assert r.value == 4.0 and r.status == "pass"


def test_headroom_and_cushion():
    assert math.isclose(headroom(3.4, 4.0, "max"), 0.15)
    assert math.isclose(ebitda_cushion(3.0, 4.0, "max"), 0.25)
    assert math.isclose(ebitda_cushion(6.0, 3.0, "min"), 0.5)
