import { useEffect, useState } from 'react';
import { getExtensionProgress, type ExtensionProgress as Progress } from './extensionProgressApi';
import { ExtensionAbandonment } from './ExtensionAbandonment';

/** Saved progress with an explicit operator recovery action for stopped builds. */
export function ExtensionProgress({ projectId, orderId }: { projectId: string; orderId: string }) {
  const [rows, setRows] = useState<Progress[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    setRows([]); setError(null);
    const refresh = () => {
      if (document.hidden || inFlight) return;
      inFlight = true;
      void getExtensionProgress(projectId, orderId, controller.signal).then((result) => {
        if (!controller.signal.aborted) { setRows(result); setError(null); }
      }).catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Progress is unavailable.');
      }).finally(() => { inFlight = false; });
    };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [projectId, orderId, revision]);
  if (!rows.length && !error) return null;
  return <section aria-label="Capability improvement progress">
    {error && <p role="alert">Could not confirm improvement progress: {error}</p>}
    {rows.map((row) => <article key={row.extensionKey}>
      <strong>{row.title}</strong><p>{row.message}</p>
      {row.abandonment && <ExtensionAbandonment projectId={projectId} candidate={row.abandonment}
        onSaved={() => setRevision((value) => value + 1)} />}
    </article>)}
  </section>;
}
