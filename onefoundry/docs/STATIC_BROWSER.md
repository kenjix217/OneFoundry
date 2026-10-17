# Static browser inspection pilot

This feature is off by default. It inspects operator-supplied HTML/CSS at one
saved localhost URL and returns bounded text evidence. The browser child has no
network, mounted files, Docker socket, credentials, or screenshot output.

1. From `onefoundry/`, build `runner/browser_static.Dockerfile` with `runner/` as
   context. Record the exact Docker image ID (`docker image inspect --format
   '{{.Id}}' <tag>`). The child image must carry the expected pinned base label.
2. Build the `browser-static-bridge` target in `onefoundry/Dockerfile` and record
   its exact image ID. Configure `BROWSER_STATIC_ENABLED=true` and
   `BROWSER_STATIC_IMAGE_ID=<child image ID>` for the OneFoundry app. Keep the
   runner spool on its persistent volume.
3. On the trusted host, run `python -m runner.browser_static_host_bridge
   --volume <compose runner_spool volume> --helper-image <bridge image ID>
   --state-root <private host directory> --watch`. The state directory must
   be private to the operator. Only this host process may use the Docker CLI.
4. Start a work order for a built-in Researcher or QA profile with
   `browser_static_inspect_v1` allowed. QA can use an active manual run;
   Researcher can also use an active text run. In the run's **Browser fixture
   and evidence** panel, submit the exact HTML/CSS fixture, approve its inbox
   request, then dispatch it to the assigned role. Check the saved job receipt
   and read the bounded evidence in the same panel.

Dispatch requires the tool in both the saved run snapshot and current profile.
An uncertain launch is never resent automatically. The live model SDK does not
yet advertise this tool; dispatch is an operator action bound to the assigned
role. The host bridge and browser child are separate from the app process.
