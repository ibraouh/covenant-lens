from covenant_lens.agreements import resolve
from covenant_lens.disclosures import (
    EbitdaFigure,
    EbitdaRelease,
    ReportedRatio,
    ReportedRatios,
    _number_in,
    to_number,
    verified_ebitda,
    verified_ratios,
)
from covenant_lens.extract import normalize
from covenant_lens.llm import HAIKU, OPUS, cost

EXCERPT = ("Covenant Requirements at September 30, 2026 Maximum consolidated net leverage ratio (1) 2.76 4.00 "
           "Minimum interest coverage ratio 7.63 3.00")


def ratio(**kw):
    base = dict(covenant_name="Net leverage", metric="net_leverage", period_end="2026-09-30", actual=2.76, limit=4.0,
                direction="max", facility=None, quote="Maximum consolidated net leverage ratio (1) 2.76 4.00")
    return ReportedRatio(**(base | kw))


def test_normalize_handles_quotes_spaces_case():
    assert normalize("“Consolidated  EBITDA” means") == '"consolidated ebitda" means'


def test_ratio_kept_when_quote_and_numbers_match():
    kept, dropped = verified_ratios(ReportedRatios(ratios=[ratio()], notes=""), EXCERPT)
    assert len(kept) == 1 and not dropped


def test_ratio_dropped_when_number_not_in_quote():
    kept, dropped = verified_ratios(ReportedRatios(ratios=[ratio(actual=2.67)], notes=""), EXCERPT)
    assert not kept and "actual 2.67 not in quote" in dropped[0]["problems"]


def test_ratio_dropped_when_quote_invented():
    kept, dropped = verified_ratios(ReportedRatios(ratios=[ratio(quote="net leverage ratio was 2.76 to 1.00")], notes=""),
                                    EXCERPT)
    assert not kept and "quote not found in filing" in dropped[0]["problems"]


def test_number_formats():
    assert _number_in(4.0, "exceed 4:00 to 1.00")
    assert _number_in(1250.0, "$1,250.0 million")
    assert to_number("(1,234.5)") == -1234.5 and to_number("$61.4") == 61.4 and to_number("n/a") is None


def test_ebitda_figures_in_dollars_and_quarters_only():
    excerpt = "Adjusted EBITDA $ 61.4 $ 58.0 $ 180.2"
    parsed = EbitdaRelease(label="Adjusted EBITDA", notes="", figures=[
        EbitdaFigure(period_end="2026-09-30", months=3, shown="61.4", unit="millions", quote="Adjusted EBITDA $ 61.4 $ 58.0"),
        EbitdaFigure(period_end="2026-09-30", months=9, shown="180.2", unit="millions", quote="$ 180.2"),
        EbitdaFigure(period_end="2025-09-30", months=3, shown="85.0", unit="millions", quote="Adjusted EBITDA $ 61.4"),
    ])
    out = verified_ebitda(parsed, excerpt)
    assert out == [{"period_end": "2026-09-30", "value": 61.4e6, "quote": "Adjusted EBITDA $ 61.4 $ 58.0"}]


def doc(url, kind, dated, amends=None, primary=True, conformed=False, chars=1000, filed="2026-01-01", title=None):
    return {"url": url, "filed": filed, "chars": chars,
            "cover": {"is_loan_document": kind != "other", "kind": kind, "title": title or url, "dated": dated,
                      "amends_dated": amends, "primary_bank_facility": primary, "includes_conformed_copy": conformed,
                      "borrower": "X"}}


def test_chain_takes_latest_primary_base_and_its_amendments_in_order():
    docs = [doc("old", "original", "2019-05-01"), doc("a_old", "amendment", "2020-01-01", amends="2019-05-01"),
            doc("base", "amended_and_restated", "2024-09-27"), doc("equip", "original", "2025-02-01", primary=False),
            doc("a2", "amendment", "2026-03-01", amends="2024-09-27"), doc("a1", "amendment", "2025-07-01", amends="2024-09-27"),
            doc("guar", "other", "2024-09-27")]
    chain, notes = resolve(docs)
    assert [d["url"] for d in chain] == ["base", "a1", "a2"] and not notes


def test_conformed_copy_becomes_the_new_base():
    docs = [doc("base", "original", "2022-01-01"), doc("a1", "amendment", "2023-01-01", amends="2022-01-01"),
            doc("a2", "amendment", "2024-01-01", amends="2022-01-01", conformed=True),
            doc("a3", "amendment", "2025-01-01", amends="2022-01-01")]
    chain, notes = resolve(docs)
    assert [d["url"] for d in chain] == ["a2", "a3"] and notes


def test_no_base_found():
    assert resolve([doc("a", "amendment", "2025-01-01", amends="2020-01-01")]) == ([], ["no base credit agreement found"])


def test_costs():
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000}
    assert cost(OPUS, usage) == 4 + 2
    assert cost(OPUS, usage, batch=True) == 3
    assert cost(HAIKU, {"input_tokens": 50_000, "output_tokens": 0}) == 50_000 * 0.10 / 1e6
    assert cost(HAIKU, {"input_tokens": 200_000, "output_tokens": 0}) == 200_000 * 0.50 / 1e6
    cached = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 1_000_000}
    assert abs(cost(OPUS, cached) - 0.20) < 1e-9


def test_normalize_closes_spaces_html_leaves_inside_quotes_and_parens():
    from_html = 'the ratio (the “ Total Leverage Ratio ” ) , determined'
    quoted = 'the ratio (the "Total Leverage Ratio"), determined'
    assert normalize(from_html) == normalize(quoted)


def test_restating_amendment_beats_a_side_facility():
    docs = [doc("warehouse", "amended_and_restated", "2023-05-26", primary=False),
            doc("a6", "amendment", "2022-12-09", amends="2013-07-18", conformed=True),
            doc("a7", "amendment", "2025-09-18", amends="2013-07-18", conformed=True),
            doc("a8", "amendment", "2026-02-01", amends="2013-07-18")]
    chain, _ = resolve(docs)
    assert [d["url"] for d in chain] == ["a7", "a8"]


def test_struck_through_text_is_dropped_as_deleted():
    from bs4 import BeautifulSoup

    from covenant_lens.edgar import deleted_text

    html = ('<p>shall not exceed <span style="text-decoration: line-through">5.25</span> <u>3.50</u> to 1.00'
            '<s> old clause</s><del> gone</del></p>')
    soup = BeautifulSoup(html, "html.parser")
    for t in deleted_text(soup):
        t.decompose()
    assert " ".join(soup.get_text(" ").split()) == "shall not exceed 3.50 to 1.00"
