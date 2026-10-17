# Paperclip workflows and remaining skill packs

This review covers the ten previously unadapted Paperclip workflow sources, 38 Paperclip skills, 50 Codex visual/document/template sources, and 35 Codex engineering/security sources. Every selected source now maps to an original OneFoundry procedure. The exact source IDs and status are in `source_inventory.json`; procedure names and file mappings are in `adapted/manifest.json`. These are procedures, not tools or permissions. A procedure with a missing capability produces a preparation or blocked result. Nothing here installs a scheduler, renderer, coding runner, security scanner, or connected app.

## Paperclip workflows and 38 skills

All eight team/task templates and four workflow references now inform OneFoundry procedures. The two engineering templates were already mapped to `manual-engineering-review.md`. The other ten inform `first-priority-review.md`, `design-work-review.md`, `content-work-review.md`, `artifact-handoff.md`, and `skill-assignment-review.md`. Priority, design, and content reviews start on operator request; the Paperclip routine and workflow references do not create OneFoundry recurring runs or Paperclip host actions.

The first two Paperclip sources mapped to `contribution-review.md` and `release-changelog-draft.md`. The other 36 now have one source-specific procedure each, with source-neutral filenames and instructions. Each states what OneFoundry can prepare and which external action requires a separate OneFoundry tool owner.

| Capability state | Sources with OneFoundry procedures | Needed for source's full workflow |
| --- | --- | --- |
| Related existing guidance plus source-specific intake | `create-paperclip-bundled-skill`, `doc-maintenance`, `release-changelog-discord-message` | Paperclip catalog mutation, repository edits, or channel publication are separate actions. |
| Paperclip host specific | `company-creator`, `create-agent-adapter`, `create-issue-interaction-ui`, `garden-inbox`, `paperclip-create-plugin`, `paperclip-dev-workspace-run-verify-fix`, `release`, `design-guide`, `paperclip-task-bridge`, `status-card-query`, `paperclip-capsules`, `skills-releases/paperclip/v0`, `skills-releases/paperclip/v7-roster`, `paperclip-board`, `paperclip` | Paperclip company, issue, heartbeat, adapter, plugin, roster, or release contracts; importing their commands would target the wrong owner. |
| Git, PR, CI, or local execution | `check-pr`, `deal-with-security-advisory`, `pr-gardening`, `prcheckloop`, `prepare-paperclip-pr`, `terminal-bench-loop` | Complete current-head checks/reviews, Git mutation, test execution, private advisory access, or terminal benchmark runner. |
| Wiki and files | `index-refresh`, `paperclip-distill`, `wiki-ingest`, `wiki-lint`, `wiki-maintainer`, `wiki-query`, `para-memory-files` | A governed wiki graph, raw source reads/writes, or agent-owned file memory; OneFoundry currently owns scoped PostgreSQL memory. |
| Browser, publishing, or external connector | `paperclip-page`, `wireframe`, `agent-browser`, `ramp`, `agentmail` | Page host, visual renderer/browser evidence, Ramp, or email connection with approval and readback. |

## Codex visuals, documents, and templates: 50

The first ten visualization concepts have text planning or critique procedures: `visualization-strategy-and-critique`, `statistical-and-uncertainty-visualization`, `gantt-chart-visualization`, `geospatial-and-cartographic-visualization`, `node-link-and-diagram-layout`, `uml-and-software-architecture-visualization`, `dashboards-and-real-time-visualization`, `testing-data-visualizations`, `scrollytelling-and-parallax-data-visualization`, and `reports-pdfs-and-slide-automation`. The other 40 selected sources now each have a source-neutral procedure file. All 20 template pointers have a template intake procedure. None of these procedures alone renders or inspects pixels.

