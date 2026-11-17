"""Run Covenant Lens across a universe of borrowers, end to end.

  discover  search EDGAR for companies whose 10-Qs state actual covenant ratios
  screen    read the latest 10-Q excerpt of each and keep the ones with usable answer keys
  collect   for each borrower: 4 quarters of reported ratios, 5 earnings releases, every loan exhibit's cover page
  chain     resolve each borrower's current agreement and its amendments
  read      Claude reads each agreement chain once: covenants plus six document terms
  compute   test every covenant, every quarter, three ways (XBRL estimate, adjusted-EBITDA estimate, reported)

Every stage caches to disk, so a re-run only does new work. Bulk Claude calls go through the Batch API.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import traceback
from datetime import date, timedelta
from pathlib import Path

from . import agreements, covenants, disclosures, edgar, financials
from .extract import AgreementReading, CovenantExtraction, combine, reading_job, verify_quotes, verify_terms
from .llm import OPUS, Outcome, cached, run_batch

ROOT = edgar.ROOT
UNIVERSE = ROOT / "data" / "universe"
GOLD = ROOT / "evals" / "gold"
COMPUTABLE = {"net_leverage", "total_leverage", "interest_coverage"}
EXCLUDED_SIC = range(6000, 6800)  # banks, insurers, REITs, funds: "leverage" means something else there
DISCOVERY_QUERIES = [
    '"net leverage ratio was"',
    '"leverage ratio was"',
    '"interest coverage ratio was"',
    '"coverage ratio was"',
    '"covenant requirement" "leverage ratio"',
    '"maximum consolidated net leverage ratio"',
    '"actual" "required" "leverage ratio" "credit agreement"',
]


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:10]


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str))


def _load(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def _near(d: date, options, days: int = 10):
    """The option date closest to d, within `days`."""
    best = min(options, key=lambda o: abs((o - d).days), default=None)
    return best if best is not None and abs((best - d).days) <= days else None


# ---------------------------------------------------------------- discover and screen

def discover(since: str, until: str, log=print) -> list[dict]:
    found: dict[str, dict] = {}
    for q in DISCOVERY_QUERIES:
        for h in edgar.search(q, forms="10-Q,10-K", start=since, end=until, max_pages=3):
            src = h["_source"]
            sic = int((src.get("sics") or ["0"])[0] or 0)
            if not sic or sic in EXCLUDED_SIC or src.get("file_type") not in ("10-Q", "10-K"):
                continue
            cik = src["ciks"][0].lstrip("0")
            name = src["display_names"][0].split("  (")[0].strip()
            c = found.setdefault(cik, {"cik": cik, "name": name, "sic": sic, "queries": 0})
            c["queries"] += 1
    cands = sorted(found.values(), key=lambda c: -c["queries"])
    _save(UNIVERSE / "candidates.json", cands)
    log(f"discover: {len(cands)} non-financial companies mention covenant ratios in recent 10-Qs")
    return cands


def _ratio_jobs(b: dict, n: int) -> list[tuple[dict, str, object]]:
    out = []
    for f in edgar.periodic_reports(b["cik"], n=n):
        excerpt = disclosures.ratio_excerpt(edgar.fetch_text(f["url"]))
        if excerpt:
            out.append((f, excerpt, disclosures.ratio_job(f"ratio-{b['cik']}-{f['period']}", b["name"], f, excerpt)))
    return out


def screen(cands: list[dict], log=print) -> list[dict]:
    work = []
    for i, b in enumerate(cands):
        try:
            work += [(b, *w) for w in _ratio_jobs(b, n=1)]
        except Exception as e:  # one bad filing shouldn't stop the run
            log(f"screen: skipped {b['name']}: {e}")
        if i % 20 == 19:
            log(f"screen: fetched {i + 1}/{len(cands)}")
    res = run_batch("excerpts", [w[3] for w in work], log=log)
    screened = []
    for b, _filing, excerpt, job in work:
        out = res.get(job.id)
        if not out or not out.parsed:
            continue
        kept, _ = disclosures.verified_ratios(out.parsed, excerpt)
        usable = [r for r in kept if r["actual"] is not None and r["limit"] is not None and r["metric"] in COMPUTABLE]
        screened.append(b | {"usable": len(usable), "metrics": sorted({r["metric"] for r in kept})})
    _save(UNIVERSE / "screened.json", screened)
    log(f"screen: {sum(1 for s in screened if s['usable'])} of {len(screened)} state an actual ratio and its limit")
    return screened


def gold_borrowers() -> list[dict]:
    return [{"cik": g["cik"], "name": g["name"], "gold": True, "docs": g["docs"]}
            for g in (json.loads(p.read_text()) for p in sorted(GOLD.glob("*.json")))]


def select(screened: list[dict], limit: int) -> list[dict]:
    picked = sorted((s for s in screened if s["usable"]), key=lambda s: (-s["usable"], -s["queries"]))[:limit]
    universe = gold_borrowers()
    have = {b["cik"] for b in universe}
    universe += [{"cik": s["cik"], "name": s["name"], "sic": s["sic"]} for s in picked if s["cik"] not in have]
    _save(UNIVERSE / "universe.json", universe)
    return universe


# ---------------------------------------------------------------- collect

def collect(universe: list[dict], until: str, log=print) -> None:
    jobs = []
    for i, b in enumerate(universe):
        d = UNIVERSE / b["cik"]
        try:
            col = {"reports": [], "releases": [], "exhibits": []}
            for f, _, job in _ratio_jobs(b, n=4):
                col["reports"].append(f | {"job": job.id})
                jobs.append(job)
            since = (date.fromisoformat(until) - timedelta(days=560)).isoformat()
            for r in disclosures.earnings_releases(b["cik"], since, until)[:5]:
                excerpt = disclosures.ebitda_excerpt(edgar.fetch_text(r["url"]))
                if excerpt:
                    job = disclosures.ebitda_job(f"ebitda-{b['cik']}-{_h(r['url'])}", b["name"], r, excerpt)
                    col["releases"].append(r | {"job": job.id})
                    jobs.append(job)
            for c in agreements.candidates(b["cik"], until)[-25:]:
                text = edgar.fetch_text(c["url"])
                job = agreements.cover_job(f"cover-{b['cik']}-{_h(c['url'])}", text, {"url": c["url"]})
                col["exhibits"].append(c | {"chars": len(text), "job": job.id})
                jobs.append(job)
            _save(d / "collected.json", col)
        except Exception as e:
            log(f"collect: {b['name']} failed: {e}")
        log(f"collect: {i + 1}/{len(universe)} {b['name']}")
    run_batch("excerpts", jobs, log=log)


# ---------------------------------------------------------------- agreement chains

def chain(b: dict) -> dict:
    col = _load(UNIVERSE / b["cik"] / "collected.json", {"exhibits": []})
    docs = []
    for e in col["exhibits"]:
        out = _cached_outcome("excerpts", e["job"], agreements.Cover)
        if out and out.parsed:
            docs.append(e | {"cover": out.parsed.model_dump()})
    auto, notes = agreements.resolve(docs)
    found = {"auto": [{"url": d["url"], "title": d["cover"]["title"], "dated": d["cover"]["dated"],
                       "kind": d["cover"]["kind"], "filed": d["filed"]} for d in auto], "notes": notes}
    if b.get("docs"):  # hand-picked chain for gold borrowers; the automatic one is kept for comparison
        by_url = {d["url"]: d for d in docs}
        found["used"] = [{"url": u, "title": by_url.get(u, {}).get("cover", {}).get("title", "hand-picked"),
                          "dated": by_url.get(u, {}).get("cover", {}).get("dated"), "kind": "hand-picked"}
                         for u in b["docs"]]
        found["source"] = "hand-picked"
    else:
        found["used"] = found["auto"]
        found["source"] = "automatic"
    _save(UNIVERSE / b["cik"] / "chain.json", found)
    return found


def rescue_chain(b: dict, until: str, log=print) -> dict:
    """When amendments point at a base agreement older than the search window, go back and get it.

    The amendments say which agreement they amend ("amends_dated"); search EDGAR from that date for the
    original and any amendments missed, read their cover pages, and resolve the chain again.
    """
    from collections import Counter

    from .llm import run_one

    path = UNIVERSE / b["cik"] / "collected.json"
    col = _load(path, {"exhibits": []})
    targets = Counter()
    for e in col["exhibits"]:
        out = _cached_outcome("excerpts", e["job"], agreements.Cover)
        if out and out.parsed and out.parsed.kind == "amendment" and out.parsed.amends_dated:
            targets[out.parsed.amends_dated] += 1
    if not targets:
        return chain(b)
    base_date = date.fromisoformat(targets.most_common(1)[0][0])
    have = {e["url"] for e in col["exhibits"]}
    hits = edgar.search('"credit agreement"', forms="8-K,10-Q,10-K", start=(base_date - timedelta(days=7)).isoformat(),
                        end=until, ciks=b["cik"], max_pages=5)
    added = 0
    for h in hits:
        src, url = h["_source"], edgar.hit_url(h)
        if url in have or not src.get("file_type", "").startswith(("EX-10", "EX-4")):
            continue
        if not url.lower().endswith((".htm", ".html", ".txt")):
            continue
        have.add(url)
        text = edgar.fetch_text(url)
        job = agreements.cover_job(f"cover-{b['cik']}-{_h(url)}", text, {"url": url})
        run_one("excerpts", job)
        col["exhibits"].append({"url": url, "filed": src["file_date"], "exhibit": src["file_type"], "chars": len(text),
                                "job": job.id})
        added += 1
    _save(path, col)
    ch = chain(b)
    log(f"rescue: {b['name']}: looked back to {base_date}, {added} more exhibits, chain now {len(ch['used'])} docs")
    return ch


def _cached_outcome(stage: str, job_id: str, schema) -> Outcome | None:
    class _J:  # the cache only needs the id and schema
        pass
    j = _J()
    j.id, j.schema = job_id, schema
    return cached(stage, j)  # type: ignore[arg-type]


def chain_text(ch: dict) -> str:
    return combine([(d["url"], edgar.fetch_text(d["url"])) for d in ch["used"]])


def read_job(b: dict, ch: dict, model: str = OPUS, legacy: bool = False):
    """Job ids depend on the documents read; chains with deleted (struck-through) text got a new id when that text
    started being removed, so only those are read again. `legacy` gives the id from before that fix."""
    urls = "|".join(d["url"] for d in ch["used"])
    if not legacy and any(edgar.has_deleted_text(d["url"]) for d in ch["used"]):
        urls += edgar.TEXT_VERSION
    key = _h(urls)
    short = model.replace("claude-", "").replace("-", "")
    return reading_job(f"read-{b['cik']}-{key}-{short}", chain_text(ch), model=model,
                       meta={"cik": b["cik"], "docs": [d["url"] for d in ch["used"]]})


def read(universe: list[dict], model: str = OPUS, log=print) -> dict[str, Outcome]:
    jobs = {}
    for b in universe:
        ch = _load(UNIVERSE / b["cik"] / "chain.json")
        if ch and ch["used"]:
            jobs[b["cik"]] = read_job(b, ch, model)
    res = run_batch("read", list(jobs.values()), log=log)
    out = {cik: res[j.id] for cik, j in jobs.items() if j.id in res}
    for b in universe:  # not read yet with the current text: fall back to an earlier reading, flagged as stale
        if b["cik"] in jobs and b["cik"] not in out:
            old = cached("read", read_job(b, _load(UNIVERSE / b["cik"] / "chain.json"), model, legacy=True))
            if old:
                old.error = "stale"
                out[b["cik"]] = old
    return out


# ---------------------------------------------------------------- compute

def _answer_keys(cik: str) -> tuple[dict, list[dict]]:
    """Reported (metric, period_end) -> ratio, newest filing wins; plus what was dropped and why."""
    col = _load(UNIVERSE / cik / "collected.json", {"reports": []})
    keys, dropped = {}, []
    for f in sorted(col["reports"], key=lambda f: f["period"]):
        out = _cached_outcome("excerpts", f["job"], disclosures.ReportedRatios)
        if not out or not out.parsed:
            continue
        excerpt = disclosures.ratio_excerpt(edgar.fetch_text(f["url"]))
        kept, bad = disclosures.verified_ratios(out.parsed, excerpt)
        dropped += [d | {"url": f["url"]} for d in bad]
        for r in kept:
            keys[(r["metric"], r["period_end"])] = r | {"url": f["url"], "form": f["form"], "filed": f["filed"]}
    return keys, dropped


def _adjusted_ebitda(cik: str) -> dict[date, dict]:
    col = _load(UNIVERSE / cik / "collected.json", {"releases": []})
    series: dict[date, dict] = {}
    for r in sorted(col["releases"], key=lambda r: r["filed"]):
        out = _cached_outcome("excerpts", r["job"], disclosures.EbitdaRelease)
        if not out or not out.parsed:
            continue
        excerpt = disclosures.ebitda_excerpt(edgar.fetch_text(r["url"]))
        for f in disclosures.verified_ebitda(out.parsed, excerpt):
            series[date.fromisoformat(f["period_end"])] = f | {"url": r["url"], "label": out.parsed.label}
    return series


def _ltm_adjusted(fin: financials.CompanyFinancials, i: int, adj: dict[date, dict]) -> float | None:
    if i < 3:
        return None
    vals = []
    for q in fin.quarters[i - 3:i + 1]:
        k = _near(q.period_end, adj.keys())
        if k is None:
            return None
        vals.append(adj[k]["value"])
    return sum(vals)


def compute(b: dict, reading: AgreementReading | CovenantExtraction, n_quarters: int = 8, stale: bool = False) -> dict:
    cik = b["cik"]
    fin = financials.load(cik)
    ch = _load(UNIVERSE / cik / "chain.json")
    text = chain_text(ch)
    quotes_ok = verify_quotes(reading, text)
    terms_ok = verify_terms(reading, text) if isinstance(reading, AgreementReading) else {}
    keys, dropped = _answer_keys(cik)
    adj = _adjusted_ebitda(cik)
    start = max(0, len(fin.quarters) - n_quarters)
    signed = date.fromisoformat(reading.agreement_date) if reading.agreement_date else None

    covs, accuracy = [], []
    for cov in reading.covenants:
        rows = []
        for i in range(start, len(fin.quarters)):
            q = fin.quarters[i]
            xb = covenants.check(cov, q)
            adj_e = _ltm_adjusted(fin, i, adj)
            ad = covenants.check(cov, q, ebitda=adj_e) if adj_e else None
            rep = _reported_for(cov, q.period_end, keys)
            rows.append({
                "period_end": q.period_end.isoformat(),
                "before_agreement": bool(signed and q.period_end < signed),
                "limit": xb.limit,
                "xbrl": xb.value, "adjusted": ad.value if ad else None,
                "reported": rep["actual"] if rep else None, "reported_limit": rep["limit"] if rep else None,
                "reported_quote": rep["quote"] if rep else None, "reported_url": rep["url"] if rep else None,
                "ltm_ebitda_xbrl": q.ebitda(set(cov.ebitda_addbacks)), "ltm_ebitda_adjusted": adj_e,
                "debt": q.debt, "cash": q.cash, "ltm_interest": q.ltm_interest,
            })
        latest = _latest(cov, rows)
        steps = covenants.upcoming_steps(cov, date.fromisoformat(rows[-1]["period_end"])) if rows else []
        covs.append({
            "name": cov.name, "metric": cov.metric, "direction": cov.direction, "section": cov.section,
            "quote": cov.quote, "quote_verified": bool(quotes_ok.get(cov.name)),
            "thresholds": [t.model_dump() for t in cov.thresholds], "test_frequency": cov.test_frequency,
            "springing_condition": cov.springing_condition, "addbacks": cov.ebitda_addbacks,
            "cash_netting_cap": cov.cash_netting_cap, "cash_netting_cap_pct_ebitda": cov.cash_netting_cap_pct_ebitda,
            "rows": rows, "latest": latest,
            "steps": [{"from": d.isoformat(), "limit": v,
                       "headroom_at_today": covenants.headroom(latest["value"], v, cov.direction) if latest else None}
                      for d, v in steps],
        })
    since = current_since(ch)
    for (metric, period), r in sorted(keys.items(), key=lambda kv: kv[0][1]):
        accuracy.append(_grade(reading, covs, metric, period, r, since))

    tight = [c for c in covs if c["latest"] and c["test_frequency"] != "springing"]
    tightest = min(tight, key=lambda c: c["latest"]["headroom"], default=None)
    result = {
        "cik": cik, "name": b["name"], "borrower": reading.borrower, "gold": bool(b.get("gold")), "stale": stale,
        "agreement_date": reading.agreement_date, "facility": getattr(reading, "facility", None),
        "chain": ch, "covenants": covs, "notes": reading.notes,
        "terms": [t.model_dump() | {"quote_verified": terms_ok.get(t.kind)} for t in getattr(reading, "terms", [])],
        "addback_cap_pct": getattr(reading, "addback_cap_pct", None),
        "cov_lite": not reading.covenants or all(c.test_frequency == "springing" for c in reading.covenants),
        "tightest": {"name": tightest["name"], **tightest["latest"]} if tightest else None,
        "accuracy": accuracy, "dropped_answer_keys": dropped,
        "adjusted_ebitda": [{"period_end": k.isoformat(), **v} for k, v in sorted(adj.items())],
        "financials_sources": fin.sources,
        "compliance": _compliance(cik),
    }
    _save(UNIVERSE / cik / "result.json", result)
    return result


def _reported_for(cov, period: date, keys: dict) -> dict | None:
    """The company's own figure for this covenant at this quarter: same metric if reported, else same family
    (a 10-Q's "total leverage ratio" is often the contract's net ratio), preferring a matching limit."""
    near = [(k, r) for k, r in keys.items() if _near(period, [date.fromisoformat(k[1])])]
    exact = [r for (m, _), r in near if m == cov.metric]
    if exact:
        return exact[0]
    family = [r for (m, _), r in near if covenants.FAMILY[m] == covenants.FAMILY[cov.metric]]
    lim = covenants.limit_for(cov, period)
    family.sort(key=lambda r: abs((r["limit"] or 1e12) - (lim or 0)))
    return family[0] if family else None


def _latest(cov, rows: list[dict]) -> dict | None:
    """The newest quarter with a usable number: the company's own figure first, then the better estimate."""
    for r in reversed(rows):
        if r["before_agreement"]:
            continue
        limit = r["reported_limit"] or r["limit"]
        for basis in ("reported", "adjusted", "xbrl"):
            v = r[basis]
            if v is not None and limit:
                return {"period_end": r["period_end"], "basis": basis, "value": v, "limit": limit,
                        "headroom": covenants.headroom(v, limit, cov.direction),
                        "cushion": covenants.ebitda_cushion(v, limit, cov.direction)}
    return None


