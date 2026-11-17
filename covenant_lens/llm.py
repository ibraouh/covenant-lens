"""One place for every Claude call: model prices, a disk cache, the Batch API, and a cost ledger.

Bulk work (dozens of agreements, hundreds of filing excerpts) goes through the Message Batches API at
half price. Every result is cached by job id, so a re-run only pays for what changed, and every call is
written to data/ledger.jsonl with its token counts and dollar cost.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import anthropic
from anthropic import transform_schema
from pydantic import BaseModel, ValidationError

from .edgar import ROOT

LLM_DIR = ROOT / "data" / "llm"
LEDGER = ROOT / "data" / "ledger.jsonl"

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"

# List prices per million tokens (input, output). Haiku 5.5 charges more for prompts over 100K tokens.
PRICES = {OPUS: (4.00, 20.00), SONNET: (2.00, 10.00), HAIKU: (0.10, 0.50)}
HAIKU_LONG = (0.50, 2.50)
CACHE_WRITE, CACHE_READ = 1.25, 0.05  # multiples of the input price (5-minute cache)
# Server-side refusal fallbacks exist for Opus 5.5 and Sonnet 5.5, not Haiku 5.5, and not in batches.
FALLBACK_MODELS = {OPUS, SONNET}

T = TypeVar("T", bound=BaseModel)


def cost(model: str, usage: dict, batch: bool = False) -> float:
    """Dollar cost of one call from its usage numbers."""
    total_in = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get(
        "cache_read_input_tokens", 0
    )
    p_in, p_out = HAIKU_LONG if model == HAIKU and total_in > 100_000 else PRICES[model]
    dollars = (
        usage.get("input_tokens", 0) * p_in
        + usage.get("cache_creation_input_tokens", 0) * p_in * CACHE_WRITE
        + usage.get("cache_read_input_tokens", 0) * p_in * CACHE_READ
        + usage.get("output_tokens", 0) * p_out
    ) / 1e6
    return dollars * (0.5 if batch else 1.0)


def _usage_dict(usage: Any) -> dict:
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return {k: getattr(usage, k, 0) or 0 for k in keys}


def record(stage: str, job_id: str, model: str, usage: dict, batch: bool) -> float:
    dollars = cost(model, usage, batch)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"), "stage": stage,
                            "job": job_id, "model": model, "batch": batch, **usage, "usd": round(dollars, 5)}) + "\n")
    return dollars


def ledger_totals() -> dict[str, dict]:
    """Spend so far, by stage and model."""
    out: dict[str, dict] = {}
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            r = json.loads(line)
            k = f"{r['stage']} / {r['model']}"
            t = out.setdefault(k, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0})
            t["calls"] += 1
            t["input_tokens"] += r["input_tokens"] + r.get("cache_creation_input_tokens", 0) + r.get("cache_read_input_tokens", 0)
            t["output_tokens"] += r["output_tokens"]
            t["usd"] += r["usd"]
    return out


@dataclass
class Job:
    """One structured-output request. `content` is the user message: a string or a list of content blocks."""

    id: str                      # [A-Za-z0-9_-]{1,64}; also the cache file name
    schema: type[BaseModel]
    content: str | list[dict]
    system: str
    model: str = OPUS
    effort: str = "high"
    max_tokens: int = 32000
    meta: dict = field(default_factory=dict)

    def params(self) -> dict:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "messages": [{"role": "user", "content": self.content}],
            "output_config": {"effort": self.effort,
                              "format": {"type": "json_schema", "schema": transform_schema(self.schema)}},
        }


@dataclass
class Outcome:
    job_id: str
    parsed: BaseModel | None
    usage: dict
    error: str | None = None


def _cache_path(stage: str, job_id: str) -> Path:
    return LLM_DIR / stage / f"{job_id}.json"


def cached(stage: str, job: Job) -> Outcome | None:
    p = _cache_path(stage, job.id)
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    if d.get("error"):
        return None  # failed before: try again
    return Outcome(job.id, job.schema.model_validate(d["parsed"]), d["usage"])


def _store(stage: str, job: Job, out: Outcome) -> None:
    p = _cache_path(stage, job.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"job": job.id, "model": job.model, "meta": job.meta, "usage": out.usage, "error": out.error,
                             "parsed": out.parsed.model_dump() if out.parsed else None}, indent=2))


def _parse_message(job: Job, message: Any) -> tuple[BaseModel | None, str | None]:
    if message.stop_reason == "refusal":
        return None, f"refusal: {getattr(message, 'stop_details', None)}"
    if message.stop_reason == "max_tokens":
        return None, "hit max_tokens before finishing"
    text = "".join(b.text for b in message.content if b.type == "text")
    try:
        return job.schema.model_validate(json.loads(text)), None
    except (json.JSONDecodeError, ValidationError) as e:
        return None, f"bad structured output: {e}"[:500]


def run_batch(stage: str, jobs: list[Job], client: anthropic.Anthropic | None = None, poll: int = 30,
              log=print) -> dict[str, Outcome]:
    """Run jobs through the Batch API (50% off). Cached jobs are skipped; an interrupted batch is resumed."""
    client = client or anthropic.Anthropic()
    results: dict[str, Outcome] = {}
    todo = []
    for j in jobs:
        hit = cached(stage, j)
        if hit:
            results[j.id] = hit
        else:
            todo.append(j)
    if not todo:
        return results
    by_id = {j.id: j for j in todo}
    pending = LLM_DIR / stage / "_pending_batch.json"
    batch_id = None
    if pending.exists():
        saved = json.loads(pending.read_text())
        if set(saved["jobs"]) == set(by_id):
            batch_id = saved["id"]
            log(f"[{stage}] resuming batch {batch_id}")
    if not batch_id:
        batch = client.messages.batches.create(requests=[{"custom_id": j.id, "params": j.params()} for j in todo])
        batch_id = batch.id
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(json.dumps({"id": batch_id, "jobs": sorted(by_id)}))
        log(f"[{stage}] submitted batch {batch_id} with {len(todo)} requests")
    while True:
        b = client.messages.batches.retrieve(batch_id)
        if b.processing_status == "ended":
            break
        c = b.request_counts
        log(f"[{stage}] {c.succeeded} done, {c.processing} running, {c.errored} errored")
        time.sleep(poll)
    spent = 0.0
    for r in client.messages.batches.results(batch_id):
        job = by_id.get(r.custom_id)
        if job is None:
            continue
        if r.result.type == "succeeded":
            msg = r.result.message
            usage = _usage_dict(msg.usage)
            spent += record(stage, job.id, job.model, usage, batch=True)
            parsed, err = _parse_message(job, msg)
            out = Outcome(job.id, parsed, usage, err)
        else:
            detail = getattr(getattr(r.result, "error", None), "error", None)
            out = Outcome(job.id, None, {}, f"{r.result.type}: {detail}")
        _store(stage, job, out)
        results[job.id] = out
    pending.unlink(missing_ok=True)
    failed = [o.job_id for o in results.values() if o.error]
    log(f"[{stage}] batch done: ${spent:.2f}, {len(failed)} failed" + (f" ({', '.join(failed[:5])})" if failed else ""))
    return results


def run_one(stage: str, job: Job, client: anthropic.Anthropic | None = None) -> Outcome:
    """One streamed request, with server-side refusal fallbacks where the model supports them. Cached."""
    hit = cached(stage, job)
    if hit:
        return hit
    client = client or anthropic.Anthropic()
    kwargs = job.params()
    if job.model in FALLBACK_MODELS:
        kwargs |= {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    else:
        kwargs |= {"betas": []}
    with client.beta.messages.stream(**kwargs) as stream:
        msg = stream.get_final_message()
    usage = _usage_dict(msg.usage)
    record(stage, job.id, msg.model if msg.model in PRICES else job.model, usage, batch=False)
    parsed, err = _parse_message(job, msg)
    out = Outcome(job.id, parsed, usage, err)
    _store(stage, job, out)
    return out
