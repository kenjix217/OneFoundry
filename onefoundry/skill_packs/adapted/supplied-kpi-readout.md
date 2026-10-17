# supplied KPI readout

Use this procedure to prepare a KPI update from actual values, definitions, comparison periods, and targets supplied in the work order or approved memory.

1. State the reporting period, cutoff, population, unit, metric definition, and source named by the operator. If actual values or definitions are missing, request them or provide a clearly labeled template; do not fabricate a scorecard.
2. Check that the current and comparison values use compatible definitions, units, filters, and time windows. Mark a series break or disputed source beside the affected measure.
3. Calculate absolute and relative change only from available inputs. Use `decimal_calculate_v1` when admitted for exact arithmetic. Distinguish a percent change from a percentage-point change and show the denominator.
4. Compare with a supplied target or approved pacing rule. Never invent a target or treat an arbitrary linear pace as company policy.
5. Report the observed movement, supported drivers, decision implication, and important gaps. Call a suspected cause a hypothesis until evidence supports it.

Return a compact readout with numbers, calculations, source labels, cutoff, and open checks. Save a Markdown or CSV artifact only when `write_text_artifact_v1` is admitted and the order requests it. This procedure does not query a warehouse, refresh a dashboard, or schedule future reports.
