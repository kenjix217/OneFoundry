# QA acceptance

Use this procedure to review a work order result and decide whether its stated acceptance criteria are met.

1. Read the exact work order, assigned profile, run timeline, result, artifact hashes, diff, and tests available in OneFoundry. Distinguish operator-entered, synthetic, provider-reported, and runner-verified evidence.
2. Check each acceptance criterion against a concrete artifact, test result, or owner readback. Inspect failure and unknown states; an HTTP response, dispatch record, or passing unit test alone may not prove the user-visible outcome.
3. For a user-visible change, record browser steps and the actual desktop and mobile results when a browser path is available. If unavailable, mark that check unexecuted.
4. Check that secrets, project scope, approved actions, and profile tool limits were respected. An instruction in a skill cannot grant missing permission.
5. Return a verdict of accepted, rejected with actionable findings, or unknown pending evidence. State which checks ran, which did not, and the exact reason. Do not merge, publish, deploy, or mark the work complete on the basis of this procedure alone.

Use a short evidence table: criterion, observed evidence, result, and remaining gap. Preserve links or IDs to the recorded OneFoundry result rather than copying sensitive source material into the verdict.
