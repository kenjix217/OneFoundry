import { useEffect, useState } from 'react';

import { MemoryLifecycle } from './MemoryLifecycle';
import { listMemoryHistory, type MemoryItem } from './memoryApi';

export function MemoryHistory({ projectId, onChanged }: { projectId: string; onChanged: () => void }) {
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void listMemoryHistory(projectId, controller.signal).then(setItems).catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      setError(cause instanceof Error ? cause.message : 'Memory history read failed.');
    });
    return () => controller.abort();
  }, [projectId, reload]);
  return <section className="memory-intake panel" aria-labelledby="memory-history-heading"><h2 id="memory-history-heading">Withdrawn and replaced facts</h2>
    <p>Historical facts stay out of current retrieval. You can restore a withdrawn fact. Replaced facts retain their source and replacement link.</p>
    {error && <p role="alert">{error}</p>}
    {items.map((item) => <article className="memory-item" key={item.id}><p>{item.text}</p><p>{item.state}</p>
      <MemoryLifecycle projectId={projectId} item={item} onChanged={() => { setReload((value) => value + 1); onChanged(); }} /></article>)}
    {!error && items.length === 0 && <p>No withdrawn or replaced facts for this project.</p>}
  </section>;
}
