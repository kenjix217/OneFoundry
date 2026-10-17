import { useState } from 'react';

import { readCaptureSource, readMemory, type Candidate, type CaptureSource, type MemoryItem } from './memoryApi';

function captureActor(actor: string): string {
  if (actor === 'operator') return 'the operator';
  if (actor.startsWith('external-memory:')) return 'an authorized external client';
  return 'a configured agent';
}

export function MemorySourceReview({ projectId, candidate }: { projectId: string; candidate: Candidate }) {
  const [source, setSource] = useState<CaptureSource | null>(null);
  const [previous, setPrevious] = useState<MemoryItem | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  async function inspect() {
    setLoading(true); setError(null);
    try {
      if (candidate.source_capture_id) setSource(await readCaptureSource(projectId, candidate.source_capture_id));
      if (candidate.replaces_memory_id) setPrevious(await readMemory(projectId, candidate.replaces_memory_id));
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Source read failed.'); }
    finally { setLoading(false); }
  }
  if (!candidate.source_capture_id && !candidate.replaces_memory_id) return null;
  return <div>
    {candidate.replaces_memory_id && <p className="quiet-note">Approval replaces an existing fact. Its history and source are retained.</p>}
    <button type="button" className="button button-outline" disabled={loading} onClick={() => { void inspect(); }}>{loading ? 'Loading source…' : 'Inspect source'}</button>
    {error && <p role="alert">{error}</p>}
    {previous && <p className="memory-text">Previous fact ({previous.state}): {previous.text}</p>}
    {source && <div><p className="memory-text">Source: {source.source_ref}</p><p className="quiet-note">Captured {new Date(source.created_at).toLocaleString()} by {captureActor(source.actor_id)}</p>
      {source.messages.map((message, index) => <blockquote key={index}><p className="quiet-note">{message.speaker} · {new Date(message.sent_at).toLocaleString()}</p><p className="memory-text">{message.text}</p></blockquote>)}</div>}
  </div>;
}
