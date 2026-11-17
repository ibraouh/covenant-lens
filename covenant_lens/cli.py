"""Command line: find agreements, check covenants, score the AI against hand labels.

  python -m covenant_lens find "Consolidated Total Net Leverage Ratio"
  python -m covenant_lens check --cik 1023731 --doc <EDGAR exhibit URL> --name 8x8
  python -m covenant_lens check --gold evals/gold/8x8.json        # use hand labels, no AI
  python -m covenant_lens eval                                     # AI vs every hand-labeled agreement
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from . import covenants, edgar, financials
from .extract import CovenantExtraction, combine, extract, verify_quotes

ROOT = edgar.ROOT
EXTRACTIONS = ROOT / "data" / "extractions"
REPORTS = ROOT / "reports"
GOLD = ROOT / "evals" / "gold"


def _money(v: float | None) -> str:
    return "n/a" if v is None else f"${v / 1e6:,.1f}M"


def _ai_extraction(name: str, text: str, refresh: bool = False) -> CovenantExtraction:
    """Run Claude once per agreement and cache the result, so re-runs cost nothing."""
    EXTRACTIONS.mkdir(parents=True, exist_ok=True)
    cached = EXTRACTIONS / f"{name}.json"
    if cached.exists() and not refresh:
        return CovenantExtraction.model_validate_json(cached.read_text())
    print(f"  reading {name} with Claude...", flush=True)
    result, usage = extract(text)
    cached.write_text(result.model_dump_json(indent=2))
    (EXTRACTIONS / f"{name}.usage.json").write_text(json.dumps(usage, indent=2))
    print(f"  done: {usage['input_tokens']:,} tokens in, {usage['output_tokens']:,} out", flush=True)
    return result


def _documents(docs: list[str]) -> str:
    """The agreement and its amendments, oldest first, as one tagged text."""
    return combine([(u, edgar.fetch_text(u)) for u in docs])


def run_check(name: str, cik: str, docs: list[str], extraction: CovenantExtraction, source: str, quarters: int,
              reported: list[dict] | None = None) -> str:
    verified = verify_quotes(extraction, _documents(docs))
    fin = financials.load(cik)
    recent = fin.quarters[-quarters:]

    lines = [
        f"# {fin.name}: covenant check",
        "",
        f"- Credit agreement: {docs[0]}" + (f" (dated {extraction.agreement_date})" if extraction.agreement_date else ""),
        *[f"- Amendment {i}: {u}" for i, u in enumerate(docs[1:], 1)],
        f"- Covenants read by: {source}",
        "- Financials: SEC XBRL company facts, trailing twelve months at each quarter-end",
        "",
        "## Covenants found",
        "",
    ]
    for c in extraction.covenants:
        steps = ", ".join(f"{t.value:g}" + (f" from {t.applies_from}" if t.applies_from else "") for t in c.thresholds)
        lines += [
            f"**{c.name}** ({c.section}): {c.direction} {steps}; "
            + ("only tested when triggered" if c.test_frequency == "springing" else f"tested {c.test_frequency}"),
            f"- Quote found in agreement: {'yes' if verified.get(c.name) else 'NO, check by hand'}",
            f"- EBITDA add-backs used: {', '.join(c.ebitda_addbacks) or 'none'}"
            + (f"; cash netting capped at {_money(c.cash_netting_cap)}" if c.cash_netting_cap else "")
            + (f"; cash netting capped at {c.cash_netting_cap_pct_ebitda:.0%} of EBITDA"
               if c.cash_netting_cap_pct_ebitda else ""),
            f"> {c.quote}",
            "",
        ]
        if c.springing_condition:
            lines.insert(-1, f"- Only tested when: {c.springing_condition}")
    if not extraction.covenants:
        lines.append("No financial maintenance covenants found.\n")

    lines += [
        "## Quarter by quarter (estimates from public data)",
        "",
        "| Quarter end | Covenant | Value | Limit | Headroom | Status |",
        "|---|---|---|---|---|---|",
    ]
    signed = date.fromisoformat(extraction.agreement_date) if extraction.agreement_date else None
    for q in recent:
        for c in extraction.covenants:
            r = covenants.check(c, q)
            val = f"{r.value:.2f}x" if r.value is not None else "n/a"
            lim = f"{r.limit:g}x" if r.limit is not None else "n/a"
            head = f"{r.headroom:+.0%}" if r.headroom is not None else "n/a"
            status = r.status + (f" ({r.detail})" if r.detail else "")
            if signed and q.period_end < signed:
                status = "not tested: before this agreement was signed"
            lines.append(f"| {r.period_end} | {r.covenant} | {val} | {lim} | {head} | {status} |")

    said = edgar.compliance_statements(cik)
    lines += ["", "## What the company says", ""]
    if said and said["sentences"]:
        lines.append(f"From its {said['form']} filed {said['filed']} ({said['url']}):")
        lines += [f"> {s}" for s in said["sentences"]]
        lines.append("")
        lines.append("If the company reports compliance where the estimate shows a covenant over its limit, "
                     "the gap is in the EBITDA or debt estimate, not a breach.")
    else:
        lines.append("No covenant compliance statement found in its recent 10-Q or 10-K filings.")

    if reported:
        lines += _reported_section(extraction, fin, reported)

    lines += [
        "",
        "## Inputs and limits",
        "",
        "| Quarter end | Debt | Cash | LTM operating income | LTM D&A | LTM interest |",
        "|---|---|---|---|---|---|",
    ]
    for q in recent:
        lines.append(f"| {q.period_end} | {_money(q.debt)} | {_money(q.cash)} | {_money(q.ltm_operating_income)} | "
                     f"{_money(q.ltm_d_and_a)} | {_money(q.ltm_interest)} |")
    lines += [
        "",
        "XBRL tags used: " + "; ".join(f"{k}: {v}" for k, v in fin.sources.items()),
        "",
        "These are estimates from public filings. Covenant EBITDA in the agreement usually allows more "
        "adjustments than public numbers show (pro forma acquisitions, cost savings, one-off items), and "
        "covenant debt may count items like letters of credit differently. Treat a close call as a reason "
        "to look closer, not as a finding.",
        "",
    ]
    if extraction.notes:
        lines += ["## Notes from the agreement", "", extraction.notes, ""]
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"{name}.md").write_text("\n".join(lines))
    return "\n".join(lines)


def _reported_section(extraction: CovenantExtraction, fin: financials.CompanyFinancials, reported: list[dict]) -> list[str]:
    """Where the company publishes its real covenant ratios, show how far the public-data estimate lands."""
    by_metric = {c.metric: c for c in extraction.covenants}
    by_end = {q.period_end: q for q in fin.quarters}
    lines = ["", "## Estimate vs the company's reported ratios", "",
             "| Quarter end | Covenant | Company reported | Estimate | Gap |", "|---|---|---|---|---|"]
    gaps: dict[str, list[float]] = {}
    for r in sorted(reported, key=lambda r: (r["period_end"], r["metric"])):
        cov, q = by_metric.get(r["metric"]), by_end.get(date.fromisoformat(r["period_end"]))
        est = covenants.check(cov, q).value if cov and q else None
        if est is None:
            lines.append(f"| {r['period_end']} | {r['metric'].replace('_', ' ')} | {r['value']:.2f}x | n/a | n/a |")
            continue
        gap = est / r["value"] - 1
        gaps.setdefault(cov.name, []).append(gap)
        lines.append(f"| {r['period_end']} | {cov.name} | {r['value']:.2f}x | {est:.2f}x | {gap:+.0%} |")
    lines.append("")
    for cov_name, g in gaps.items():
        lines.append(f"- {cov_name}: estimate off by {sum(abs(x) for x in g) / len(g):.0%} on average "
                     f"over {len(g)} quarters (range {min(g):+.0%} to {max(g):+.0%}).")
    lines.append(f"- Source: the company's 10-Q and 10-K covenant tables, e.g. {reported[-1]['source']}")
    return lines


def cmd_find(args: argparse.Namespace) -> None:
    for r in edgar.search_exhibits(args.phrase, start=args.since):
        print(f"{r['filed']}  {r['exhibit']:8s}  {r['company'][:55]:55s}  cik={r['cik']}\n    {r['url']}")


def cmd_check(args: argparse.Namespace) -> None:
    if args.gold:
        g = json.loads(Path(args.gold).read_text())
        name, cik, docs = g["name"], g["cik"], g["docs"]
        extraction = CovenantExtraction.model_validate(g["extraction"])
        source = "hand labels (no AI)"
        reported = g.get("reported")
    else:
        reported = None
        name, cik, docs = args.name, args.cik, args.doc
        extraction = _ai_extraction(name, _documents(docs), refresh=args.refresh)
        source = "Claude (claude-opus-5-5), quotes verified in code"
    print(run_check(name, cik, docs, extraction, source, args.quarters, reported))


def _score(ai: CovenantExtraction, gold: CovenantExtraction, text: str) -> dict:
    """Field-by-field match of the AI's covenants against hand labels, matched on metric."""
    verified = verify_quotes(ai, text)
    by_metric = {c.metric: c for c in ai.covenants}
    fields = {"found": 0, "direction": 0, "thresholds": 0, "addbacks": 0, "quote_verified": 0}
    for g in gold.covenants:
        a = by_metric.get(g.metric)
        if not a:
            continue
        fields["found"] += 1
        fields["direction"] += a.direction == g.direction
        fields["thresholds"] += sorted(t.value for t in a.thresholds) == sorted(t.value for t in g.thresholds)
        fields["addbacks"] += set(a.ebitda_addbacks) - {"other"} == set(g.ebitda_addbacks) - {"other"}
        fields["quote_verified"] += bool(verified.get(a.name))
    extra = len([a for a in ai.covenants if a.metric not in {g.metric for g in gold.covenants}])
    return {"gold_covenants": len(gold.covenants), "extra_ai_covenants": extra, **fields}


