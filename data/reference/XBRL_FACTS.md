# XBRL Reference Table - MSFT / AAPL, FY2023 & FY2024

Generated from SEC XBRL `companyfacts`. Every value is the raw tagged fact in
**whole dollars**. Use the `value_usd` column of `xbrl_facts.csv` for
`expected_numeric` - never the rounded billions shown here.

| Ticker | FY | Period end | Fiscal window |
|---|---|---|---|
| MSFT | 2023 | 2023-06-30 | Jul 2022 - Jun 2023 |
| MSFT | 2024 | 2024-06-30 | Jul 2023 - Jun 2024 |
| AAPL | 2023 | 2023-09-30 | Oct 2022 - Sep 2023 |
| AAPL | 2024 | 2024-09-28 | Oct 2023 - Sep 2024 |

Period ends are EDGAR `reportDate` values from `data/raw/manifest.json`.

> **AAPL FY2023 spanned 53 weeks; FY2024 spanned 52 weeks.** Stated under
> "Fiscal Period" in Item 7 of both Apple filings. Every AAPL FY2023 -> FY2024
> change below therefore compares a 53-week year against a 52-week one. MSFT has
> no such effect (fixed June 30 year end).

## MSFT

| Metric | FY2023 | FY2024 | YoY | us-gaap tag | FY2024 raw (whole $) |
|---|---:|---:|---:|---|---:|
| Revenue | $211.91B | $245.12B | +15.67% | `RevenueFromContractWithCustomerExcludingAssessedTax` | `245122000000` |
| Gross profit | $146.05B | $171.01B | +17.09% | `GrossProfit` | `171008000000` |
| R&D expense | $27.20B | $29.51B | +8.51% | `ResearchAndDevelopmentExpense` | `29510000000` |
| SG&A **NOT TAGGED** | -- | -- | -- | `(not reported)` | -- |
| &nbsp;&nbsp;- Selling & marketing | $22.76B | $24.46B | +7.46% | `SellingAndMarketingExpense` | `24456000000` |
| &nbsp;&nbsp;- General & administrative | $7.58B | $7.61B | +0.45% | `GeneralAndAdministrativeExpense` | `7609000000` |
| Operating income | $88.52B | $109.43B | +23.62% | `OperatingIncomeLoss` | `109433000000` |
| Net income | $72.36B | $88.14B | +21.80% | `NetIncomeLoss` | `88136000000` |
| Total assets | $411.98B | $512.16B | +24.32% | `Assets` | `512163000000` |
| Operating cash flow | $87.58B | $118.55B | +35.36% | `NetCashProvidedByUsedInOperatingActivities` | `118548000000` |

## AAPL

| Metric | FY2023 | FY2024 | YoY | us-gaap tag | FY2024 raw (whole $) |
|---|---:|---:|---:|---|---:|
| Revenue | $383.29B | $391.04B | +2.02% | `RevenueFromContractWithCustomerExcludingAssessedTax` | `391035000000` |
| Gross profit | $169.15B | $180.68B | +6.82% | `GrossProfit` | `180683000000` |
| R&D expense | $29.91B | $31.37B | +4.86% | `ResearchAndDevelopmentExpense` | `31370000000` |
| SG&A | $24.93B | $26.10B | +4.67% | `SellingGeneralAndAdministrativeExpense` | `26097000000` |
| &nbsp;&nbsp;- Selling & marketing | $18.26B | $18.64B | +2.08% | `SellingAndMarketingExpense` | `18639000000` |
| &nbsp;&nbsp;- General & administrative | $6.67B | $7.46B | +11.78% | `GeneralAndAdministrativeExpense` | `7458000000` |
| Operating income | $114.30B | $123.22B | +7.80% | `OperatingIncomeLoss` | `123216000000` |
| Net income | $97.00B | $93.74B | -3.36% | `NetIncomeLoss` | `93736000000` |
| Total assets | $352.58B | $364.98B | +3.52% | `Assets` | `364980000000` |
| Operating cash flow | $110.54B | $118.25B | +6.98% | `NetCashProvidedByUsedInOperatingActivities` | `118254000000` |

## Cross-company comparison - read the warning first

MSFT FY2024 covers **Jul 2023 - Jun 2024**. AAPL FY2024 covers **Oct 2023 - Sep 2024**.
These windows overlap by only three quarters. Any side-by-side "FY2024" comparison
below is comparing *different periods* and must be described as such.

| Metric | MSFT FY2024 | AAPL FY2024 |
|---|---:|---:|
| Revenue | $245.12B | $391.04B |
| Gross profit | $171.01B | $180.68B |
| R&D expense | $29.51B | $31.37B |
| SG&A | -- | $26.10B |
| &nbsp;&nbsp;- Selling & marketing | $24.46B | $18.64B |
| &nbsp;&nbsp;- General & administrative | $7.61B | $7.46B |
| Operating income | $109.43B | $123.22B |
| Net income | $88.14B | $93.74B |
| Total assets | $512.16B | $364.98B |
| Operating cash flow | $118.55B | $118.25B |

## SG&A asymmetry

**AAPL** tags `SellingGeneralAndAdministrativeExpense` directly.
**MSFT does not tag a combined SG&A at all** - it reports `SellingAndMarketingExpense`
and `GeneralAndAdministrativeExpense` as separate line items.

A single-tag "SG&A" lookup therefore succeeds for AAPL and returns nothing for MSFT.
Summing MSFT's two components is a *derivation*, not a reported fact - if you write a
question that depends on it, say so in `expected_answer`.
