import { useEffect, useState } from 'react';
import { ApiError, request } from './api';
import { WorkspaceArtifactPanel } from './WorkspaceArtifactPanel';

type Owner = { run_id: string; profile_name: string };

export function TeamArtifactsPanel({ projectId, orderId }: { projectId: string; orderId: string }) {
  const [owners, setOwners] = useState<Owner[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    setOwners([]); setError(null); setLoading(true);
    void request(`/api/projects/${encodeURIComponent(projectId)}/work-orders/${encodeURIComponent(orderId)}/team-session/artifacts`, {
      signal: controller.signal,
    }).then((value) => {
      if (controller.signal.aborted) return;
      if (!Array.isArray(value) || value.some((item: unknown) => !item || typeof item !== 'object' ||
          !('run_id' in item) || !('profile_name' in item) || typeof item.run_id !== 'string' || typeof item.profile_name !== 'string')) {
        throw new ApiError('Team artifact owners could not be confirmed.', null);
      }
      const unique = new Map<string, Owner>();
      for (const item of value as Owner[]) unique.set(item.run_id, item);
      setOwners([...unique.values()]); setLoading(false);
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) { setError(cause instanceof Error ? cause.message : 'Artifacts unavailable.'); setLoading(false); }
    });
    return () => controller.abort();
  }, [projectId, orderId]);
  return <section aria-label="Team deliverables">
    <h3>Team deliverables</h3>
    {loading && <p role="status">Reading saved artifact owners…</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {!loading && !error && owners.length === 0 && <p>No team artifact is saved yet. A plan or successful chat turn is not a deliverable.</p>}
    {owners.map((item) => <details key={item.run_id}><summary>{item.profile_name} · saved artifact</summary>
      <WorkspaceArtifactPanel projectId={projectId} runId={item.run_id} /></details>)}
  </section>;
}
