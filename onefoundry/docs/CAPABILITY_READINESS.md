# Capability readiness

The first self-extension slice adds `capability_readiness` to existing work-order
readiness responses. Chief's existing `read_project_status` tool returns the same
scoped observations in bounded pages. Normal agent context includes compact
status/action summaries. The primary conversation's Details and existing Execution status panel show all blockers
and concrete next steps, using repository names rather than internal IDs/hashes;
these observations never start or approve work.

`capability_schemas.py` owns the typed contract and blocker priority.
`capability_inspection.py` owns policy checks and routing to existing dependency
owners. `role_repository_file.inspect_source` uses the file reader's bounded Git
admission and exact branch/revision checks without reading file contents.
Execution Gateway owns fresh coding-runner capability validation. Profile and
source queries remain project scoped, and a foreign profile is rejected.

All independently observed blockers are retained. Disabled policy, absent grant,
missing setup, unavailable dependency and unknown observations take precedence
over `missing_implementation`. Only a check whose final action is `implement`
supports the linked engineering proposal described in `SELF_EXTENSION_RUNTIME.md`.
Stale source selection requires setup; an installed CLI does not prove runner
connection or a supported arbitrary-project target.

`observed_at` marks a preflight observation, not continuing availability. Chief
content pagination retains the original cutoff/digest and rejects changed state.
Use-time owners recheck all permissions and source/runner identities. Sources
outside the bounded inventory, including filtered sensitive metadata, are counted
as omitted and never described as inspected. Resolve those in Project sources.

## Feature audit and defaults

Python settings, Compose and the sample agree. Precedence remains explicit
constructor values over environment over defaults; explicit false is respected,
and empty/invalid booleans fail validation. Existing private installation values
are retained during upgrades. A source default change is not an installed update.

| Setting / owner | Packaged default | Installed audit (0.13.0, 2026-10-13) | Remaining prerequisite |
| --- | --- | --- | --- |
| TEAM_WORKER_ENABLED | true | true | Admitted task and connected profile/model |
| TEAM_REPOSITORY_READ_ENABLED | true | true | Exact read-only source mount, saved revision, profile grant |
| EXTERNAL_CODE_ENABLED | true | true | Fresh isolated runner and exact approved target; new target exists in source only |
| PUBLIC_WEB_ENABLED | true | true after explicit approval | Researcher tool grants; search service endpoint for search/follow-up fetch |
| MEDIA_CAPTURE_ENABLED | true | true | Operator selects source and starts each local recording |
| BROWSER_STATIC_ENABLED | false | false | Pinned browser image and separate host bridge |
| VIDEO_EDIT_ENABLED | false | false effective Python default | Pinned renderer image and separate worker/host bridge |
| OFFLINE_SOURCE_PROBE_ENABLED | false | false | Optional offline probe supervisor; not ordinary team work |
| REMOTE_CHANNEL_NETWORK_ENABLED | false in Python, true in Compose/sample | true | Explicit paired personal channel; no automatic pairing |
| COORDINATION_MODE | off | auto | Operator-authored rule and existing launch admission |
| ONEFOUNDRY_BUSINESS_MODE | off | off | Separate optional historical business workflow; not the native team worker |
| MEMORY_SEARCH_MODE | lexical | hybrid | Pinned native assets/index; approved-fact retrieval still scoped |
| MAIL_IDLE_ENABLED | true | true | Explicit mailbox connection |

The typed inventory covers worker/web/recording policy, renderer/browser policy,
search configuration, selected source reads, isolated coding, approved maintenance
targets and task-requested tool registration/admission.
Policy checks confirm the enable gate only; role admission and actual execution
remain separate. Memory capture/publication, connectors and automation already
have dedicated owners; this slice does not replace their readiness contracts.
Conversation capture remains off by default, and outbound approvals remain intact.

The 2026-10-13 installed 0.13.0 audit has no repository bind, no repository-file profile grants
and no connected app coding runner. Search has no configured endpoint. These are
visible setup/implementation gaps, not reasons to silently flip an unrelated switch.
Browser/video/probe switches remain explicit setup requirements because enabling
them without their required isolated dependencies would misrepresent availability.

The approved 0.14.0 local update on 2026-10-13 installed the maintenance target and
linked self-extension contracts. Runtime readback confirms all five core flags
remain true. Existing mounts, profile grants and connection configuration were
preserved; the isolated worker, search service and source access still need setup.
Actual installed desktop/mobile Details show these prerequisites. See `PROGRESS.md`
for installed identity, checkpoint and acceptance evidence.

Focused checks: `pytest -q tests/test_capability_inspection.py tests/test_core_feature_defaults.py`.
The existing PostgreSQL Chief/start/source tests cover scoped HTTP and SDK reads;
frontend contract/render and primary-conversation tests are in
`frontend/tests/capabilityReadiness.test.tsx` and `chiefWorkspace.test.tsx`. Run `make check`
and real desktop/mobile acceptance before installing this slice.
