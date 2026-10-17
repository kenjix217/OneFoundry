# regression-first implementation plan

Trigger: The operator requests a fix or feature with a testable behavior change.

Inputs: Expected behavior, existing contract, defect reproduction or new acceptance case, affected owner, and test environment.

Capability preflight: Check for an authorized coding runner that can edit the disposable checkout and execute the relevant tests with receipts. The text route cannot write tests, run them, or observe a failing baseline; provider-reported test names are not independent proof.

1. State the smallest test case that would fail on the baseline for the intended reason.
2. Specify success, invalid-input, dependency-failure, and boundary cases relevant to the change.
3. Require a recorded baseline failure, minimal implementation, focused pass, and broader regression result from the runner.
4. If any stage lacks a receipt, mark the verification incomplete and request the exact missing execution evidence.

Output: A test sequence and evidence checklist, or `blocked_for_test_runner`. This procedure does not claim a red/green cycle unless saved receipts prove both states.