def cmd_eval(args: argparse.Namespace) -> None:
    totals: dict[str, int] = {}
    for path in sorted(GOLD.glob("*.json")):
        g = json.loads(path.read_text())
        text = _documents(g["docs"])
        ai = _ai_extraction(g["name"], text, refresh=args.refresh)
        s = _score(ai, CovenantExtraction.model_validate(g["extraction"]), text)
        print(f"{g['name']:12s} {s}")
        for k, v in s.items():
            totals[k] = totals.get(k, 0) + v
    n = totals.get("gold_covenants", 0)
    if n:
        print("\nAcross all agreements (share of hand-labeled covenants):")
        for k in ("found", "direction", "thresholds", "addbacks", "quote_verified"):
            print(f"  {k:15s} {totals[k]}/{n} = {totals[k] / n:.0%}")
        print(f"  extra covenants the AI listed that aren't in the labels: {totals['extra_ai_covenants']}")


def cmd_build(args: argparse.Namespace) -> None:
    from . import pipeline as pl
    from .llm import OPUS

    today = date.today().isoformat()
    stages = ["discover", "screen", "collect", "chain", "read", "compute"] if args.stage == "all" else [args.stage]
    if "discover" in stages:
        pl.discover(args.since, today)
    if stages == ["discover"]:
        return
    if "screen" in stages:
        pl.screen(pl._load(pl.UNIVERSE / "candidates.json")[:args.screen])
    universe = pl._load(pl.UNIVERSE / "universe.json")
    if "screen" in stages or universe is None:
        universe = pl.select(pl._load(pl.UNIVERSE / "screened.json"), args.limit)
        print(f"universe: {len(universe)} borrowers")
    if "collect" in stages:
        pl.collect(universe, today)
    if "chain" in stages:
        for b in universe:
            ch = pl.chain(b)
            print(f"chain: {b['name'][:40]:40s} {len(ch['used'])} docs ({ch['source']})" +
                  (f"  notes: {'; '.join(ch['notes'])}" if ch["notes"] else ""))
    if "read" in stages or "compute" in stages:
        readings = pl.read(universe, OPUS)
        if "compute" in stages:
            pl.run_compute(universe, readings)


