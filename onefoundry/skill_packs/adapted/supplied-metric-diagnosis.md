# supplied metric diagnosis

Use this procedure to examine a reported metric change when the work order supplies comparable values and, if available, segment or component data.

1. Pin the metric definition, grain, population, current period, comparison period, and source description. Check whether a definition change, missing period, or data-quality concern could explain the apparent movement.
2. Recompute the headline change from supplied values. Use `decimal_calculate_v1` when admitted, retain units, and distinguish rates from counts.
3. Test the smallest useful decomposition supported by the inputs: numerator and denominator, segment contribution, volume and rate, or another stated metric identity. Reconcile component totals with the headline before ranking drivers.
4. Separate arithmetic contributors from causal explanations. List plausible mechanisms as hypotheses with the source, comparison, or experiment needed to test them.
5. State what changed, what can explain the change arithmetically, what remains unknown, and the next evidence request. If inputs are too sparse, return a diagnostic plan rather than a cause claim.

The work order and approved memory are the evidence boundary. This procedure does not discover live analytics sources, inspect dashboards, or prove causality.