| Capability state | Sources with OneFoundry procedures | Needed for source's full workflow |
| --- | --- | --- |
| Coding and rendering runtime | `canvas2d-data-visualization`, `d3-data-visualization`, `grammar-of-graphics-and-declarative-visualization`, `react-and-nextjs-data-visualization`, `threejs-data-visualization`, `typescript-data-visualization-engineering` | Code workspace, render/build execution, data access, and visual browser acceptance. |
| Router overlaps installed procedures | `data-visualization` | Use the focused visual and accessibility procedures. |
| Document and spreadsheet runtime | `documents`, `pdf`, `presentations`, `spreadsheets`, `deep-research` | File import/authoring, rendering and visual QA; spreadsheet recalculation or cited DOCX where required. |
| Codex or connected host specific | `excel-live-control`, `template-creator`, `browser-use/browser`, `browser/control-in-app-browser`, `chrome/control-chrome`, `sites-building`, `sites-hosting`, `visualize` | Live host session, browser, Sites, interactive response, or Codex personal template installation. |
| Reference artifact pointers, not independent text templates | `artifact-template-design-report`, `artifact-template-experiment-analysis`, `artifact-template-investment-committee-memo`, `artifact-template-legal-memorandum`, `artifact-template-minimal-letterhead`, `artifact-template-strategy-memorandum`, `artifact-template-system-design`; `artifact-template-analytics-dashboard`, `artifact-template-financial-budget`, `artifact-template-operating-calendar`, `artifact-template-project-tracker`, `artifact-template-sales-pipeline`, `artifact-template-three-statement-forecast`; `artifact-template-business-review`, `artifact-template-market-trends-report`, `artifact-template-operating-review`, `artifact-template-project-kickoff`, `artifact-template-simple-dark-mode`, `artifact-template-simple-light-mode`, `artifact-template-team-alignment` | Retained DOCX, XLSX, or PPTX source artifacts must be imported/cloned and visually checked; the pointer text alone cannot produce the intended template. |

## Codex engineering and security: 35

The first three procedures cover bounded concepts from seven sources: `systematic-debugging` and `verification-before-completion` → `engineering-defect-diagnosis.md`; `assess-patch-risk` and `review-agent` → `immutable-patch-risk-brief.md`; `triage-finding`, `threat-model`, and `attack-path-analysis` → `provisional-security-finding-triage.md`. The other 28 sources now each have a source-neutral procedure file. They use supplied evidence and admitted saved owner records. They cannot make a complete repository or security scan claim.

| Capability state | Sources with OneFoundry procedures | Needed for source's full workflow |
| --- | --- | --- |
| Codex Security workbench | `security-scan`, `security-diff-scan`, `deep-security-scan` | Scan coordinator, canonical coverage/findings artifacts, source/diff reads, and independent workers. |
| Complete source, tests, or coding actions | `define-security-policy`, `finding-discovery`, `fix-finding`, `validation`, `verify-fix`, `vulnerability-writeup` | Complete revision-bound source/contract reads, controlled tests, source-to-sink validation, and an approved coding path where a fix is needed. |
| Connected tracker write | `track-findings` | Issue/advisory connector with identity, duplicate check, approval, dispatch, and readback. |
| Source-specific preparation with related existing guidance | `brainstorming`, `receiving-code-review`, `test-driven-development`, `writing-plans`, `writing-skills` | Full implementation remains an external coding-provider concern. |
| Codex host orchestration or Git | `diagnosing-superpowers`, `dispatching-parallel-agents`, `executing-plans`, `finishing-a-development-branch`, `requesting-code-review`, `subagent-driven-development`, `using-git-worktrees`, `using-superpowers` | Codex host agents, workspace ownership, test runner, Git mutation, and branch integration. |
| Local Codex host capability | `playwright`, `imagegen`, `openai-docs`, `plugin-creator`, `skill-installer` | Real browser, binary image artifacts, current official docs, or approved plugin/skill installation. |

The remaining full-procedure gaps are concrete Tool Gateway or runner work: complete revision-bound repository and diff reads, trusted tests, real browser and screenshot evidence, file rendering, a wiki owner, selected connected app contracts, approval-bound writes, and Codex Security scan orchestration. New immutable tool IDs need saved and current profile admission. A procedure alone cannot supply any of these.
