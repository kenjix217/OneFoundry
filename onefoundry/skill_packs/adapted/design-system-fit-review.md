# Design system fit review

**Trigger:** A proposed UI change should be checked against an existing product design system.

**Required inputs:** Product-specific tokens, component inventory, target screens, interaction states, and accessibility criteria.

**Capability preflight:** OneFoundry does not inherit external design-system colors or components. Live role agents cannot inspect rendered UI; static fixture inspection is approval-bound and returns bounded text only.

**Procedure:** Compare supplied UI decisions with the product’s own spacing, typography, status, focus, disabled, and error conventions. Identify reuse candidates and mismatches with exact references.

**Output:** Return a design-system review and visual checks for a human/QA browser pass. Do not claim a rendered design was inspected.
