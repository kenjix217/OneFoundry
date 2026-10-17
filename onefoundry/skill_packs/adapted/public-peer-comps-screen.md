# public peer comps screen

Use this limited screen when the operator explicitly asks for a quick peer comparison and supplies a listed issuer, a proposed peer set, and comparable valuation inputs. An admitted built-in Researcher may fetch public HTTPS source pages; OneFoundry has no licensed price, enterprise-value, or consensus feed.

1. Pin issuer identity, valuation date, currency, fiscal basis, and the specific valuation question. Mark each peer as core, secondary, or excluded with a business-model reason.
2. List every price, shares, debt, cash, denominator, and estimate with source, period, as-of time, and definition. Do not combine reported, adjusted, and forecast metrics without a labeled bridge.
3. Calculate supported multiples or implied ranges from compatible supplied values only through admitted `decimal_calculate_v1`. Show inputs, the bounded decimal result, and checks for unit scaling and stale or missing observations. Without that tool, list the needed calculation instead of presenting a new numeric multiple. An absent EV bridge is not zero debt.
4. Return a short peer comparison, outliers and exclusions, what the range can and cannot establish, and the first source needed to improve it. Save Markdown or CSV through `write_text_artifact_v1` only when requested and admitted.

This screen is not a refreshed comps workbook, live valuation, price target, or investment instruction.
