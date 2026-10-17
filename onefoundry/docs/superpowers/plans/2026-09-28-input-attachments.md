# Input Attachments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator attach common files during project creation, work-order creation, and Task chat, and prove what an assigned agent received.

**Architecture:** A dedicated project-scoped input store retains verified originals and extracted, labeled chunks. A no-external-network document worker handles untrusted formats. Creation forms upload after owner creation; chat binds exact file IDs to an idempotent turn. Provider requests contain bounded extracted context and verified visual parts.

**Tech Stack:** Python 3.12, FastAPI, PostgreSQL/Alembic, Docker Compose, React/TypeScript, uv, Pytest/Vitest.

**Spec:** `/home/ken/KJ31/onefoundry/docs/INPUT_ATTACHMENTS_DESIGN.md`

## Global Constraints

- Preserve the unrelated dirty checkout and the existing Task chat fix. The user has forbidden a separate worktree. Do not commit, push, migrate the regular database, restart the regular app, or invoke a live provider without action-specific authorization.
- The existing `ArtifactWorkspace` remains output-only; input bytes get a separate owner and persistent volume. Every saved row carries project and actor IDs; originals have exact paths, SHA-256, and byte sizes.
- Upload makes no provider call. Files are untrusted task data and do not grant instructions, tools, or permissions. No contents enter events, logs, memory, or metrics. Unknown chat outcomes never trigger automatic resend.
- Proposed defaults: 25 MiB per file, eight linked files per chat turn or work order, 30 project files, 200 MiB project originals. Put changed limits in validated settings, Compose, `.env.example`, docs, and tests.
- The first user-facing release requires both intake and agent-readable context. Legacy Office and external coding are the follow-on slice approved in the staged design.

## Review Focus

1. A selected unreadable or oversized file must block dispatch before provider egress, with its name and reason shown. Test in Tasks 3 and 5.
2. A file ID from another project or a stale work-order assignment must never enter a prompt or runner. Test in Tasks 3 and 7.
3. Two retries with the same `client_turn_id` but different file sets must conflict, and an unknown turn must not resend. Test in Task 5.
4. Files with deceptive signatures, zip bombs, external Office relationships, or secret patterns must not be installed as ready context. Test in Tasks 1 and 2.
5. A project or work order created before a failed upload remains visible with an exact retry path, never a false “all saved” notice. Test in Task 4.

---

### Task 1: Input bytes, settings, and owner schema

**Files:** Create `src/onefoundry/input_file_bytes.py`, `src/onefoundry/input_file_schemas.py`, and `migrations/versions/0035_input_files.py`; modify `src/onefoundry/settings.py`, `compose.yaml`, `.env.example`, `Dockerfile`, and `scripts/check_postgres.sh`; test `tests/test_input_file_bytes.py`, `tests/test_input_files_postgres.py`, and `tests/test_settings.py`.

**Interfaces:** Produce `InputFileBytes.write_temp/read_verified/remove`, project-scoped `input_files`, `input_file_chunks`, `input_file_links`, and typed settings. The DB links use foreign keys to projects, work orders, conversations, and turns; no free-form owner path.

- [ ] Write tests for private atomic storage, size/hash/path checks, typed limits, project ownership, and migration upgrade/downgrade; run them and observe the missing behavior fail.
- [ ] Implement the storage owner, schema, config, and test-stack volume; run the focused tests green.
- [ ] Verify `ruff`, `mypy`, migration in disposable PostgreSQL, and `git diff --check`.

### Task 2: Bounded extraction worker

**Files:** Create `src/onefoundry/input_file_types.py`, `src/onefoundry/input_file_extract.py`, `src/onefoundry/input_file_worker.py`; modify `Dockerfile`, `compose.yaml`, `pyproject.toml`, `uv.lock`; test `tests/test_input_file_extract.py` and `tests/test_input_file_worker_postgres.py`.

**Interfaces:** Consume pending `input_files` and temporary bytes from Task 1. Produce verified `ready_text`, `ready_visual`, or typed rejection plus labeled chunks and extractor version. Worker has an internal DB network only, no external egress or model credential mount.

- [ ] Write and fail tests for text/HTML/CSV/JSON/YAML/XML, PDF text/OCR, DOCX, XLSX, PPTX, JPEG/PNG/WebP, corrupt and misleading files, decompression limits, external references, and secret rejection.
- [ ] Implement parsing with reviewed locked dependencies and bounded page/cell/slide/time/memory limits; record labels and never execute macros, formulas, scripts, or remote references.
- [ ] Run focused tests and a disposable worker/container check green.

