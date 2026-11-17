"""Ask a question about a borrower's credit agreement and get an answer that cites the contract.

The agreement chain goes in as document blocks with citations turned on, so every claim in the answer points
at the exact passage it rests on (the API returns the cited text, which is checked against the filing). The
documents are marked for prompt caching: the first question pays to read the contract, and follow-up
questions within five minutes read it from cache at a twentieth of the input price.
"""

from __future__ import annotations

import json

import anthropic

from . import edgar
from . import pipeline as pl
from .extract import normalize
from .llm import OPUS, cost, record

SYSTEM = """You are a credit analyst answering questions about a borrower's credit agreement, given the agreement \
and its amendments (oldest first; later amendments control). Answer from the documents only. Be specific: name \
the section, the basket or ratio, and the dollar amounts or levels. Where the answer depends on facts the \
documents don't give (current EBITDA, amounts already used), say what it depends on instead of guessing. Keep it \
under 250 words."""


def ask(cik: str, question: str, client: anthropic.Anthropic | None = None) -> dict:
    ch = pl._load(pl.UNIVERSE / cik / "chain.json")
    if not ch or not ch["used"]:
        raise SystemExit(f"No agreement chain for CIK {cik}; run `build --stage chain` first.")
    texts = [(d, edgar.fetch_text(d["url"])) for d in ch["used"]]
    blocks = [{"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": text},
               "title": f"{d.get('title') or 'Document'} ({d.get('dated') or 'undated'})", "citations": {"enabled": True}}
              for d, text in texts]
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    client = client or anthropic.Anthropic()
    with client.beta.messages.stream(
        model=OPUS, max_tokens=16000, system=SYSTEM, output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        messages=[{"role": "user", "content": [*blocks, {"type": "text", "text": question}]}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude declined to answer this question.")
    usage = {k: getattr(msg.usage, k, 0) or 0 for k in
             ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
    record("ask", f"ask-{cik}", OPUS, usage, batch=False)
    hay = [normalize(t) for _, t in texts]
    answer, cites = [], []
    for block in msg.content:
        if block.type != "text":
            continue
        answer.append(block.text)
        for c in block.citations or []:
            n = len(cites) + 1
            i = getattr(c, "document_index", 0)
            cites.append({"n": n, "document": ch["used"][i].get("title"), "url": ch["used"][i]["url"],
                          "text": c.cited_text, "found": normalize(c.cited_text) in hay[i]})
            answer.append(f" [{n}]")
    return {"question": question, "answer": "".join(answer).strip(), "citations": cites, "usage": usage,
            "usd": cost(OPUS, usage)}


def save(cik: str, qa: dict) -> None:
    path = pl.UNIVERSE / cik / "qa.json"
    items = json.loads(path.read_text()) if path.exists() else []
    items = [q for q in items if q["question"] != qa["question"]] + [qa]
    pl._save(path, items)
