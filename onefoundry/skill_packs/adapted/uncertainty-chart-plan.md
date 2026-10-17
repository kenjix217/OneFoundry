# uncertainty chart plan

Use this procedure when a work order needs to communicate a supplied distribution, estimate, range, forecast, or comparison with uncertainty. Inputs are the question, observations or summary statistics, definitions of intervals, units, population, and source method.

1. Identify what is measured and which uncertainty is actually quantified. Distinguish sampling error, model scenarios, observed spread, and missing data.
2. Recommend an encoding that preserves the central comparison and the meaning of each interval or distribution. State the required axis, baseline, sample size, and annotations.
3. Flag claims the supplied data cannot support, including false precision, hidden denominators, or comparisons between incompatible intervals.
4. Provide a text alternative and a validation checklist for calculations and rendering.

Return a chart specification and a plain-language interpretation with explicit unknowns. Do not invent confidence intervals or claim a rendered chart, statistical validation, or browser QA. Exact arithmetic may use `decimal_calculate_v1` only when admitted; a Markdown artifact requires admitted `write_text_artifact_v1`.
