# OneFoundry frontend

React and TypeScript UI for the localhost OneFoundry project, profile, and manual
workflow setup. Project work orders can be assigned, made ready, and started as
manual planning runs. The activity view shows the full work-order event timeline,
including pre-run steps, actor, time, task context, zero usage, and server-side
search. Handoffs and outcomes update recorded state. The approval inbox stores
outbound proposals and approve/reject decisions only; it never dispatches them.
The Memory view accepts one operator-entered result per done run, stages a
deterministic DreamCycle candidate, and lets the operator approve or reject it.
Approved memory can be browsed or searched with exact source IDs. Project memory
stays in its project; company memory is visible from other projects. Search
scores count local lexical term matches. Scope selection is sent to the backend
before its result limit, so older project items remain reachable when recent
company items fill the default page. No model or embedding call is made.
The production build is served by the OneFoundry backend on the same origin as
`/api`. It does not load scripts, styles, fonts, or images from a CDN.

```sh
npm ci
npm run check
```

`npm run check` runs ESLint, Vitest, TypeScript, and the Vite production build.
`npm run dev` starts Vite on loopback for UI development. Its `/api` proxy
defaults to `http://127.0.0.1:8765`; set `ONEFOUNDRY_API_PROXY_TARGET` to an
alternate local backend address. The integrated backend build is the supported
path for testing writes because it presents the exact Host and Origin that
OneFoundry checks on write requests.

Browser check: create a project and confirm Setup opens. Follow Team, Models,
Work orders, and Activity from Setup; return to Setup and use Refresh saved
progress to confirm it follows persisted profile instructions, work orders, and
runs. Model accounts are optional for local synthetic runs.

Skill Library browser check: open a project → Skill Library → create a procedure
→ add a SKILL.md version → expand Version 1 to inspect its content and SHA-256.
In Team, edit a profile and select that procedure and exact version. Return to
Skill Library and append Version 2; reopen the profile to confirm it still shows
Version 1. Select Version 2 explicitly to replace the future-run binding. Open
a run in Activity to inspect the pinned procedure name, version, and hash saved
at assignment. Procedure text does not grant tool access; allowed tool IDs and
Tool Gateway admission remain separate.

Models, autonomy levels, and tool IDs remain stored metadata. Manual runs do
not call a model, coding agent, provider, or outbound tool.
