# Coding provider adapter design

**Trigger:** An engineer is asked to plan a new external coding provider connection.

**Required inputs:** Provider API/CLI contract, authentication method, session and receipt semantics, runner isolation rules, and failure cases.

**Capability preflight:** The Execution Gateway owns coding attempts; role tools cannot register an adapter or launch a CLI. Live launch requires proved isolation, authentication, and spending control.

**Procedure:** Map launch inputs, session identity, cancellation, timeout, receipt, and unknown-outcome reconciliation to existing Execution Gateway contracts. Identify secrets and network boundaries. Propose contract tests for duplicate attempts and crash recovery.

**Output:** Return an adapter design and test matrix; mark implementation and live connection unexecuted.
