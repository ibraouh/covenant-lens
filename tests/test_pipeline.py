from datetime import date

from covenant_lens.extract import AgreementReading, Covenant, DocTerm, Threshold, verify_terms
from covenant_lens.pipeline import _grade, _latest, _near, summarize


def cov(metric="net_leverage", direction="max", limit=4.0):
    return Covenant(name="Net Leverage", metric=metric, direction=direction, thresholds=[Threshold(value=limit, applies_from=None)],
                    test_frequency="quarterly", springing_condition=None, section="7.11", quote="q", ebitda_addbacks=[],
                    cash_netting_cap=None, cash_netting_cap_pct_ebitda=None)


def row(end, xbrl=None, adjusted=None, reported=None, limit=4.0, reported_limit=None, before=False):
    return {"period_end": end, "before_agreement": before, "limit": limit, "xbrl": xbrl, "adjusted": adjusted,
            "reported": reported, "reported_limit": reported_limit}


def test_near_matches_within_tolerance_only():
    opts = [date(2026, 9, 30), date(2026, 6, 30)]
    assert _near(date(2026, 10, 2), opts) == date(2026, 9, 30)
    assert _near(date(2026, 8, 15), opts) is None


def test_latest_prefers_company_figure_then_better_estimate():
    c = cov()
    rows = [row("2026-06-30", xbrl=3.0, adjusted=2.8, reported=2.7, reported_limit=4.0),
            row("2026-09-30", xbrl=3.2, adjusted=3.0)]
    latest = _latest(c, rows)
    assert latest["basis"] == "adjusted" and latest["period_end"] == "2026-09-30"
    rows[1]["reported"] = 2.9
    assert _latest(c, rows)["basis"] == "reported"


def test_latest_skips_quarters_before_the_agreement():
    rows = [row("2026-06-30", xbrl=3.0), row("2026-09-30", xbrl=9.0, before=True)]
    assert _latest(cov(), rows)["period_end"] == "2026-06-30"


def test_grade_scores_limit_and_both_estimates():
    reading = AgreementReading(borrower="X", agreement_date=None, covenants=[cov()], notes="", facility="", terms=[],
                               addback_cap_pct=None)
    covs = [{"name": "Net Leverage", "rows": [row("2026-09-30", xbrl=3.3, adjusted=2.86)]}]
    key = {"actual": 2.75, "limit": 4.0, "quote": "q", "url": "u", "facility": None}
    g = _grade(reading, covs, "net_leverage", "2026-09-30", key)
    assert g["limit_match"] is True and g["covenant_found"]
    assert abs(g["xbrl_error"] - 0.2) < 1e-9 and abs(g["adjusted_error"] - 0.04) < 1e-9
    missing = _grade(reading, covs, "interest_coverage", "2026-09-30", key)
    assert missing["covenant_found"] is False and missing["limit_match"] is False


def test_summary_pairs_only_quarters_with_both_estimates():
    acc = [{"limit_match": True, "covenant_found": True, "xbrl_error": 0.2, "adjusted_error": 0.05},
           {"limit_match": False, "covenant_found": True, "xbrl_error": -0.3, "adjusted_error": None},
           {"limit_match": None, "covenant_found": False, "xbrl_error": None, "adjusted_error": None}]
    s = summarize([{"cik": "1", "accuracy": acc, "covenants": [{"quote_verified": True}], "terms": []}])
    assert (s["limits_checked"], s["limits_matched"], s["covenant_found"]) == (2, 1, 2)
    assert s["paired_points"] == 1 and s["paired_xbrl_median"] == 0.2 and s["paired_adjusted_median"] == 0.05
    assert s["xbrl_median_error"] == 0.25


def test_terms_need_a_real_quote_unless_absent():
    reading = AgreementReading(borrower="X", agreement_date=None, covenants=[], notes="", facility="", addback_cap_pct=None,
                               terms=[DocTerm(kind="j_crew_blocker", present="yes", section="7.02", quote="no Material IP", summary=""),
                                      DocTerm(kind="serta_protection", present="no", section=None, quote=None, summary=""),
                                      DocTerm(kind="equity_cure", present="yes", section=None, quote="made up", summary="")])
    ok = verify_terms(reading, "the Borrower shall transfer no  Material IP to any Unrestricted Subsidiary")
    assert ok == {"j_crew_blocker": True, "serta_protection": None, "equity_cure": False}
