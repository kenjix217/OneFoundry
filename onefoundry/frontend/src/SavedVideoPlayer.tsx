import { useState } from 'react';
import { savedVideoUrl, verifyVideoPlayback } from './mediaCaptureApi';

export function SavedVideoPlayer({ projectId, runId, artifactId, sha256, verified, onVerified }: {
  projectId: string; runId: string; artifactId: string; sha256: string; verified: boolean; onVerified?: () => void;
}) {
  const [watched, setWatched] = useState(false); const [accepted, setAccepted] = useState(verified);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  const [decodeFailed, setDecodeFailed] = useState(false);
  async function verify() {
    if (!watched || busy) return;
    setBusy(true); setError(null);
    try { await verifyVideoPlayback(projectId, runId, artifactId, sha256); setAccepted(true); onVerified?.(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Playback verification failed.'); }
    finally { setBusy(false); }
  }
  return <section aria-label="Saved video playback">
    <p><a href={savedVideoUrl(projectId, runId, artifactId)} target="_blank" rel="noopener noreferrer">Open video</a></p>
    <video src={savedVideoUrl(projectId, runId, artifactId)} controls playsInline preload="metadata"
      style={{ width: '100%', maxHeight: '440px' }} onEnded={() => setWatched(true)}
      onError={() => { setWatched(false); setDecodeFailed(true); setError('This browser cannot play the saved video. Playback has not been verified.'); }} />
    <p>Saved byte integrity is verified. {accepted ? 'The operator verified playback of this exact hash. Independent QA and work-order acceptance remain separate.' : 'Watch the entire saved clip and check picture, captions and narration if used.'}</p>
    {!accepted && <button type="button" disabled={!watched || busy || decodeFailed} onClick={() => { void verify(); }}>I watched and verified this saved video</button>}
    {error && <p className="form-error" role="alert">{error}</p>}
  </section>;
}
