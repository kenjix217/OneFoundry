import { ApiError, request } from './api';
import type { VideoLimits } from './localVideoRecorder';

export type CaptureRequest = { capture_id: string; run_id: string; title: string; caption: string;
  status: 'waiting_for_operator' | 'pending' | 'unknown' | 'ready'; artifact_id?: string; sha256?: string };
const base = (project: string) => `/api/projects/${encodeURIComponent(project)}`;

export async function getVideoLimits(project: string, signal?: AbortSignal): Promise<VideoLimits> {
  const value = await request(`${base(project)}/media-capture/limits`, { signal });
  if (!value || typeof value !== 'object' || !('enabled' in value) || typeof value.enabled !== 'boolean' ||
      !['max_bytes', 'max_duration_seconds', 'max_width', 'max_height', 'frame_rate'].every((key) =>
        key in value && Number.isSafeInteger((value as Record<string, unknown>)[key]) &&
        Number((value as Record<string, unknown>)[key]) > 0)) throw new ApiError('Recording limits could not be verified.', null);
  return value as VideoLimits;
}

export async function listMediaCaptures(project: string, order: string, signal?: AbortSignal): Promise<CaptureRequest[]> {
  const value = await request(`${base(project)}/work-orders/${encodeURIComponent(order)}/media-captures`, { signal });
  if (!Array.isArray(value) || value.some((item: unknown) => !item || typeof item !== 'object' ||
      !['capture_id', 'run_id', 'title', 'caption', 'status'].every((key) => key in item && typeof (item as Record<string, unknown>)[key] === 'string' && (item as Record<string, string>)[key].length > 0) || !('status' in item) || !['waiting_for_operator', 'pending', 'unknown', 'ready'].includes(String(item.status)))) {
    throw new ApiError('Recording requests could not be verified.', null);
  }
  return value as CaptureRequest[];
}

export async function saveRecordedVideo(project: string, capture: CaptureRequest, blob: Blob): Promise<void> {
  const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
  const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
  const value = await request(`${base(project)}/runs/${encodeURIComponent(capture.run_id)}/media-captures/${encodeURIComponent(capture.capture_id)}`, {
    method: 'POST', body: blob, headers: { 'Content-Type': 'video/webm', 'X-OneFoundry-Recording-Reviewed': 'true' },
  });
  if (!value || typeof value !== 'object' || !('sha256' in value) || value.sha256 !== sha256 ||
      !('byte_size' in value) || value.byte_size !== blob.size || !('kind' in value) || value.kind !== 'video' ||
      !('mime_type' in value) || value.mime_type !== 'video/webm') throw new ApiError('The exact saved video bytes could not be confirmed. Check its saved intent before retrying.', null);
}

export function savedVideoUrl(project: string, run: string, artifact: string): string {
  return `${base(project)}/runs/${encodeURIComponent(run)}/workspace-artifacts/${encodeURIComponent(artifact)}/bytes`;
}

export async function verifyVideoPlayback(project: string, run: string, artifact: string, sha256: string): Promise<void> {
  const value = await request(`${base(project)}/runs/${encodeURIComponent(run)}/workspace-artifacts/${encodeURIComponent(artifact)}/verify-playback`, {
    method: 'POST', body: JSON.stringify({ expected_sha256: sha256,
      watched_picture_and_captions: true, narration_reviewed_if_present: true }),
  });
  if (!value || typeof value !== 'object' || !('artifact_id' in value) || value.artifact_id !== artifact ||
      !('sha256' in value) || value.sha256 !== sha256 || !('playback_verified' in value) || value.playback_verified !== true) {
    throw new ApiError('The exact playback decision could not be confirmed.', null);
  }
}
