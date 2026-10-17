import { useRef, useState } from 'react';
import { abandonExtensionBuild, type ExtensionAbandonment as Abandonment } from './extensionProgressApi';

/** An explicit operator decision never rewrites or retries an uncertain attempt. */
export function ExtensionAbandonment({ projectId, candidate, onSaved }: {
  projectId: string; candidate: Abandonment; onSaved: () => void;
}) {
  const [reason, setReason] = useState('');
  const [discarded, setDiscarded] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const identity = useRef<{ id: string; reason: string } | null>(null);
  async function save() {
    if (pending || !discarded || reason.trim().length < 10) return;
    const action = identity.current ?? { id: crypto.randomUUID(), reason: reason.trim() };
    identity.current = action;
    setPending(true); setError(null);
    try {
      await abandonExtensionBuild(projectId, candidate, action.id, action.reason);
      onSaved();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'The decision could not be confirmed.');
    } finally { setPending(false); }
  }
  return <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
    <p>A replacement can start after you confirm the stopped worker’s disposable work was discarded.</p>
    <label><input type="checkbox" checked={discarded} disabled={pending || identity.current !== null}
      onChange={(event) => setDiscarded(event.target.checked)} />
      The worker has stopped and its disposable changes have been discarded.</label>
    <label>Reason for discarding this build
      <textarea value={reason} minLength={10} maxLength={1000} required
        disabled={pending || identity.current !== null} onChange={(event) => setReason(event.target.value)} />
    </label>
    {error && <p role="alert">{error} Retry confirms the same decision.</p>}
    <button disabled={pending || !discarded || reason.trim().length < 10} type="submit">
      {pending ? 'Saving decision…' : 'Discard unverified build'}
    </button>
  </form>;
}
