"""Small SEC EDGAR client: full-text search, document download, XBRL company facts.

Everything is cached under data/cache so re-runs don't hit the SEC again.
SEC asks automated clients to identify themselves and stay under 10 requests/second.
Set SEC_USER_AGENT to your own "Name contact" string if the SEC ever rejects requests.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> None:
    """Read KEY=value lines from the project's .env into the environment (no override)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()
USER_AGENT = os.environ.get("SEC_USER_AGENT", "CovenantLens/0.1 personal research project")
CACHE_DIR = ROOT / "data" / "cache"
REFRESH = False  # when True, searches and filing histories skip the cache (set by `refresh`)
_MIN_INTERVAL = 0.15  # seconds between live requests, well under the SEC's 10/s limit
_last_request = 0.0


def _get(url: str, fresh: bool = False) -> bytes:
    """GET a URL with caching (gzip on disk) and polite pacing. `fresh` skips the cache, for searches that change."""
    global _last_request
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / hashlib.sha256(url.encode()).hexdigest()
    if cached.exists() and not fresh:
        body = cached.read_bytes()
        return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
    for attempt in range(4):
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
            break
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503) or attempt == 3:
                raise
            time.sleep(2 ** attempt)
        finally:
            _last_request = time.monotonic()
    cached.write_bytes(gzip.compress(body))
    return body


def search(q: str, forms: str, start: str, end: str, ciks: str | None = None, fresh: bool = False,
           max_pages: int = 5) -> list[dict]:
    """EDGAR full-text search. Returns raw hits (each has `_id` = "accession:filename" and `_source`)."""
    hits: list[dict] = []
    for page in range(max_pages):
        params = {"q": q, "forms": forms, "dateRange": "custom", "startdt": start, "enddt": end, "from": page * 100}
        if ciks:
            params["ciks"] = f"{int(ciks):010d}"
        data = json.loads(_get(f"https://efts.sec.gov/LATEST/search-index?{urllib.parse.urlencode(params)}", fresh or REFRESH))
        batch = data.get("hits", {}).get("hits", [])
        hits += batch
        if len(batch) < 100:
            break
    return hits


def hit_url(hit: dict) -> str:
    accession, filename = hit["_id"].split(":", 1)
    return document_url(hit["_source"]["ciks"][0], accession, filename)


def submissions(cik: str, fresh: bool = False) -> dict:
    """The company's filing history (recent filings: form, dates, accession, primary document)."""
    return json.loads(_get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", fresh or REFRESH))


def periodic_reports(cik: str, n: int = 4, fresh: bool = False) -> list[dict]:
    """Latest n 10-Q/10-K filings, newest first: form, filed, period, url."""
    r = submissions(cik, fresh)["filings"]["recent"]
    out = []
    for form, filed, period, acc, doc in zip(r["form"], r["filingDate"], r["reportDate"], r["accessionNumber"],
                                             r["primaryDocument"], strict=True):
        if form in ("10-Q", "10-K") and doc:
            out.append({"form": form, "filed": filed, "period": period, "url": document_url(cik, acc, doc)})
    return sorted(out, key=lambda f: f["period"], reverse=True)[:n]


def windows(text: str, pattern: str, before: int = 1200, after: int = 1200, limit: int = 16000,
            must: str | None = None) -> str:
    """Excerpts around every match of `pattern`, merged where they overlap, optionally kept only if they
    also match `must`, capped at `limit` characters. Used to send a model only the parts of a filing it needs."""
    import re

    spans: list[list[int]] = []
    for m in re.finditer(pattern, text, flags=re.I):
        a, b = max(0, m.start() - before), min(len(text), m.end() + after)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    parts, total = [], 0
    for a, b in spans:
        chunk = text[a:b]
        if must and not re.search(must, chunk, flags=re.I):
            continue
        if total + len(chunk) > limit:
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n[...]\n".join(parts)


def search_exhibits(phrase: str, forms: str = "8-K", start: str = "2024-01-01", end: str = "2026-12-31") -> list[dict]:
    """Full-text search across EDGAR filings. Returns one dict per matching document."""
    params = urllib.parse.urlencode(
        {"q": f'"{phrase}"', "forms": forms, "dateRange": "custom", "startdt": start, "enddt": end}
    )
    data = json.loads(_get(f"https://efts.sec.gov/LATEST/search-index?{params}"))
    results = []
    for hit in data.get("hits", {}).get("hits", []):
        src = hit["_source"]
        accession, filename = hit["_id"].split(":", 1)
        cik = src["ciks"][0].lstrip("0")
        results.append(
            {
                "company": (src.get("display_names") or ["?"])[0],
                "cik": cik,
                "filed": src.get("file_date"),
                "exhibit": src.get("file_type"),
                "url": document_url(cik, accession, filename),
            }
        )
    return results


def document_url(cik: str, accession: str, filename: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{filename}"


def fetch_text(url: str) -> str:
    """Download a filing document and return plain text (HTML tags stripped). The text is cached too."""
    tcache = CACHE_DIR / "text" / (hashlib.sha256(url.encode()).hexdigest() + ".txt.gz")
    if tcache.exists():
        return gzip.decompress(tcache.read_bytes()).decode()
    from bs4 import BeautifulSoup

    raw = _get(url)
    if url.lower().endswith(".txt"):
        text = raw.decode("utf-8", "replace")
    else:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text("\n")
    lines = (" ".join(line.split()) for line in text.splitlines())
    text = "\n".join(line for line in lines if line)
    tcache.parent.mkdir(parents=True, exist_ok=True)
    tcache.write_bytes(gzip.compress(text.encode()))
    return text


def compliance_statements(cik: str, since: str = "2025-01-01") -> dict | None:
    """What the company itself says about covenant compliance in its most recent 10-Q or 10-K."""
    import re

    params = urllib.parse.urlencode(
        {"q": '"in compliance with" covenants', "forms": "10-Q,10-K", "ciks": f"{int(cik):010d}",
         "dateRange": "custom", "startdt": since, "enddt": time.strftime("%Y-%m-%d")}
    )
    hits = json.loads(_get(f"https://efts.sec.gov/LATEST/search-index?{params}")).get("hits", {}).get("hits", [])
    if not hits:
        return None
    latest = max(hits, key=lambda h: h["_source"]["file_date"])
    accession, filename = latest["_id"].split(":", 1)
    url = document_url(cik, accession, filename)
    text = " ".join(fetch_text(url).split())
    sentences = re.findall(r"[^.]*\bin compliance with\b[^.]*\bcovenants?\b[^.]*\.", text, flags=re.I)
    return {
        "filed": latest["_source"]["file_date"],
        "form": latest["_source"]["form"],
        "url": url,
        "sentences": [s.strip() for s in dict.fromkeys(sentences)][:4],
    }


def company_facts(cik: str) -> dict:
    """XBRL financial data the company has reported in its 10-Q and 10-K filings."""
    return json.loads(_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json", REFRESH))
