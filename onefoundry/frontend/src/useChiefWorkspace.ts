import { useCallback, useEffect, useState } from 'react';
import { ApiError, request } from './api';
import { listChiefConversations, resolveChiefConversation, type ChiefConversation } from './chiefWorkspaceApi';
import { getTeamSession, type TeamSessionView } from './taskSessionApi';
import { listApprovals, listWorkOrders, type Approval, type WorkOrder } from './workApi';
import type { ChiefFile } from './ChiefAgentPanel';
import { getChiefProgress, type ChiefProgress } from './chiefProgressApi';

export function useChiefWorkspace(projectId: string, initialOrderId: string | null) {
  const [rows, setRows] = useState<ChiefConversation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [resolving, setResolving] = useState(!!initialOrderId);
  const [agentId, setAgentId] = useState<string | null>(null);
  const [view, setView] = useState<TeamSessionView | null>(null);
  const [files, setFiles] = useState<ChiefFile[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [reviewOrder, setReviewOrder] = useState<WorkOrder | null>(null);
  const [progress, setProgress] = useState<ChiefProgress | null>(null);
  const [progressError, setProgressError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    if (!initialOrderId) { setSelectedId(null); setAgentId(null); setResolving(false); return () => controller.abort(); }
    setSelectedId(null); setAgentId(null); setResolving(true);
    void resolveChiefConversation(projectId, initialOrderId, controller.signal).then((saved) => {
      if (controller.signal.aborted) return;
      setSelectedId(saved.order_id); setAgentId(saved.order_id !== initialOrderId ? saved.agent_session_id : null);
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Conversation unavailable.'); })
      .finally(() => { if (!controller.signal.aborted) setResolving(false); });
    return () => controller.abort();
  }, [projectId, initialOrderId]);
  useEffect(() => {
    const controller = new AbortController();
    let pending = false;
    async function load() {
      if (pending) return;
      pending = true;
      try {
        const savedRows = await listChiefConversations(projectId, controller.signal);
        if (selectedId && savedRows.some((row) => row.id === selectedId && row.trashed)) {
          if (controller.signal.aborted) return;
          setRows(savedRows); setSelectedId(null); setAgentId(null); setView(null);
          setFiles([]); setApprovals([]); setReviewOrder(null); setProgress(null); setProgressError(null); setError(null);
          return;
        }
        let savedView: TeamSessionView | null = null;
        if (selectedId) {
          try { savedView = await getTeamSession(projectId, selectedId, undefined, controller.signal); }
          catch (cause) {
            if (!(cause instanceof ApiError) || cause.status !== 404) throw cause;
            await request(`/api/projects/${encodeURIComponent(projectId)}/work-orders/${encodeURIComponent(selectedId)}/team-session`, { method: 'POST', signal: controller.signal });
            savedView = await getTeamSession(projectId, selectedId, undefined, controller.signal);
          }
        }
        const [savedFiles, pendingApprovals, orders] = selectedId ? await Promise.all([
          request(`/api/projects/${encodeURIComponent(projectId)}/work-orders/${encodeURIComponent(selectedId)}/team-session/artifacts`, { signal: controller.signal }),
          listApprovals(projectId, 'pending', controller.signal), listWorkOrders(projectId, controller.signal),
        ]) : [[], [], []];
        if (controller.signal.aborted) return;
        if (!Array.isArray(savedFiles) || savedFiles.some((file: unknown) => !file || typeof file !== 'object' ||
            !('id' in file) || !('run_id' in file) || !('profile_id' in file))) throw new Error('Saved files could not be read.');
        const scope = new Set([selectedId, ...(savedView?.children.map((child) => child.work_order_id) ?? [])]);
        const selected = savedRows.find((item) => item.id === selectedId);
        let savedProgress: ChiefProgress | null = null;
        let savedProgressError: string | null = null;
        if (selectedId && selected?.canonical_chief) {
          try { savedProgress = await getChiefProgress(projectId, selectedId, controller.signal); }
          catch (cause) { savedProgressError = cause instanceof Error ? cause.message : 'Current work unavailable.'; }
        }
        if (controller.signal.aborted) return;
        setRows(savedRows); setView(savedView); setFiles(savedFiles as ChiefFile[]);
        setProgress(savedProgress); setProgressError(savedProgressError);
        setApprovals(pendingApprovals.filter((item) => scope.has(item.work_order_id)));
        setReviewOrder(orders.find((item) => item.id === selectedId) ?? null); setError(null);
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Workspace unavailable.');
      } finally { pending = false; }
    }
    setView(null); setFiles([]); setApprovals([]); setReviewOrder(null); setProgress(null); setProgressError(null);
    void load(); const timer = window.setInterval(() => { void load(); }, 3000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [projectId, selectedId, revision]);
  return { rows, selectedId, setSelectedId, resolving, agentId, setAgentId, view, files, approvals, reviewOrder, progress, progressError, error, refresh };
}
