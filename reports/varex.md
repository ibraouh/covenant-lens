# Varex Imaging Corp: covenant check

- Credit agreement: https://www.sec.gov/Archives/edgar/data/1681622/000168162226000035/exhibit101-creditandguaran.htm (dated 2026-03-13)
- Covenants read by: hand labels (no AI)
- Financials: SEC XBRL company facts, trailing twelve months at each quarter-end

## Covenants found

**Consolidated Total Net Leverage Ratio** (Section 6.12(a)): max 4.25, 4 from 2027-12-01, 3.75 from 2028-12-01; tested quarterly
- Quote found in agreement: yes
- EBITDA add-backs used: stock_comp, impairments, restructuring, other; cash netting capped at $100.0M
> The Parent Borrower shall not permit the Consolidated Total Net Leverage Ratio, on the last day of any Fiscal Quarter, to be greater than the ratio set forth below opposite such Fiscal Quarter

**Consolidated Fixed Charge Coverage Ratio** (Section 6.12(b)): min 1.2; tested quarterly
- Quote found in agreement: yes
- EBITDA add-backs used: stock_comp, impairments, restructuring, other
> The Parent Borrower shall not permit the Consolidated Fixed Charge Coverage Ratio, on the last day of any Fiscal Quarter, to be less than 1.20:1.00.

## Quarter by quarter (estimates from public data)

| Quarter end | Covenant | Value | Limit | Headroom | Status |
|---|---|---|---|---|---|
| 2025-04-04 | Consolidated Total Net Leverage Ratio | 4.86x | 4.25x | -14% | not tested: before this agreement was signed |
| 2025-04-04 | Consolidated Fixed Charge Coverage Ratio | n/a | 1.2x | n/a | not tested: before this agreement was signed |
| 2025-07-04 | Consolidated Total Net Leverage Ratio | 2.69x | 4.25x | +37% | not tested: before this agreement was signed |
| 2025-07-04 | Consolidated Fixed Charge Coverage Ratio | n/a | 1.2x | n/a | not tested: before this agreement was signed |
| 2025-10-03 | Consolidated Total Net Leverage Ratio | 2.47x | 4.25x | +42% | not tested: before this agreement was signed |
| 2025-10-03 | Consolidated Fixed Charge Coverage Ratio | n/a | 1.2x | n/a | not tested: before this agreement was signed |
| 2026-01-02 | Consolidated Total Net Leverage Ratio | 2.39x | 4.25x | +44% | not tested: before this agreement was signed |
| 2026-01-02 | Consolidated Fixed Charge Coverage Ratio | n/a | 1.2x | n/a | not tested: before this agreement was signed |
| 2026-04-03 | Consolidated Total Net Leverage Ratio | 2.51x | 4.25x | +41% | pass |
| 2026-04-03 | Consolidated Fixed Charge Coverage Ratio | n/a | 1.2x | n/a | can't compute (fixed charge coverage needs data public filings don't break out) |
| 2026-07-03 | Consolidated Total Net Leverage Ratio | 2.17x | 4.25x | +49% | pass |
| 2026-07-03 | Consolidated Fixed Charge Coverage Ratio | n/a | 1.2x | n/a | can't compute (fixed charge coverage needs data public filings don't break out) |

## What the company says

No covenant compliance statement found in its recent 10-Q or 10-K filings.

## Inputs and limits

| Quarter end | Debt | Cash | LTM operating income | LTM D&A | LTM interest |
|---|---|---|---|---|---|
| 2025-04-04 | $567.2M | $205.1M | $53.5M | $27.3M | $33.2M |
| 2025-07-04 | $367.5M | $136.4M | $-36.5M | $27.1M | $35.2M |
| 2025-10-03 | $367.5M | $145.0M | $-27.8M | $27.2M | $35.5M |
| 2026-01-02 | $367.6M | $119.5M | $-23.6M | $26.9M | $35.4M |
| 2026-04-03 | $347.6M | $85.3M | $-31.3M | $27.1M | $42.4M |
| 2026-07-03 | $347.1M | $99.2M | $72.2M | $27.3M | $38.8M |

XBRL tags used: debt: LongTermDebt; cash: CashAndCashEquivalentsAtCarryingValue; operating income: OperatingIncomeLoss; D&A: Depreciation + AmortizationOfIntangibleAssets; interest: InterestExpenseNonoperating; stock comp: ShareBasedCompensation; impairments: GoodwillImpairmentLoss; restructuring: not reported

These are estimates from public filings. Covenant EBITDA in the agreement usually allows more adjustments than public numbers show (pro forma acquisitions, cost savings, one-off items), and covenant debt may count items like letters of credit differently. Treat a close call as a reason to look closer, not as a finding.

## Notes from the agreement

Leverage steps down from 4.25x (closing to the September 2027 quarter) to 4.00x (December 2027 to September 2028) and 3.75x after that. After an acquisition of $125M or more the limit rises 0.50x for that quarter and the next three, capped at 4.50x. Consolidated Adjusted EBITDA adds back non-cash charges and impairments, non-cash equity compensation, non-recurring fees, earn-out changes, and restructuring, severance and cost-savings charges plus run-rate synergies (those two together capped at 20% of EBITDA). Only cash in accounts under the agent's control, up to $100M, is netted against debt. The fixed charge ratio needs capex, cash taxes and scheduled principal payments, which this tool doesn't compute.