def current_since(ch: dict) -> date | None:
    """Date of the latest document in the chain: the terms Claude read apply from here on."""
    dates = [date.fromisoformat(d["dated"]) for d in ch.get("used", []) if d.get("dated")]
    return max(dates) if dates else None


def _grade(reading, covs: list[dict], metric: str, period: str, r: dict, since: date | None = None) -> dict:
    """Score one answer key: did Claude read the right limit, and how close is each estimate?"""
    p = date.fromisoformat(period)
    cov = covenants.match(reading.covenants, metric, r["limit"], p)
    row = None
    if cov:
        c = next(c for c in covs if c["name"] == cov.name)
        row = next((x for x in c["rows"] if _near(date.fromisoformat(x["period_end"]), [p])), None)
    ours = covenants.limit_for(cov, p) if cov else None
    skip = covenants.gradable(metric, r["limit"], p, since)
    err = lambda est: (est / r["actual"] - 1) if (est is not None and r["actual"]) else None  # noqa: E731
    return {
        "metric": metric, "period_end": period, "reported": r["actual"], "reported_limit": r["limit"],
        "covenant_found": cov is not None, "matched": cov.name if cov else None, "our_limit": ours,
        "limit_match": None if skip else (ours is not None and covenants.same_limit(ours, r["limit"])),
        "not_graded": skip,
        "xbrl": row["xbrl"] if row else None, "adjusted": row["adjusted"] if row else None,
        "xbrl_error": err(row["xbrl"]) if row and FAMILY_COMPUTABLE(metric) else None,
        "adjusted_error": err(row["adjusted"]) if row and FAMILY_COMPUTABLE(metric) else None,
        "facility": r.get("facility"), "quote": r["quote"], "url": r["url"],
    }


