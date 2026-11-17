"""Build the static dashboard (site/index.html) from the pipeline's results. No server, no build tools:
one HTML file with the data embedded, so it can be hosted anywhere (GitHub Pages, Vercel, a subfolder of a site)."""

from __future__ import annotations

import json
from pathlib import Path

from .edgar import ROOT
from .llm import LEDGER

SITE = ROOT / "site"
TEMPLATE = Path(__file__).with_name("site_template.html")
ROW_KEYS = ("period_end", "before_agreement", "limit", "xbrl", "adjusted", "reported", "reported_limit",
            "ltm_ebitda_xbrl", "ltm_ebitda_adjusted", "debt", "cash")


def _slim(r: dict) -> dict:
    """Only what the page shows, so the embedded data stays small."""
    covs = []
    for c in r["covenants"]:
        covs.append({k: c[k] for k in ("name", "metric", "direction", "section", "quote", "quote_verified", "thresholds",
                                       "test_frequency", "springing_condition", "cash_netting_cap",
                                       "cash_netting_cap_pct_ebitda", "latest", "steps")}
                    | {"rows": [{k: row.get(k) for k in ROW_KEYS} for row in c["rows"]]})
    acc = [{k: a.get(k) for k in ("metric", "period_end", "reported", "reported_limit", "our_limit", "limit_match",
                                  "xbrl", "adjusted", "xbrl_error", "adjusted_error", "url")} for a in r["accuracy"]]
    comp = r.get("compliance") or {}
    return {
        "cik": r["cik"], "name": r["name"], "borrower": r["borrower"], "gold": r["gold"], "facility": r["facility"],
        "agreement_date": r["agreement_date"], "cov_lite": r["cov_lite"], "tightest": r["tightest"],
        "chain": {"source": r["chain"]["source"], "notes": r["chain"]["notes"],
                  "used": [{k: d.get(k) for k in ("url", "title", "dated")} for d in r["chain"]["used"]]},
        "covenants": covs, "terms": r["terms"], "accuracy": acc, "notes": r["notes"],
        "compliance": {k: comp.get(k) for k in ("form", "filed", "url", "sentences")} if comp else None,
        "qa": [{"question": q["question"], "answer": q["answer"],
                "citations": [{k: c[k] for k in ("n", "document", "url", "text", "found")} for c in q["citations"]]}
               for q in r.get("qa", [])],
    }


def read_cost() -> dict | None:
    """What the production reading pass (Opus, stage 'read') cost, from the ledger."""
    if not LEDGER.exists():
        return None
    rows = [json.loads(line) for line in LEDGER.read_text().splitlines()]
    reads = [r for r in rows if r["stage"] == "read" and r["model"] == "claude-opus-5-5"]
    if not reads:
        return None
    latest = {}
    for r in reads:
        latest[r["job"]] = r  # one entry per agreement chain
    return {"read": sum(r["usd"] for r in latest.values()), "agreements": len(latest),
            "tokens": sum(r["input_tokens"] + r["output_tokens"] for r in latest.values()),
            "all_stages": sum(r["usd"] for r in rows)}


def build(results: list[dict], summary: dict, models: list[dict] | None = None, model_note: str | None = None,
          gold: dict | None = None, repo: str | None = None) -> Path:
    data = {"summary": summary, "borrowers": [_slim(r) for r in results], "cost": read_cost(),
            "models": models or [], "model_note": model_note, "gold": gold, "repo": repo}
    blob = json.dumps(data, default=str, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("/*DATA*/", blob)
    SITE.mkdir(exist_ok=True)
    out = SITE / "index.html"
    out.write_text(html)
    return out
