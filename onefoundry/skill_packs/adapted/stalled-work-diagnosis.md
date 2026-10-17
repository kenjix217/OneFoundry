# stalled-work diagnosis

Use this procedure when an operator asks why a named OneFoundry work order or run has stopped advancing. Diagnose one owner path at a time from supplied records and, when admitted, `recent_project_work_v1`, `run_evidence_v1`, and `coding_evidence_v1`.

1. Identify the exact project, order, run, expected next transition, and observation time. Read the saved result and event types before interpreting a displayed status.
2. Build a concise timeline of known transitions and receipts. Distinguish an approval wait, a deliberate block, a failed attempt, an unknown outcome, and a run that simply lacks enough evidence. For coding attempts, treat a terminal result as verified only when the coding evidence says its receipt is corroborated.
3. Name the earliest transition that failed to produce its required owner record. Give the leading explanation, direct supporting evidence, competing explanations, and confidence. If the record does not establish a cause, say which owner read or receipt would decide it.
4. Recommend the smallest next operator or system-owner action. Do not suggest retrying an unknown side effect without reconciliation. Do not change run state or launch work as part of diagnosis.

Return a brief timeline, conclusion, evidence IDs, uncertainty, and next check. This procedure does not inspect external issue records, infer a complete execution history from bounded lists, or perform automatic recovery.
