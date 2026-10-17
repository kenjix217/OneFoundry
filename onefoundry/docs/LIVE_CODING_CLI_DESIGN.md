# Trusted local Codex CLI and Claude Code execution

Status: source, disposable live pilot, and localhost port 8765 activation accepted on 2026-09-30. The installed worker reports both CLIs ready; a fresh installed Codex work order completed and reconciled.

## Scope

OneFoundry queues one ready built-in Engineer work order against the fixed disposable calculator repository. A separate host-native worker polls for jobs, invokes the installed Codex CLI or Claude Code with the operator's existing CLI login, and saves a session record, bounded progress, test result, diff, trace, and receipt. Reconciliation shows the result under the work order. No merge, publish, deployment, email, or cloud action follows automatically. Arbitrary project repositories and continuation of a prior CLI session are later work.

The app process never runs a coding CLI. The worker uses a disposable Git repository and an exclusive attempt claim. It does not retry a claimed run. A timeout or missing terminal result is recorded as unknown. The fixed test and final diff are checked by the worker; the operator reviews the result before applying it elsewhere.

## Trust boundary

This mode runs the coding CLI as the operator on the local host. It can use that user's CLI credential store and host filesystem/network permissions. It is a trusted local session, not OS isolation or independent receipt attestation. The activity view must not describe it as isolated. OneFoundry does not copy the CLI credential into its Models account or the work-order record.

Codex's CLI login is cached under `CODEX_HOME` (normally `~/.codex`) or an OS credential store; OneFoundry's Models-page Codex connection is a separate store. [OpenAI authentication](https://learn.chatgpt.com/docs/auth) and [Hermes's Codex runtime guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/codex-app-server-runtime.md) describe that distinction. The worker uses `codex exec --json --sandbox workspace-write` for bounded noninteractive work, following [OpenAI's noninteractive guide](https://learn.chatgpt.com/docs/non-interactive-mode). Claude Code runs in print mode with an explicit tool allowlist and turn bound.

## Readiness and ownership

The host worker owns `capabilities.json` in the shared external spool. It reports `mode=trusted_local_cli_v1`, `isolated=false`, fresh observation time, and per-adapter readiness. The app accepts only that exact mode when `EXTERNAL_CODE_ENABLED=true`. Installed CLI presence and a credential file alone are insufficient; a live run must produce a provider session, terminal event, saved receipt, and work-order readback.

The app and worker share only the external job/receipt spool. The opt-in Compose override gives app UID 10001 the operator's shared group for that bind directory; files are group readable and neither CLI login directory is mounted into the app. The worker runs as the operator on the host and uses that operator's CLI stores. The installed app uses this override and an enabled user service for the host worker. Its private local-source Compose override remains mounted. The old external observer is stopped.

## Acceptance

1. Focused unit tests cover command construction, timeout/unknown, malformed terminal events, one-shot claim, and receipt readback.
2. The repository's `make check` passes, including disposable PostgreSQL and frontend checks.
3. A disposable app stack and host worker completed live calculator repairs with Codex and Claude Code. Both saved a provider session, verified diff and trace, failed baseline check, passed final check, and `done` work order. The Task Workspace showed progress and expandable actions at desktop and mobile widths.
4. The installed app reports both adapters eligible. Its first Codex attempt became `unknown` at the original 30-second configured wall limit; no automatic retry occurred. A new audit work order with a 60-second limit finished with a provider session, failing baseline, passing final check, verified diff and trace, and `done` readback. The Task Workspace showed progress and expandable observed actions at desktop and 390px mobile width. No automatic merge or deployment occurred.
