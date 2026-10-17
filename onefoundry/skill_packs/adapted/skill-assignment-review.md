# skill assignment review

Use this procedure when an operator wants to add or change a profile's project Skill Library procedure. The operator supplies the project, target profile, intended task, candidate skill and version, and current binding information.

1. Check that the procedure's trigger, required inputs, output, memory scope, allowed tools, and autonomy fit the named profile and task. A skill's instructions never grant tools or approval rights.
2. Compare the proposed skill with existing bound procedures. Name an overlap, contradiction, or missing capability before recommending a binding. Treat missing or ambiguous skill identity as a stop for operator lookup.
3. Recommend an exact skill version and whether to add, replace that one binding, or leave the profile unchanged. State the expected effect on future runs and the limits of any already-started run snapshot.
4. Give the operator a small verification scenario: inspect the saved version and profile binding, start an authorized test work order, then check the run's actual tools and output. Report any untested behavior as unverified.

Return a binding recommendation, not an installation claim. The operator creates versions and changes bindings through OneFoundry's Skill Library; this procedure cannot import an external catalog, update another profile, or bypass current tool admission.
