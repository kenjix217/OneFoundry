# Local video edit

Use for an ordered cut from authorized project recordings, images and audio. This procedure adapts the task-owned DistrictAI production scripts; it never grants tools, browser access or outbound permission.

1. Confirm `edit_video_v1`, `video_edit_status_v1` and cancellation are admitted. Confirm supplied media asset IDs and provenance. Report an unavailable capability directly.
2. Build a bounded recipe: order scenes, trim source time, use contain unless explicit cropping is intended, reserve caption space, supply narration/music tracks and leave a complete ending. Generation providers may supply assets through the same operator-approved intake; no provider is implied.
3. Submit one stable request key. A queued response is a job, not a finished video. Inspect status until the owner returns done/failed/canceled/unknown; never duplicate a pending or unknown request.
4. Return the actual artifact/run IDs and hash from status. QA reads the saved artifact and receipts; operator playback remains separate. Full decode, audio length or correlation does not prove clear speech or readable captions.
5. Cancellation uses the registered tool. Known failures may be explicitly retried by the operator within the same job's attempt bound. Publishing, email and other outbound delivery still require approval.

Source inventory and licensing: `docs/video-edit-inventory.md`. Browser capture and local Kokoro synthesis are separate capabilities, not activated by this editing tool.
