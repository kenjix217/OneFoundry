# Documentation update plan

**Trigger:** A named behavior change requires edits to user or operator documentation.

**Required inputs:** Exact changed behavior, candidate revision, named docs, and verified runtime evidence.

**Capability preflight:** Trusted `repository_file_v1` reads exact saved files; live SDK cannot receive raw repo text through that tool. There is no role tool to edit a checkout or open a PR.

**Procedure:** Compare supplied claims to implementation and tests, identify stale commands/defaults/examples, propose precise wording per file, and order changes by user impact. Mark claims lacking runtime evidence for verification.

**Output:** Return a file-level patch plan and review checklist. Do not claim docs were edited or merged.
