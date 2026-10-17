# public equity scenario screen

Use this limited screen when the operator explicitly requests a quick scenario view and supplies a dated base case and variables to stress for one listed issuer. A missing base model, price, or valuation anchor must remain missing.

1. Record the issuer, base-case owner and date, scenario horizon, units, and which variables may change. Separate reported inputs, operator assumptions, and calculated outputs.
2. Construct a small low/base/high table by changing one or two named drivers only when `decimal_calculate_v1` is admitted for the needed bounded decimal arithmetic. Show each formula and input, and reconcile the base case before comparing deltas. Without calculation admission, return a qualitative scenario framework and the missing calculation path.
3. Identify the binding breakpoint or evidence threshold only if a supplied target, hurdle, or decision rule defines it. Do not fabricate probabilities, expected returns, price targets, or portfolio actions.
4. Return the supported table or qualitative framework, sensitivity interpretation, source gaps, and the next fact that could change the view. Save text or CSV through `write_text_artifact_v1` only when requested and admitted.

This is an illustrative calculation from supplied values, not a validated workbook, model refresh, or market-price snapshot.
