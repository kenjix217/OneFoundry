import { createHash, webcrypto } from 'node:crypto';
import { afterEach, expect, it, vi } from 'vitest';
import { request } from '../src/api';
import { getVideoLimits, listMediaCaptures, saveRecordedVideo, verifyVideoPlayback } from '../src/mediaCaptureApi';
vi.mock('../src/api', async (importOriginal) => ({ ...await importOriginal<typeof import('../src/api')>(), request: vi.fn() }));
afterEach(() => { vi.clearAllMocks(); vi.unstubAllGlobals(); });
it('rejects malformed recording limits and unknown request states', async () => {
  vi.mocked(request).mockResolvedValue({ enabled: true, max_bytes: 0 });
  await expect(getVideoLimits('p')).rejects.toThrow('limits could not be verified');
  vi.mocked(request).mockResolvedValue([{ capture_id: 'c', run_id: 'r', title: 't', caption: 'c', status: 'pretend_success' }]);
  await expect(listMediaCaptures('p', 'o')).rejects.toThrow('requests could not be verified');
});
it('requires a playback receipt for the exact artifact and hash', async () => {
  const sha = 'a'.repeat(64);
  for (const value of [{}, { artifact_id: 'other', sha256: sha, playback_verified: true },
    { artifact_id: 'a', sha256: 'b'.repeat(64), playback_verified: true }, { artifact_id: 'a', sha256: sha, playback_verified: false }]) {
    vi.mocked(request).mockResolvedValue(value);
    await expect(verifyVideoPlayback('p', 'r', 'a', sha)).rejects.toThrow('decision could not be confirmed');
  }
  vi.mocked(request).mockResolvedValue({ artifact_id: 'a', sha256: sha, playback_verified: true });
  await expect(verifyVideoPlayback('p', 'r', 'a', sha)).resolves.toBeUndefined();
});
it('checks the saved bytes hash and size instead of inferring success from HTTP', async () => {
  vi.stubGlobal('crypto', webcrypto);
  const bytes = new TextEncoder().encode('synthetic container bytes'); const blob = new Blob([bytes]);
  Object.defineProperty(blob, 'arrayBuffer', { value: async () => bytes.buffer });
  const capture = { capture_id: 'c', run_id: 'r', title: 't', caption: 'c', status: 'waiting_for_operator' as const };
  const saved = { kind: 'video', mime_type: 'video/webm', byte_size: blob.size, sha256: createHash('sha256').update(bytes).digest('hex') };
  vi.mocked(request).mockResolvedValue({ ...saved, byte_size: blob.size + 1 });
  await expect(saveRecordedVideo('p', capture, blob)).rejects.toThrow('bytes could not be confirmed');
  vi.mocked(request).mockResolvedValue(saved);
  await expect(saveRecordedVideo('p', capture, blob)).resolves.toBeUndefined();
});
