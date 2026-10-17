# security fix verification checklist

Trigger: The operator asks whether a named security finding is closed by a specified code revision.

Inputs: Original finding and preconditions, fix revision, intended boundary, before/after evidence, regression tests, and legitimate behavior criteria.

Capability preflight: Confirm complete original and fixed source reads plus trustworthy before/after test receipts. The text route cannot execute security tests or inspect raw repository files; trusted exact-file reads and coding receipts must be separately admitted.

1. Restate the exploit path and identify the enforcement point the fix claims to change.
2. Compare the claimed fix against the original path and adjacent paths that share the same authority decision.
3. Check a negative case, authorized case, and any migration or compatibility behavior using actual receipts, not test names alone.
4. Return `closed`, `still_open`, or `unproven` only to the extent direct source and runtime evidence support the label; otherwise list the exact missing check.

Output: A bounded verification checklist and evidence verdict. It does not edit code, run tests, or certify a fix when source or receipts are unavailable.
