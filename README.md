# Covenant Lens

Reads public companies' credit agreements, finds the financial covenants they promised their lenders, tests them every quarter against SEC data, and grades itself against the numbers the companies report.

When a company borrows from banks, the loan contract usually caps how indebted it can get, for example "debt may not exceed 4.00 times EBITDA." Lenders and credit funds track these limits for every borrower, every quarter, because a borrower drifting toward a limit is the earliest sign of trouble. The contracts run 150 to 300 pages, they get amended every year or two, and each one defines EBITDA its own way, so this monitoring is mostly done by hand.

**Dashboard:** `site/index.html`, one static file with the data embedded (see [Run it](#run-it)).

## Results

<!--RESULTS-->
A full run over 33 borrowers is in progress; its scores will replace this section. Verified so far, on the three hand-labeled agreements (8x8, Casella Waste, Varex Imaging): Claude Opus found 8 of 8 covenants with the right direction, limits, step-downs and EBITDA add-backs, and all 8 quotes were found word for word in the filings. Against the 8 quarters of covenant ratios Casella publishes, debt and netted cash matched the company's own figures to within $0.1M; the estimate from GAAP line items ran about 20% high because the contract's EBITDA counts acquisitions for a full year.

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
