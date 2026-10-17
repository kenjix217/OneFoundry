# AgentPod local pilot — final report

## What works

AgentPod runs on loopback with projects, built-in and custom profiles, work orders, handoffs, an approval inbox, append-only run events, reviewed company/project memory, a fixed synthetic coding runner, local Sales previews, and read-only Infra status. Researcher → GTM uses the OpenAI Agents SDK with a scripted local model. Engineer → QA uses a disposable repository and a runner with no network. These are controlled fixtures, not live AI agents.

## Repeat the browser checks

Start from the commands in [README](README.md), then open [AgentPod](http://127.0.0.1:8765/).

1. **Custom profile:** Create a project → **Team** → **Add a profile** → save a name, model ID, and instructions. Open **Work orders**, create a draft assigned to that profile, and see the assignment. Its permissions are stored metadata, not executable authority.
2. **Research → GTM → Sales:** Create a Researcher work order → **Mark ready** → **Run synthetic text fixture**. **Activity** shows separate Researcher and GTM outputs, a handoff, source memory citations, actor/time, and zero provider cost. Click **Create local Sales draft**. In **Approvals**, approve the exact local preview; back in **Activity**, confirm delivery. Open AgentPod Mailpit on the configured loopback `MAILPIT_UI_PORT` to see its subject. Reject a separate draft and confirm the Mailpit message count does not rise.
3. **Engineer → QA:** Create an Engineer work order → **Mark ready** → **Run synthetic Engineer → QA fixture**. In **Activity**, use **Check runner receipt** twice. See the test fail before the repair, pass after it, QA pass, verified diff, artifact hashes, and zero provider cost. Click **Request synthetic merge approval** → **Approvals** → **Approve and queue local merge** → return to **Activity** → **Check runner receipt**. The merged commit and observed target HEAD must match. Rejecting a separate proposal queues no merge.
4. **Memory:** In **Memory**, choose the delivered Sales run and the passing QA run under **Stage a completed run**. Stage and **Approve to memory** each candidate. A rejected Sales delivery is refused. Start a later Researcher fixture with words from both results; **Activity** lists both source-linked citations.
5. **Infra:** Start the host observer from the command in [README](README.md), then open **Infra**. It shows the app, DB, runner, and Mailpit. When the AgentPod runner alone is stopped, the view changes to degraded; after restart, it returns to healthy.
6. **Run history:** In a new project, edit Engineer's model and instructions in **Team**. Create and ready an Engineer work order, then start a manual run. In **Activity**, see the task and **Profile history**. Change Engineer's model and instructions in **Team**, return to the same run, and see its original settings preserved.

## Verification and limits

`make check` passed Ruff, strict mypy, 86 local Python tests, 45 disposable PostgreSQL tests, 40 frontend tests, and frontend build. Migration 0011 was applied to the mounted app; readiness returned ready. New runs save their initial task and each assigned profile's settings. The database rejects updates and deletes of profile snapshots; older runs display unavailable historical values instead of current settings. The mounted browser confirmed an Engineer run retained its original model and instructions after a profile edit. Live wrong Host and write Origin requests returned 400 and 403. The app and Mailpit UI publish only to 127.0.0.1; PostgreSQL has no published port; the runner has no network. Desktop and mobile browser checks had zero console errors and no horizontal overflow at 390px or 320px. Screenshots: [Step 8 desktop](../output/playwright/agentpod-step8-desktop.png), [Step 8 mobile](../output/playwright/agentpod-step8-mobile.png), [Step 7 desktop](../output/playwright/agentpod-step7-desktop.png), [Step 7 mobile](../output/playwright/agentpod-step7-mobile.png). A Step 7 pattern scan found zero obvious credential signatures in 103 event payloads, four memory items, 115 spool files, and service logs; it cannot prove the absence of every possible secret.

One early approved Sales preview reached Mailpit but its app state stayed `unknown` because Mailpit normalized line endings. The readback was corrected and a new approval completed successfully. The unknown approval was never resent.

Malformed Mailpit readback now leaves a delivery `unknown` with a terminal result event and no automatic resend. The behavior is covered by focused shape tests; malformed responses were not induced in the mounted Mailpit service.

Live provider execution, Researcher web search, arbitrary repositories, Codex/Claude CLI, real email, external merges, cloud monitoring, provider spending limits, and production deployment remain unproven. No provider credential or numeric spend budget was supplied for a live run. **Total AI provider spend: $0.** AgentPod sent only approved synthetic previews to its local Mailpit; no real email, DistrictAI change, remote push, or production action occurred.

## Step 8: frozen Go code

Keep `api/`, `worker/`, and `web/` for now. AgentPod's current Compose runtime and CI build from `agentpod/` without them, but legacy systemd units, deployment preflight, and top-level tests still reference those trees. Python covers the selected local workflows; it has not replaced the Go provider, general tool, artifact, or integration routes, nor the legacy memory/indexing behavior. Removing the trees would break those documented paths and needs a separate owner decision after installed-use inventory and parity checks. Nothing was deleted.

## Paperclip design review

The local MIT-licensed Paperclip checkout offers useful **concepts** for a later AgentPod slice: a guided project launch checklist, explicit work-order ownership and blocked/handoff states, a versioned skill library distinct from profile tool permissions, and a scan-friendly run outcome view. Sources inspected include [project setup](/home/ken/paperclip/skills/paperclip/references/workflows.md), [task workflow](/home/ken/paperclip/docs/guides/agent-developer/task-workflow.md), [skill guidance](/home/ken/paperclip/docs/guides/agent-developer/writing-a-skill.md), and [run ledger](/home/ken/paperclip/ui/src/components/IssueRunLedger.tsx). AgentPod should express these in its existing green operator-console style with its own layout and copy. No Paperclip code or visuals were copied; substantial future copying would retain its MIT notice and check imported skill provenance.
