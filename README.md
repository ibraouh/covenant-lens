# Covenant Lens

Reads public companies' credit agreements, finds the financial covenants they promised their lenders, tests them every quarter against SEC data, and grades itself against the numbers the companies report.

When a company borrows from banks, the loan contract usually caps how indebted it can get, for example "debt may not exceed 4.00 times EBITDA." Lenders and credit funds track these limits for every borrower, every quarter, because a borrower drifting toward a limit is the earliest sign of trouble. The contracts run 150 to 300 pages, they get amended every year or two, and each one defines EBITDA its own way, so this monitoring is mostly done by hand.

**Dashboard:** `site/index.html`, one static file with the data embedded (see [Run it](#run-it)).

## Results

<!--RESULTS-->
Run of November 17, 2026: 29 public borrowers with agreements read, picked from 246 companies whose 10-Qs mention covenant ratios. Every number below is recomputed by `build` and shown on the dashboard.

**Reading the contract.** Graded against the limits companies report in their 10-Qs (from the compliance certificates they send lenders), for quarters on or after the latest amendment Claude read:

| Check | Result |
|---|---|
| Covenant limit matches what the company reports | **96 / 100** |
| Covenants the company reports that Claude found in the contract | 166 / 172 |
| Covenant quotes found word for word in the filing | 63 / 64 |
| Lender-protection quotes found word for word | 83 / 84 |
| Hand-labeled agreements (8 covenants, every field) | 8 / 8 |

The four limit misses: three are Mayville Engineering, whose amended agreement marks deleted terms with strikethrough, which the HTML-to-text step was dropping, so deleted limits read as live. The eval caught it; deleted text is now removed, and the nine agreements with strikethrough are queued for a fresh read. The fourth is National CineMedia, where Claude mapped "Q4 2026" to December when NCM's fiscal quarter ends October 1. The one failed quote is Blackbaud's, which quoted deleted text and is now correctly flagged.

**Finding the contract.** On the three hand-labeled borrowers, the agreement finder picked the same documents I did for 8x8 and Varex, and for Casella it also found a 2025 amendment I had missed (it changes only a pricing table). When an original agreement predates the search window, the finder follows the amendments back to it.

**Doing the math.** On the 33 borrower-quarters where both estimates exist, the median error against the company's reported ratio is **8.5%** when EBITDA is rebuilt from GAAP line items and **4.8%** when it comes from the company's own adjusted EBITDA. Where the company spells out its inputs (Casella), debt and netted cash match its figures to within $0.1M; the gap is EBITDA, because contracts add back items public data doesn't show.

**Which model.** The same 11 agreement chains read by three models, same prompt and settings ([full table](evals/model_comparison.md)):

| Model | Limits match | Covenant quotes verified | Hand labels | Cost per agreement (Batch API) |
|---|---|---|---|---|
| Claude Opus 5.5 | 53 / 56 | 28 / 29 | 8 / 8 | $0.52 |
| Claude Sonnet 5.5 | 51 / 56 | 27 / 29 | 8 / 8 | $0.25 |
| Claude Haiku 5.5 | 51 / 56 | 24 / 28 | 7 / 8 | $0.06 |

Opus reads limits and quotes most reliably; Sonnet is close at half the price; Haiku costs a ninth as much and slips mainly on exact quotes. For a monitoring job where a wrong limit costs more than a dollar, Opus is the default.

**Asking questions.** Six questions across three agreements, answered with citations: all 58 cited passages were found in the filings. The first question on a contract costs $0.99 to $1.52 (the contract goes into the prompt cache); the follow-up costs $0.07 to $0.09.

**Cost.** $31.26 for the whole run, including screening 240 filings, reading 30 agreement chains ($13.83), the model comparison and the questions. See `python -m covenant_lens costs`.

**One real flag.** Nuvera Communications reported a debt service coverage ratio of 1.99 against a 2.00 minimum at June 30, 2026, below its covenant with CoBank; its next 10-Q describes a new loan facility. It tops the watchlist as the only reported miss.

## How it works

```
EDGAR full-text search ──► screen 10-Q excerpts ──► borrowers that publish their covenant ratios (answer keys)
                                                          │
           ┌──────────────────────────────────────────────┼──────────────────────────────────┐
           ▼                                              ▼                                  ▼
 every loan exhibit since 2016                   last 4 10-Qs / 10-Ks                last 5 earnings releases
 Claude reads cover pages ─► code links          Claude reads excerpts:              Claude reads excerpts:
 base agreement + amendments                     reported ratio + limit              company adjusted EBITDA
           │                                              │                                  │
           ▼                                              │                                  │
 Claude Opus reads the whole chain once:                  │          XBRL company facts      │
 covenants, step-downs, EBITDA add-backs,                 │          (debt, cash, EBIT, D&A, │
 cash caps, six lender protections                        │           interest, add-backs)   │
           │                                              │                  │               │
           └──────────────► code checks every quote ◄─────┴──────────────────┴───────────────┘
                                      │
                                      ▼
            covenant math in code, three ways per quarter: reported, adjusted EBITDA, GAAP line items
                                      │
                                      ▼
            grades vs answer keys ─► static dashboard ─► weekly refresh (GitHub Actions)
```

1. **Find borrowers worth checking.** EDGAR full-text search finds 10-Qs that talk about covenant ratios (financial companies excluded). Claude reads only the excerpts around each ratio, a few thousand tokens per filing, and keeps companies that state both their actual ratio and the limit. Those numbers come from the compliance certificate the company sends its lenders, so they are the answer key.
2. **Find the contract without being told where it is.** Every loan-related exhibit the company filed since 2016 is pulled. Claude reads each cover page (title, "dated as of", which agreement it amends), and code picks the latest base agreement for the main bank facility plus every later amendment, in order. An amendment that restates the whole agreement becomes the new base.
3. **Read it once.** Claude Opus 5.5 reads the whole chain in one request (up to about 500K tokens) and returns each maintenance covenant, its step-downs, the EBITDA add-backs, cash netting caps, springing conditions, and six document terms lenders check after recent liability management deals (J.Crew, Serta and Chewy protections, unrestricted subsidiary caps, add-back caps, equity cures).
4. **Trust nothing a model says without checking.** Every covenant, every document term, every reported ratio and every EBITDA figure must come with a verbatim quote. Code checks that the quote is in the filing and that the number is in the quote. Anything that fails is dropped or flagged.
5. **Do the math in code.** Quarterly figures are rebuilt from XBRL year-to-date facts (Q4 is the full year minus nine months) and summed to trailing twelve months. Each covenant is tested at every quarter against the limit in force then, three ways: the company's reported ratio, an estimate from the company's adjusted EBITDA, and an estimate from GAAP line items. The dashboard also shows the EBITDA cushion (how far EBITDA can fall before a test trips) and upcoming step-downs.
6. **Grade it.** Reported limits grade the reading; reported ratios grade the math. Three agreements are also hand-labeled field by field.

## Engineering notes

- **Batch API for bulk work.** Screening, excerpts, cover pages and agreement reading all go through the Message Batches API at half price. `llm.run_batch` caches every result by job id and resumes an interrupted batch, so re-running the pipeline only pays for new filings.
- **Cost ledger.** Every call is logged to `data/ledger.jsonl` with its token counts and dollar cost; `python -m covenant_lens costs` totals it by stage and model.
- **Structured outputs.** Every extraction uses a Pydantic schema enforced by the API (`output_config.format`), with effort set per task: `high` for contracts, `medium` for filing excerpts, `low` for cover pages.
- **Citations and prompt caching for Q&A.** `ask` sends the agreement as document blocks with citations on and the documents cached, so each claim in an answer points at the passage it rests on, and follow-up questions read the contract from cache at a twentieth of the input price.
- **Model choice is measured.** `compare` reads the same agreements with Opus 5.5, Sonnet 5.5 and Haiku 5.5 and scores each against the same answer keys (see `evals/model_comparison.md`).
- **Tests and CI.** `pytest` covers the quarterly math, step-downs, cash caps, cushions, quote and number verification, the agreement chain resolver, grading and pricing, with no network calls. GitHub Actions runs `ruff` and the tests on every push, and a weekly workflow refreshes the data and redeploys the dashboard.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # SEC_USER_AGENT (name and email; the SEC requires one) and ANTHROPIC_API_KEY
```

```bash
.venv/bin/python -m covenant_lens build             # discover, screen, collect, chain, read, compute
.venv/bin/python -m covenant_lens site              # writes site/index.html
.venv/bin/python -m covenant_lens compare           # Opus vs Sonnet vs Haiku on the same agreements
.venv/bin/python -m covenant_lens ask 911177 "How much more debt could the borrower take on today?"
.venv/bin/python -m covenant_lens refresh           # new filings only
.venv/bin/python -m covenant_lens costs             # spend so far, by stage and model
.venv/bin/python -m covenant_lens check --gold evals/gold/casella.json   # one borrower, hand labels, no AI
```

## Limits

- **Covenant EBITDA is still an estimate** where a company doesn't report its ratio. Agreements add back items public data can't see (pro forma acquisitions, projected synergies), so estimates run high on leverage. The dashboard marks every number as reported or estimated.
- **Some ratios can't be computed from public data**: secured leverage, fixed charge coverage and minimum liquidity need inputs filings don't break out. They are read and shown, not estimated.
- **The answer keys cover companies that publish their ratios**, which skews toward larger, more transparent borrowers.
- **Public companies only.** The same pipeline would run on a lender's own documents and compliance certificates, which is where this kind of tool earns its keep.