def FAMILY_COMPUTABLE(metric: str) -> bool:  # noqa: N802
    """Estimates exist only for leverage and interest coverage; compare like with like."""
    return covenants.FAMILY[metric] in ("leverage", "interest")


def _compliance(cik: str) -> dict | None:
    try:
        return edgar.compliance_statements(cik)
    except Exception:
        return None


# ---------------------------------------------------------------- summary

def summarize(results: list[dict]) -> dict:
    acc = [a for r in results for a in r["accuracy"]]
    limits = [a for a in acc if a["limit_match"] is not None]
    def med(xs):
        xs = [abs(x) for x in xs if x is not None]
        return statistics.median(xs) if xs else None
    def share_within(xs, tol):
        xs = [abs(x) for x in xs if x is not None]
        return sum(x <= tol for x in xs) / len(xs) if xs else None
    paired = [a for a in acc if a["xbrl_error"] is not None and a["adjusted_error"] is not None]
    quotes = [c["quote_verified"] for r in results for c in r["covenants"]]
    terms = [t for r in results for t in r["terms"] if t["present"] != "no"]
    return {
        "borrowers": len(results),
        "covenants": len(quotes),
        "quotes_verified": sum(quotes),
        "answer_keys": len(acc),
        "borrowers_with_answer_keys": len({r["cik"] for r in results if r["accuracy"]}),
        "limits_checked": len(limits),
        "limits_matched": sum(a["limit_match"] for a in limits),
        "covenant_found": sum(a["covenant_found"] for a in acc),
        "xbrl_points": sum(a["xbrl_error"] is not None for a in acc),
        "xbrl_median_error": med(a["xbrl_error"] for a in acc),
        "xbrl_within_10pct": share_within([a["xbrl_error"] for a in acc], 0.10),
        "adjusted_points": sum(a["adjusted_error"] is not None for a in acc),
        "adjusted_median_error": med(a["adjusted_error"] for a in acc),
        "adjusted_within_10pct": share_within([a["adjusted_error"] for a in acc], 0.10),
        "paired_points": len(paired),
        "paired_xbrl_median": med(a["xbrl_error"] for a in paired),
        "paired_adjusted_median": med(a["adjusted_error"] for a in paired),
        "term_quotes": len(terms),
        "term_quotes_verified": sum(bool(t["quote_verified"]) for t in terms),
        "generated": date.today().isoformat(),
    }


def run_compute(universe: list[dict], readings: dict[str, Outcome], log=print) -> list[dict]:
    results = []
    for b in universe:
        out = readings.get(b["cik"])
        if not out or not out.parsed:
            log(f"compute: no reading for {b['name']}" + (f" ({out.error})" if out and out.error else ""))
            (UNIVERSE / b["cik"] / "result.json").unlink(missing_ok=True)  # never show a result for an outdated chain
            continue
        if out.error == "stale":
            log(f"compute: {b['name']} uses an earlier reading; its contract has deleted text and needs a fresh read")
        try:
            results.append(compute(b, out.parsed, stale=out.error == "stale"))
        except Exception as e:
            log(f"compute: {b['name']} failed: {e}\n{traceback.format_exc(limit=2)}")
    summary = summarize(results)
    _save(UNIVERSE / "summary.json", summary)
    log("summary: " + json.dumps(summary))
    return results
