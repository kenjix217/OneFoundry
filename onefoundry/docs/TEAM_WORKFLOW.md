# Team workflow candidate

Opening a task reads saved state. Mark ready admits the work order; Enable team
session explicitly cuts over a main legacy session while preserving its history.
Neither action queues a turn. Continue work queues one bounded SDK turn for the
selected agent. The worker must be configured and enabled; the readiness panel
reports blockers. Preserved legacy child drafts are historical and are not
silently launched. The Chief can create fresh scoped assignments with dependencies.

`report_result` exposes the assignment's exact evidence contract: body only for
text reports, body plus saved artifact/run IDs for artifacts, and body plus a
verdict for independent QA. Incompatible fields return a recorded failure with
a safe correction step. The existing SDK can correct that input within the same
bounded turn; ownership, dependency, scope and exact-byte checks still apply.

A saved artifact must be read back through its run owner. QA reviews the exact
hash and upstream evidence. QA rejection keeps failed work orders visible. Chief
completion submits for operator review; Return for revision queues nothing, and
Accept closes the submitted sessions and work orders. Outbound approvals stay
separate. Cancellation stops only queued branches; active execution and uncertain
actions require reconciliation first.

Unknown attempts stay unknown. Reconcile saved actions before using **Abandon
unverified result** with a reason. This records an operator decision, preserves
history, and queues nothing; it never certifies success or replays an old action.
A later Continue is a distinct attempt. Pending approvals or active turns block
abandonment. Installation requires migration 0074 and the source-declared narrow
`task_sessions.execution_mode` update grant; this candidate does not apply either.

## Optional local video

`request_video_capture_v1` requires the profile tool permission and
`MEDIA_CAPTURE_ENABLED=true` (default true; explicit false disables requests). The agent requests a recording; the
operator selects an approved app window in a browser permission prompt, optionally
records microphone narration, trims one clip and burns one requested caption.
Bytes remain in the browser until **Save reviewed video locally**. A saved video
is owned by the exact project/run, bounded, hashed and immutable. Open Results,
watch the saved video, then explicitly verify playback of its hash. Independent
QA and operator work-order acceptance remain separate. QA receives the caption,
metadata and operator playback decision; this text-model path does not inspect
video frames itself.

No autonomous browser recording, paid voice generation, multi-clip editing or
publishing is implemented here. A real DistrictAI demo still needs approved
source-screen capture and narration, playback evidence and independent QA. The
synthetic fixture is only a local codec and ownership test.

| Setting | Default | Valid range / behavior |
| --- | --- | --- |
| MEDIA_CAPTURE_ENABLED | true | Boolean; operator selects and starts each recording |
| MEDIA_MAX_BYTES | 33554432 | 1024–67108864; enforced on upload and readback |
| MEDIA_MAX_DURATION_SECONDS | 120 | 1–300; browser recording/editor and upload-intake deadline |
| MEDIA_MAX_WIDTH | 1280 | 320–1920; browser capture/render bound |
| MEDIA_MAX_HEIGHT | 720 | 240–1080; browser capture/render bound |
| MEDIA_FRAME_RATE | 24 | 1–30; browser encoder/render rate |

The browser enforces duration/dimensions/frame rate. The backend identifies WebM
containers and verifies exact bytes; container identification is not a decoding
or playback claim. Invalid settings fail startup. Existing Settings precedence
is unchanged: explicit initialization overrides environment, then dotenv, then
validated defaults. Compose and `.env.example` expose all media settings.

Canceled branches remain in the timeline. Once every remaining assignment in a plan revision has a confirmed result, the Chief receives the synthesis turn automatically. An empty or wholly canceled revision does not supply completion evidence; blocked or unfinished assignments still prevent synthesis.


## Long goals and turn bounds

Team sessions have no lifetime total-turn cutoff. The 30-turn ceiling in the live acceptance driver belongs to that test authorization, not the product. `TASK_CONVERSATION_MAX_TURNS` bounds recent context and each older-chat history page; it never stops new turns. Task chat loads earlier saved turns using **Load earlier turns**. The conversation-ID read supports `before_sequence` and reports `latest_sequence` plus `turns_omitted`. Exact client-ID lookup and replay still work outside the recent page. The Task screen shows the latest 500 entries of each history type and reports omitted entries; complete history remains stored. Unacknowledged unknown attempts and unsettled action receipts stay visible even outside the recent window.

`TEAM_MAX_MODEL_CALLS_PER_TURN` defaults to `unlimited`; the existing Agents SDK owns the tool loop with no arbitrary model-call cutoff. An explicitly configured finite limit (2–30) remains supported; reaching it records a failed attempt, not goal completion. `TEAM_TURN_TIMEOUT_SECONDS` (300), usage policy, cancellation and approval boundaries still apply. A timeout or interrupted worker records an unknown outcome that requires reconciliation before a separate continuation. Restart resumes persisted queued work; it does not replay an uncertain claim. Idle polling queues no model calls.

Automatic Chief decisions pause after 6 decisions without new confirmed result evidence or an explicit operator checkpoint. Distinct text result digests, artifact bytes, and QA verdicts about distinct artifact bytes advance the evidence checkpoint. Repeated results with fresh receipt IDs do not. This is recorded progress, not a semantic quality assessment; declared evidence checks and final operator acceptance still apply. A pause is saved as waiting for the operator; Reply or Continue work starts another bounded window. Other already queued agents may still run.

Artifact reports retain the full exact owner-read content, within the artifact owner's byte limit, alongside a separately validated short summary. The text-report message limit does not reject this derived evidence envelope.

The assignment limit (8) bounds unfinished work, including blocked/input/approval waits. Satisfied or canceled assignments release capacity without deleting their history. Successive batches in the same plan get fresh synthesis causes, coalesced while pending. Delegation depth (2), admitted plan revisions (3), per-attempt limits, one provably safe retry, business-workflow usage/elapsed budgets and outbound approvals remain enforced. Unlimited task length does not grant unlimited spending. New work orders retain their existing legacy mode; team activation requires readiness and the worker flag, imports history, and queues no work until the operator sends or continues.

Task and Manage refresh the work-order snapshot when execution readiness reports a changed status. Background completion and cancellation update the header and operator review controls without requiring a page reload.
