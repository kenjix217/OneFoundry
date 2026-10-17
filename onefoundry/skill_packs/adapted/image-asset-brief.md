# image asset brief

Trigger: The operator requests a new or edited image for a project artifact or UI.

Inputs: Intended use, subject, style, dimensions, source image rights when editing, destination, accessibility text, and review criteria.

Capability preflight: Check for an admitted image-generation provider, approved spend and egress, binary artifact storage, provenance and hash readback, and permission to place the asset. OneFoundry currently has `write_text_artifact_v1` for text/Markdown/CSV, not image generation or binary files; an external `image_gen` capability is a host tool, not an OneFoundry role tool.

1. Draft a precise prompt and negative constraints from the intended use, avoiding secrets and protected data.
2. Define how the operator will judge legibility, factual accuracy, rights, and accessibility.
3. Name output format, dimensions, intended destination, and collision behavior before generation.
4. If the image tool or binary store is absent, hand off the brief and mark generation unexecuted; do not invent an image path or hash.

Output: An image production brief or `blocked_for_image_tool`. No image is generated, edited, saved, or installed by this procedure alone.
