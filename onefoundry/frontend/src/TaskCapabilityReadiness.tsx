import { useEffect, useState } from 'react';
import { ExtensionProgress } from './ExtensionProgress';
import { CapabilityReadiness } from './CapabilityReadiness';
import type { CapabilityReport } from './capabilityReadiness';
import { getTeamReadiness } from './taskWorkflowApi';

/** Read-only checks inside the primary conversation's explicitly opened Details. */
export function TaskCapabilityReadiness({ projectId, orderId }: { projectId: string; orderId: string }) {
  const [report, setReport] = useState<CapabilityReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    setReport(null); setError(null); setLoaded(false);
    const refresh = () => {
      if (document.hidden || inFlight) return;
      inFlight = true;
      void getTeamReadiness(projectId, orderId, controller.signal).then((value) => {
        if (!controller.signal.aborted) { setReport(value.capability_readiness ?? null); setError(null); setLoaded(true); }
      }).catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Readiness is unavailable.');
      }).finally(() => { inFlight = false; });
    };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [projectId, orderId]);
  return <section aria-label="Conversation capability checks">
    {error && <p role="alert">Could not confirm capability readiness: {error}</p>}
    {!loaded && !error && <p role="status">Checking capability readiness…</p>}
    {loaded && !report && <p>Detailed capability checks are unavailable in this app version.</p>}
    {report && <CapabilityReadiness report={report} />}
    <ExtensionProgress projectId={projectId} orderId={orderId} />
  </section>;
}