def cmd_compare(args: argparse.Namespace) -> None:
    from . import compare
    from . import pipeline as pl

    universe = pl._load(pl.UNIVERSE / "universe.json")
    results = compare.run(universe)
    names = pl._load(pl.UNIVERSE / "models.json")["borrowers"]
    md = compare.markdown(results, names)
    (ROOT / "evals" / "model_comparison.md").write_text(md)
    print(md)


def cmd_ask(args: argparse.Namespace) -> None:
    from . import ask

    qa = ask.ask(args.cik, args.question)
    print(qa["answer"], "\n")
    for c in qa["citations"]:
        print(f"[{c['n']}] {c['document']} {'(found in filing)' if c['found'] else '(NOT found)'}\n    \"{c['text'][:300]}\"")
    u = qa["usage"]
    print(f"\n{u['input_tokens']:,} input, {u['cache_creation_input_tokens']:,} written to cache, "
          f"{u['cache_read_input_tokens']:,} read from cache, {u['output_tokens']:,} output: ${qa['usd']:.3f}")
    if args.save:
        ask.save(args.cik, qa)


def cmd_site(args: argparse.Namespace) -> None:
    from . import pipeline as pl
    from . import site

    universe = pl._load(pl.UNIVERSE / "universe.json")
    results = [r for b in universe if (r := pl._load(pl.UNIVERSE / b["cik"] / "result.json"))]
    for r in results:
        r["qa"] = pl._load(pl.UNIVERSE / r["cik"] / "qa.json", [])
    models = pl._load(pl.UNIVERSE / "models.json")
    gold = _gold_summary()
    out = site.build(results, pl.summarize(results), models["models"] if models else None, args.model_note, gold, args.repo)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {len(results)} borrowers)")


