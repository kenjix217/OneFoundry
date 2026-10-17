# Input attachments for projects, work orders, and Task chat

Status: implementation in the shared checkout, 2026-09-28. Current source covers local upload, extraction, text and OpenAI/Codex image turns; original file staging into coding-runner sandboxes remains open. Live provider image acceptance and regular-app deployment remain separate gates.

## Purpose and scope

An operator can choose files while creating a project, while creating a work order in either Setup or Work orders, and while composing a Task chat message. The files become project-owned inputs, not agent-produced artifacts. A saved file is useful only when OneFoundry can show whether its contents were extracted, which exact parts were supplied to the assigned agent, and whether the selected model can receive any visual content. Upload alone never calls a provider.

This design assumes “task” means an individual Task chat message. It uses staged format support: modern documents and common images first, then legacy Office conversion. These assumptions await the operator's answer.

## Existing owners and new boundary

- `ProjectStore` and `ToolGateway` own project and work-order creation. `TaskConversationStore` owns exact chat submissions and unknown-outcome handling. `LiveTextStore` and `TextRequest` currently send text only.
- `ArtifactWorkspace` is a 64 KiB UTF-8 output-artifact store. It must not be repurposed for untrusted input files.
- A new project-scoped input-file store owns original bytes in a dedicated private persistent volume. PostgreSQL owns immutable file identity, SHA-256, and byte size; explicit processing status and extraction versions; and links to project context, work orders, and exact chat turns. Every row carries project and actor IDs. File names are display metadata, never paths.
- The API streams into a bounded temporary file, verifies actual type against an allowlist, hashes bytes, and atomically installs the original. A restricted Python document worker with no network extracts content; the API process does not execute Office, HTML, PDF, macros, or external references. Extraction writes bounded page, sheet, slide, or text chunks with source labels. The Tool Gateway records status events without file contents.

## Format contract

| Stage | Formats | Agent-visible representation |
| --- | --- | --- |
| First | `.txt`, `.md`, `.csv`, `.tsv`, `.json`, `.yaml`, `.xml`, `.html` | Validated UTF-8 text; HTML scripts and remote resources are ignored; tables retain row/column labels. |
| First | `.pdf`, `.docx`, `.xlsx`, `.pptx` | Local text extraction with page, sheet, or slide labels. Scanned PDF pages use local OCR. Formulas are never executed, and missing cached values are labeled. Embedded visuals are not described as read unless a verified vision path processes them. |
| First | `.jpg`, `.jpeg`, `.png`, `.webp` | Original image is available only to a provider/model adapter whose image-input contract has been verified. The UI marks other selections “stored, visual input unavailable.” Local OCR can provide labeled text but is not called full visual understanding. |
| Follow-on | `.doc`, `.xls`, `.ppt`, `.rtf`, `.odt`, `.ods`, `.odp` | Convert in an isolated no-network worker with bounded LibreOffice execution, then apply the same extraction and integrity rules. No macro execution. |

Unknown extensions, archives, executable content, macro-enabled Office formats, encrypted files, deceptive MIME/signature pairs, and malformed or excessive decompression fail with an actionable reason. “More formats” means an explicit reviewed allowlist entry and tests, not a wildcard upload.

Proposed initial limits are 25 MiB per file, eight linked files per chat turn or work order, 30 project-context files, and 200 MiB of originals per project. The parser also needs separately validated page, cell, slide, extracted-text, time, and memory limits. These values belong in typed settings, Compose defaults, the sample environment, and boundary tests.

## Operator and agent flows

1. **Create project:** The file picker appears with the project name. Save the project first, upload each selected file under its new project ID, and confirm each attachment by owner readback. If an upload fails, keep the project and show exactly which files need retry; never claim the set was saved. Setup lists project files for later work-order use.
2. **Create work order:** Both creation forms accept files. Save the draft, upload and link files to that exact work order, then read back the links. A partial upload leaves a visible draft with a retry action. Reassignment does not move files across projects.
3. **Task chat:** Upload before Send, then include chosen file IDs and hashes in the `TurnSend` identity. The send transaction binds that exact set to the `client_turn_id` and turn. The same submission ID may be reconciled, but an unknown turn is never automatically resent. A subsequent message may select files again in the same work order.
4. **Dispatch:** Project files are offered as project context; work-order and turn files are shown with them. For text inference, a local deterministic search selects labeled chunks relevant to the work-order instructions or latest chat message within the existing prompt limit. Readback shows the included chunks and omitted count. Include the first labeled chunk of each selected file and then rank additional chunks by PostgreSQL full-text search; if the remaining budget cannot fit even the first chunk, block before provider egress. The operator can choose specific sections when selection is insufficient. The system blocks a send if a selected file has no usable representation; it never silently drops it. Visual parts use an explicitly supported provider adapter and a separate size/cost preflight. No provider file upload happens during project or work-order creation.
5. **External coding:** A later slice stages only selected, verified files read-only into the disposable runner and records hashes in the launch manifest and receipt. The app process never launches a coding agent or hands it a host filesystem path.

File content is untrusted task data, never instructions with authority. Extracted text is checked for the existing secret patterns before permanent installation and again before provider egress. A rejected temporary original is removed. Visual egress requires explicit file selection plus local OCR preflight; OCR's limits must be shown because it cannot prove an image has no hidden secrets. No file bytes or extracted text enter events, logs, memory, or metrics. The UI distinguishes stored, processing, ready as text, ready as visual, and failed. Download and preview verify project ownership, path, size, hash, and file identity before returning content.

## Delivery and acceptance

1. Add private input storage, metadata/link schema, bounded upload/readback API, extraction worker, and tests. Include the project picker, both work-order forms, and Task chat picker in this slice so the same contract appears everywhere.
2. Wire labeled extracted chunks into live-text and chat prompts, with exact file snapshots, prompt budget enforcement, and synthetic provider tests. Add verified image parts to the current Codex/OpenAI adapter first; enable other providers only after their own contract tests. Do not label a text-model account as a proven file-capable connection from catalog discovery alone. Slices 1 and 2 together form the first user-facing release; a stored file alone is not presented as agent-readable.
3. Add isolated legacy Office conversion and read-only external coding-runner staging with separate integration proof.

Acceptance covers normal files of every enabled type; empty, corrupt, mislabeled, encrypted, and oversized files; malicious HTML/Office payloads and decompression limits; secret-containing content; cross-project or stale links; interrupted upload; partial create; unknown chat outcome and idempotent retry; exact hashes and source labels in saved dispatch; real PostgreSQL and worker/container tests; and desktop/mobile browser flows. Live provider acceptance is a separate gate and requires an explicitly authorized provider call. Restarting the regular app requires separate authorization.

## Implemented scope and remaining gates

The source implementation includes the creation pickers, owner-scoped upload/readback, extraction worker, text context for live text and coding manifests, exact Task chat selection, and image parts for the OpenAI Responses and Codex account transports. Project images require explicit selection for visual egress; work-order images are part of their assigned order. Original bytes are not yet staged in disposable coding repositories, and download or preview routes and manual section picking are not implemented. Provider image capability for an individual configured model and live provider acceptance remain unverified. The regular app still needs an approved migration and rebuild before these source changes appear on port 8765.
