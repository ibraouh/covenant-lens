"""Build the static dashboard (site/index.html) from the pipeline's results. No server, no build tools:
one HTML file with the data embedded, so it can be hosted anywhere (GitHub Pages, Vercel, a subfolder of a site)."""

from __future__ import annotations

import json
import math
import shutil
from datetime import date
from pathlib import Path

from .edgar import ROOT
from .llm import LEDGER

SITE = ROOT / "site"
TEMPLATE = Path(__file__).with_name("site_template.html")
ASSETS = Path(__file__).with_name("site_assets")
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
        "stale": r.get("stale", False),
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


def _finite(x):
    """JSON has no NaN or Infinity; a ratio that can't be computed is null."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_finite(v) for v in x]
    return x


def _when(d: str) -> str:
    return date.fromisoformat(d).strftime("%b %-d, %Y")


def _example(results: list[dict]) -> dict | None:
    """The worked example at the top: Casella publishes its covenant figures, so every number is its own."""
    r = next((r for r in results if r["cik"] == "911177" and r["tightest"] and r["tightest"]["basis"] == "reported"), None)
    if not r:
        return None
    t = r["tightest"]
    key = next((a for a in r["accuracy"] if a["period_end"] == t["period_end"] and a["reported"] == t["value"]), None)
    return {"name": "Casella Waste Systems", "short": "Casella", "value": t["value"], "limit": t["limit"], "cushion": t["cushion"],
            "period_label": _when(t["period_end"]), "url": key["url"] if key else None, "form": "10-Q"}


def _findings(results: list[dict]) -> list[dict]:
    """Three things worth reading first, with their numbers taken from the data."""
    by = {r["cik"]: r for r in results}
    out = []
    n = by.get("71557")
    if n and n["tightest"] and n["tightest"]["value"] < n["tightest"]["limit"]:
        t = n["tightest"]
        out.append({"date": t["period_end"], "title": "A covenant miss, reported by the company",
                    "body": f"Nuvera Communications reported debt service coverage of {t['value']:.2f} at {_when(t['period_end'])}, "
                            f"below the {t['limit']:.2f} minimum in its loan agreement. "
                            "Its next quarterly report describes a new loan facility.",
                    "link": "b-71557", "link_text": "See Nuvera"})
    e = by.get("1023731")
    if e and e["tightest"] and e["tightest"]["basis"] == "adjusted":
        c = next(c for c in e["covenants"] if c["name"] == e["tightest"]["name"])
        row = next((x for x in reversed(c["rows"]) if x["xbrl"] is not None and x["period_end"] == e["tightest"]["period_end"]), None)
        if row:
            t = e["tightest"]
            out.append({"date": t["period_end"], "title": "An estimate that looked like a breach and wasn't",
                        "body": f"Rebuilt from standard accounting lines, 8x8's leverage came out at {row['xbrl']:.2f}x against a "
                                f"{t['limit']:.2f}x limit, while 8x8 said it was in compliance. Using the adjusted EBITDA 8x8 publishes "
                                f"gives {t['value']:.2f}x. How the contract defines EBITDA decides the answer.",
                        "link": "b-1023731", "link_text": "See 8x8"})
    if "1766368" in by:
        out.append({"date": "2026-11-17", "title": "A bug the grading caught",
                    "body": "Mayville Engineering's amended agreement shows deleted terms in strikethrough. The first version of the tool "
                            "read those deleted limits as if they still applied, and checking against the limits Mayville reports exposed "
                            "it. Deleted text is now removed before Claude reads a contract.",
                    "link": "b-1766368", "link_text": "See Mayville"})
    return out


def build(results: list[dict], summary: dict, models: list[dict] | None = None, model_note: str | None = None,
          gold: dict | None = None, repo: str | None = None) -> Path:
    data = {"summary": summary, "borrowers": [_slim(r) for r in results], "cost": read_cost(),
            "models": models or [], "model_note": model_note, "gold": gold, "repo": repo,
            "example": _example(results), "findings": _findings(results)}
    blob = json.dumps(_finite(data), default=str, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("/*DATA*/", blob)
    SITE.mkdir(exist_ok=True)
    for asset in ASSETS.iterdir():  # favicon, touch icon, share card
        shutil.copy(asset, SITE / asset.name)
    out = SITE / "index.html"
    out.write_text(html)
    return out
