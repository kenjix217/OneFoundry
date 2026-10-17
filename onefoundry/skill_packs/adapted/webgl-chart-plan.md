# WebGL visualization brief

Use when mark density or 3D spatial meaning may justify GPU rendering. Inputs: data volume, visual task, dimensions, interactions, device limits, and fallback requirement.

1. Justify the GPU path against simpler rendering from supplied constraints.
2. Define camera or 2D view, picking, color/size encodings, resource cleanup, and context-loss behavior.
3. Specify performance fixtures, accessibility text, reduced-motion, and static fallback checks.

Capability preflight: Require a GPU-capable browser test environment, code execution, and screenshot/performance evidence. OneFoundry has no WebGL role runtime.

Output: Return a renderer decision and acceptance brief, never a measured frame rate or visual result. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