def _gold_summary() -> dict | None:
    """Claude's cached production readings of the hand-labeled agreements, scored against the labels."""
    from . import pipeline as pl

    found = thresholds = addbacks = n = 0
    for path in sorted(GOLD.glob("*.json")):
        g = json.loads(path.read_text())
        ch = pl._load(pl.UNIVERSE / g["cik"] / "chain.json")
        if not ch:
            continue
        from .llm import cached
        job = pl.read_job({"cik": g["cik"], "name": g["name"]}, ch)
        out = cached("read", job)
        if not out:
            continue
        s = _score(out.parsed, CovenantExtraction.model_validate(g["extraction"]), pl.chain_text(ch))
        n += s["gold_covenants"]
        found += s["found"]
        thresholds += s["thresholds"]
        addbacks += s["addbacks"]
    return {"covenants": n, "found": f"{found}/{n}", "thresholds": f"{thresholds}/{n}", "addbacks": f"{addbacks}/{n}"} if n else None


def cmd_refresh(args: argparse.Namespace) -> None:
    """Pick up new filings for the current universe. Only new excerpts, covers and agreement chains cost anything."""
    from . import pipeline as pl
    from .llm import OPUS

    edgar.REFRESH = True
    universe = pl._load(pl.UNIVERSE / "universe.json")
    today = date.today().isoformat()
    pl.collect(universe, today)
    edgar.REFRESH = False
    for b in universe:
        pl.chain(b)
    pl.run_compute(universe, pl.read(universe, OPUS))


def cmd_costs(args: argparse.Namespace) -> None:
    from .llm import ledger_totals

    total = 0.0
    for k, t in sorted(ledger_totals().items()):
        total += t["usd"]
        print(f"{k:40s} {t['calls']:4d} calls  {t['input_tokens']:>11,} in  {t['output_tokens']:>9,} out  ${t['usd']:.2f}")
    print(f"{'total':40s} ${total:.2f}")


def main() -> None:
    p = argparse.ArgumentParser(prog="covenant_lens", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(required=True)

    f = sub.add_parser("find", help="search EDGAR exhibits for a phrase")
    f.add_argument("phrase")
    f.add_argument("--since", default="2024-01-01")
    f.set_defaults(func=cmd_find)

    c = sub.add_parser("check", help="extract covenants and test them quarter by quarter")
    c.add_argument("--gold", help="hand-labeled JSON to use instead of the AI")
    c.add_argument("--name")
    c.add_argument("--cik")
    c.add_argument("--doc", action="append",
                   help="EDGAR URL of the credit agreement; repeat for each amendment, oldest first")
    c.add_argument("--quarters", type=int, default=6)
    c.add_argument("--refresh", action="store_true", help="re-run the AI instead of using the cached extraction")
    c.set_defaults(func=cmd_check)

    e = sub.add_parser("eval", help="score the AI against every hand-labeled agreement in evals/gold")
    e.add_argument("--refresh", action="store_true")
    e.set_defaults(func=cmd_eval)

    b = sub.add_parser("build", help="run the pipeline over a universe of borrowers (see pipeline.py)")
    b.add_argument("--stage", default="all", choices=["all", "discover", "screen", "collect", "chain", "read", "compute"])
    b.add_argument("--since", default="2026-07-15", help="discovery window start for 10-Q/10-K filings")
    b.add_argument("--limit", type=int, default=40, help="most borrowers to keep after screening")
    b.add_argument("--screen", type=int, default=120, help="how many discovered companies to screen")
    b.set_defaults(func=cmd_build)

    m = sub.add_parser("compare", help="read the same agreements with Opus, Sonnet and Haiku and score each")
    m.set_defaults(func=cmd_compare)

    a = sub.add_parser("ask", help="ask a question about a borrower's agreement; the answer cites the contract")
    a.add_argument("cik")
    a.add_argument("question")
    a.add_argument("--save", action="store_true", help="keep the answer for the dashboard")
    a.set_defaults(func=cmd_ask)

    w = sub.add_parser("site", help="build the static dashboard into site/index.html")
    w.add_argument("--repo", default=None, help="link for the Code nav item")
    w.add_argument("--model-note", default=None)
    w.set_defaults(func=cmd_site)

    r = sub.add_parser("refresh", help="pick up new 10-Qs, releases and amendments for the current universe")
    r.set_defaults(func=cmd_refresh)

    k = sub.add_parser("costs", help="Claude spend so far, from data/ledger.jsonl")
    k.set_defaults(func=cmd_costs)

    args = p.parse_args()
    args.func(args)