### Task 3: Upload, link, readback, and context API

**Files:** Create `src/onefoundry/input_file_store.py`, `src/onefoundry/input_file_api.py`, `src/onefoundry/input_file_context.py`; modify `src/onefoundry/main.py`, `src/onefoundry/tool_gateway.py`; test `tests/test_input_files_postgres.py`.

**Interfaces:** `POST /api/projects/{project_id}/input-files` streams one bounded file; list/link/read/preview routes return validated owner-scoped metadata. `select_context(project_id, selected_ids, query, byte_budget)` returns labeled chunks, selected hashes, and omission counts or a typed preflight error.

- [ ] Write and fail API tests for normal upload/readback, wrong project/work order, partial stream, unsupported type, duplicate content with distinct owners, status transitions, and cross-project access.
- [ ] Implement Tool Gateway orchestration, owner readback, typed errors, and deterministic PostgreSQL full-text chunk selection with first-chunk coverage.
- [ ] Run focused PostgreSQL tests green; verify no provider call on upload.

### Task 4: Creation surfaces and file picker

**Files:** Create `frontend/src/inputFileApi.ts` and `frontend/src/InputFilePicker.tsx`; modify `frontend/src/App.tsx`, `frontend/src/SetupView.tsx`, `frontend/src/WorkOrdersView.tsx`, and their tests.

**Interfaces:** File picker uploads after project/work-order create, lists saved states, and exposes exact retry for partial failure. All three creation surfaces use the same API and component.

- [ ] Write and fail frontend tests for create with files, partial upload, retry/readback, invalid files, and mobile-safe status display.
- [ ] Implement shared picker and the three creation flows; run focused frontend tests, lint, and build green.

### Task 5: Exact Task chat attachments and text dispatch

**Files:** Modify `src/onefoundry/task_conversation_schemas.py`, `task_conversation_store.py`, `live_text_store.py`, `inference/contracts.py`, `frontend/src/TaskChatView.tsx`, `frontend/src/conversationApi.ts`; test `tests/test_task_conversation_postgres.py`, `tests/test_live_text_postgres.py`, and `frontend/tests/taskChat.test.tsx`.

**Interfaces:** `TurnSend.attachment_ids` becomes part of exact submission identity. Prepared text requests contain selected labeled chunks and saved file hashes; saved turn/run readback identifies used and omitted sections. Unsupported or too-large selection fails before egress.

- [ ] Write and fail tests for same-ID/same-files idempotency, same-ID/different-files conflict, unknown outcome, prompt provenance, work-order and project context, oversized context, and stale owner.
- [ ] Implement transactional binding, context preparation, and frontend selection/readback; run focused PostgreSQL and frontend tests green.

### Task 6: Visual inputs and provider capability

**Files:** Modify `src/onefoundry/inference/contracts.py`, `inference/subscription_codex.py`, `inference/responses.py`, model capability readback in `src/onefoundry/model_gateway.py`, `frontend/src/TaskChatView.tsx`, and `frontend/src/WorkOrdersView.tsx`; test transport unit, PostgreSQL preflight, and frontend tests.

**Interfaces:** A typed image part carries exact MIME, SHA-256, and bounded bytes. Codex/OpenAI adapters encode it using their documented image-input shapes. Other providers reject visual selection before egress until separately verified; no catalog text category implies image support.

- [ ] Write and fail tests for image wire shape, size and MIME rejection, provider preflight, no silent downgrade to OCR, and exact visual-use readback.
- [ ] Implement only proven adapter contracts and explicit unverified state; run focused tests green without a live provider call.

### Task 7: Legacy conversion, coding staging, and completion gates

**Files:** Modify the document worker and `execution_gateway.py`/runner manifest owner; add legacy conversion and runner tests; update `README.md`, `PROGRESS.md`, and the configuration reference.

**Interfaces:** Isolated LibreOffice handles `.doc`, `.xls`, `.ppt`, `.rtf`, `.odt`, `.ods`, `.odp` under the same limits. External coding attempts stage selected verified originals read-only in disposable repositories and record hashes in manifests/receipts.

- [ ] Write and fail tests for conversion success/failure, disabled macros, no network, stale/cross-project refs, and runner hash readback.
- [ ] Implement conversion and staged runner context without giving the app process coding execution capability; run focused integration tests green.
- [ ] Run repository `make check`, `git diff --check`, desktop/mobile browser flows against an isolated source app, and exact owner-path readback. Record skipped live provider and regular-app deployment gates separately in `PROGRESS.md`.
