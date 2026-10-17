import { TaskWorkflowPanel } from './TaskWorkflowPanel';
import { useCallback, useEffect, useState, type FormEvent } from 'react';

import { ApiError, type Profile } from './api';
import { getCodeEligibility, getSyntheticCode, launchSyntheticCode, type CodeEligibility } from './codeApi';
import { getExternalCode, getExternalCodeEligibility, launchExternalCode,
  getOfflineSourceProbeEligibility, launchOfflineSourceProbe,
  type CodingAdapterId, type ExternalCodeEligibility, type OfflineSourceProbeEligibility } from './externalCodeApi';
import { listProjectSources, type ProjectSource } from './sourceApi';
import { SnapshotCodeLaunch } from './SnapshotCodeLaunch';
import { listCatalogModels, listModelAccounts, listModelProviders } from './modelApi';
import { assignWorkOrder, blockWorkOrder, editUnknownWorkOrder, getLiveTextOutput, getRun, getSyntheticTextEligibility, getTextOutputs, listApprovals, listRuns,
  listWorkOrders, markWorkReady, runLiveText, runSyntheticText, startManualRun, unblockWorkOrder,
  LiveRunError, type Approval, type Run, type SyntheticTextEligibility, type WorkOrder } from './workApi';
import { WorkOrderReviewActions } from './WorkOrderReviewActions';
import { WorkOrderCreateDialog } from './WorkOrderCreateDialog';
import { InputFilePicker } from './InputFilePicker';
import { workOrderNow, workOrderStatusLabel } from './workOrderActivity';
import { WorkOrderTrigger } from './WorkOrderTrigger';
import { listInputFiles, uploadInputFiles, type InputFileMeta, type PendingInputFile } from './inputFileApi';

type Props = {
  projectId: string;
  profiles: Profile[];
  onOpenRun: (runId: string) => void;
  onOpenTask?: (orderId: string) => void;
  initialOrderId?: string | null;
  initiallyManage?: boolean;
  initiallyCreate?: boolean;
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Could not complete the request. Check the saved state before retrying.';
}

function profileName(profiles: Profile[], profileId: string): string {
  return profiles.find((profile) => profile.id === profileId)?.name ?? 'Previous agent';
}

export function WorkOrdersView({ projectId, profiles, onOpenRun, onOpenTask, initialOrderId,
  initiallyManage = false, initiallyCreate = false }: Props) {
  const [orders, setOrders] = useState<WorkOrder[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [pendingApprovals, setPendingApprovals] = useState<Approval[]>([]);
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(initialOrderId ?? null);
  const [manageOpen, setManageOpen] = useState(initiallyManage || onOpenTask === undefined);
  useEffect(() => {
    if (initiallyManage && initialOrderId) { setSelectedId(initialOrderId); setManageOpen(true); }
  }, [initiallyManage, initialOrderId]);
  const [filter, setFilter] = useState<'all' | 'needs' | 'working' | 'ready' | 'completed'>('all');
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [showForm, setShowForm] = useState(initiallyCreate);
  const [action, setAction] = useState<string | null>(null);
  const [reassignmentProfileId, setReassignmentProfileId] = useState('');
  const [blockReason, setBlockReason] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const [editingUnknown, setEditingUnknown] = useState(false);
  const [editTitle, setEditTitle] = useState('');
  const [editInstructions, setEditInstructions] = useState('');
  const [restartAcknowledged, setRestartAcknowledged] = useState(false);
  const [selectedFiles, setSelectedFiles] = useState<InputFileMeta[]>([]);
  const [additionalFiles, setAdditionalFiles] = useState<PendingInputFile[]>([]);
  const [fileAction, setFileAction] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const [filePanelOpen, setFilePanelOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [eligibility, setEligibility] = useState<SyntheticTextEligibility | null>(null);
  const [eligibilityLoading, setEligibilityLoading] = useState(false);
  const [eligibilityError, setEligibilityError] = useState<string | null>(null);
  const [codeEligibility, setCodeEligibility] = useState<CodeEligibility | null>(null);
  const [codeEligibilityLoading, setCodeEligibilityLoading] = useState(false);
  const [codeEligibilityError, setCodeEligibilityError] = useState<string | null>(null);
  const [externalEligibility, setExternalEligibility] = useState<ExternalCodeEligibility | null>(null);
  const [externalEligibilityLoading, setExternalEligibilityLoading] = useState(false);
  const [externalEligibilityError, setExternalEligibilityError] = useState<string | null>(null);
  const [codingAdapter, setCodingAdapter] = useState<CodingAdapterId | ''>('');
  const [codingLaunchUncertain, setCodingLaunchUncertain] = useState(false);
  const [offlineEligibility, setOfflineEligibility] = useState<OfflineSourceProbeEligibility | null>(null);
  const [offlineError, setOfflineError] = useState<string | null>(null);
  const [offlineLoading, setOfflineLoading] = useState(false);
  const [offlineSources, setOfflineSources] = useState<ProjectSource[]>([]);
  const [offlineSourceId, setOfflineSourceId] = useState('');
  const [offlineLaunchUncertain, setOfflineLaunchUncertain] = useState(false);
  const [liveTextEligible, setLiveTextEligible] = useState(false);
  const [liveTextEligibilityLoading, setLiveTextEligibilityLoading] = useState(false);
  const [liveTextEligibilityError, setLiveTextEligibilityError] = useState<string | null>(null);
  const [liveToolRouteNote, setLiveToolRouteNote] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    void Promise.all([listWorkOrders(projectId, controller.signal), listRuns(projectId, '', controller.signal)])
      .then(([items, runItems]) => {
        setOrders(items);
        setRuns(runItems);
        setSelectedId((current) => current && items.some((item) => item.id === current) ? current : items[0]?.id ?? null);
        setLoading(false);
      }).catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setLoadError(errorMessage(error));
        setLoading(false);
      });
    setApprovalError(null);
    void listApprovals(projectId, 'pending', controller.signal).then((approvals) => {
      if (!controller.signal.aborted) setPendingApprovals(approvals);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setApprovalError(errorMessage(error));
    });
    return () => controller.abort();
  }, [projectId, reload]);

  const selected = orders.find((order) => order.id === selectedId);
  const synchronizeOrderStatus = useCallback((status: string) => {
    if (selected && selected.status !== status) setReload((value) => value + 1);
  }, [selected]);
  const latestSelectedRun = runs.find((run) => run.work_order_id === selectedId);
  const recoverableRun = selected?.status === 'unknown' && latestSelectedRun?.status === 'unknown' &&
    latestSelectedRun.mode === 'live_text' ? latestSelectedRun : null;
  useEffect(() => {
    if (!selectedId || !filePanelOpen) { setSelectedFiles([]); return; }
    const controller = new AbortController();
    void listInputFiles(projectId, selectedId).then((items) => {
      if (!controller.signal.aborted) setSelectedFiles(items);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setFileError(errorMessage(error));
    });
    return () => controller.abort();
  }, [projectId, selectedId, reload, filePanelOpen]);
  const researcherReady = selected?.status === 'ready' && profiles.some((profile) =>
    profile.id === selected.profile_id && profile.kind === 'builtin' && profile.name === 'Researcher');
  const engineerReady = selected?.status === 'ready' && profiles.some((profile) =>
    profile.id === selected.profile_id && profile.kind === 'builtin' && profile.name === 'Engineer');
  const liveTextCandidate = !!selected && (selected.status === 'ready' || !!recoverableRun) && profiles.some((profile) =>
    profile.id === selected.profile_id && Boolean(profile.model_account_id) && Boolean(profile.model) &&
    (profile.kind === 'custom' || (profile.kind === 'builtin' && ['Researcher', 'GTM'].includes(profile.name))));

  useEffect(() => {
    setLiveTextEligible(false);
    setLiveTextEligibilityError(null);
    setLiveTextEligibilityLoading(false);
    setLiveToolRouteNote(null);
    if (!liveTextCandidate || !selected) return;
    const assigned = profiles.find((profile) => profile.id === selected.profile_id);
    if (!assigned?.model_account_id || !assigned.model) return;
    const controller = new AbortController();
    setLiveTextEligibilityLoading(true);
    void Promise.all([
      listModelProviders(projectId, controller.signal),
      listModelAccounts(projectId, controller.signal),
      listCatalogModels(projectId, controller.signal),
    ]).then(([providers, accounts, catalog]) => {
      const account = accounts.find((item) => item.id === assigned.model_account_id);
      const supported = providers.some((item) => item.id === account?.provider && item.text_runtime_available);
      const textModel = catalog.some((item) => item.account_id === account?.id &&
        item.model_id === assigned.model && item.category === 'text' && item.available);
      const requiresTools = assigned.allowed_tools.length > 0;
      const toolRouteUnavailable = Boolean(requiresTools && account && account.provider !== 'openai');
      if (toolRouteUnavailable) {
        setLiveToolRouteNote('This model connection cannot execute OneFoundry role tools. The saved tool IDs are requests, and starting this work order would only generate text. No provider call will be made while tools are required.');
      } else if (account?.provider === 'openai_codex_runtime') {
        setLiveToolRouteNote('This Codex subscription connection currently generates text only. It cannot create work orders, save artifacts, or call other OneFoundry role tools.');
      }
      setLiveTextEligible(Boolean(account?.catalog_valid && supported && textModel &&
        (account.auth_kind === 'none' || account.credential_present) && !toolRouteUnavailable));
      setLiveTextEligibilityLoading(false);
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setLiveTextEligibilityError(errorMessage(error));
      setLiveTextEligibilityLoading(false);
    });
    return () => controller.abort();
  }, [projectId, selected, profiles, liveTextCandidate]);

  useEffect(() => {
    setEligibility(null);
    setEligibilityError(null);
    if (!researcherReady || !selectedId) return;
    const controller = new AbortController();
    setEligibilityLoading(true);
    void getSyntheticTextEligibility(projectId, selectedId, controller.signal).then((result) => {
      setEligibility(result);
      setEligibilityLoading(false);
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setEligibilityError(errorMessage(error));
      setEligibilityLoading(false);
    });
    return () => controller.abort();
  }, [projectId, selectedId, researcherReady]);

  useEffect(() => {
    setCodeEligibility(null);
    setCodeEligibilityError(null);
    if (!engineerReady || !selectedId) return;
    const controller = new AbortController();
    setCodeEligibilityLoading(true);
    void getCodeEligibility(projectId, selectedId, controller.signal).then((result) => {
      setCodeEligibility(result);
      setCodeEligibilityLoading(false);
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setCodeEligibilityError(errorMessage(error));
      setCodeEligibilityLoading(false);
    });
    return () => controller.abort();
  }, [projectId, selectedId, engineerReady]);

  useEffect(() => {
    setOfflineEligibility(null); setOfflineError(null); setOfflineSources([]); setOfflineSourceId('');
    setOfflineLaunchUncertain(false);
    if (!engineerReady || !selectedId) return;
    const controller = new AbortController();
    setOfflineLoading(true);
    void Promise.all([getOfflineSourceProbeEligibility(projectId, selectedId, controller.signal),
      listProjectSources(projectId, controller.signal)]).then(([result, sources]) => {
      if (controller.signal.aborted) return;
      setOfflineEligibility(result);
      setOfflineSources(sources.filter((source) => source.kind === 'local' && source.project_id === projectId));
      setOfflineLoading(false);
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setOfflineError(errorMessage(error)); setOfflineLoading(false);
    });
    return () => controller.abort();
  }, [projectId, selectedId, engineerReady]);

  useEffect(() => {
    setExternalEligibility(null);
    setExternalEligibilityError(null);
    setCodingAdapter('');
    setCodingLaunchUncertain(false);
    if (!engineerReady || !selectedId) return;
    const controller = new AbortController();
    setExternalEligibilityLoading(true);
    void getExternalCodeEligibility(projectId, selectedId, controller.signal).then((result) => {
      setExternalEligibility(result);
      setExternalEligibilityLoading(false);
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setExternalEligibilityError(errorMessage(error));
      setExternalEligibilityLoading(false);
    });
    return () => controller.abort();
  }, [projectId, selectedId, engineerReady]);

  async function readback(workId: string, expectedStatus: WorkOrder['status']) {
    const [items, runItems] = await Promise.all([listWorkOrders(projectId), listRuns(projectId)]);
    const saved = items.find((item) => item.id === workId);
    if (!saved || saved.status !== expectedStatus) {
      throw new ApiError('The write returned, but work order readback did not confirm the expected state. Check the saved state before retrying.', null);
    }
    setOrders(items);
    setRuns(runItems);
    setSelectedId(workId);
    return runItems;
  }

  async function refreshAfterOwnershipError(error: unknown) {
    try {
      const [items, runItems] = await Promise.all([listWorkOrders(projectId), listRuns(projectId)]);
      setOrders(items);
      setRuns(runItems);
      setFormError(`${errorMessage(error)} Saved work orders were refreshed; review the current owner and state before retrying.`);
    } catch (readbackError) {
      setFormError(`${errorMessage(error)} Saved-state refresh also failed: ${errorMessage(readbackError)}`);
    }
  }

  async function changeOwnership(order: WorkOrder, kind: 'assign' | 'block' | 'unblock') {
    setAction(kind);
    setFormError(null);
    setNotice(null);
    try {
      if (kind === 'assign') {
        const target = reassignmentProfileId || order.profile_id;
        if (target === order.profile_id) return;
        await assignWorkOrder(projectId, order.id, target, order.version);
      } else if (kind === 'block') {
        const reason = blockReason.trim();
        if (!reason) {
          setFormError('Enter a reason before blocking this work order.');
          return;
        }
        await blockWorkOrder(projectId, order.id, reason, order.version);
      } else {
        await unblockWorkOrder(projectId, order.id, order.version);
      }
      const [items, runItems] = await Promise.all([listWorkOrders(projectId), listRuns(projectId)]);
      const saved = items.find((item) => item.id === order.id);
      const expectedStatus = kind === 'block' ? 'blocked' : kind === 'unblock' ? order.blocked_from_status : order.status;
      if (!saved || saved.version <= order.version || saved.status !== expectedStatus ||
          (kind === 'assign' && saved.profile_id !== (reassignmentProfileId || order.profile_id)) ||
          (kind === 'block' && saved.block_reason !== blockReason.trim()) ||
          (kind === 'unblock' && saved.block_reason !== null)) {
        throw new ApiError('The write returned, but saved work order readback did not confirm the change.', null);
      }
      setOrders(items);
      setRuns(runItems);
      setReassignmentProfileId('');
      setBlockReason('');
      setNotice(kind === 'assign' ? 'Assigned profile updated.' : kind === 'block' ? 'Work order blocked.' : 'Work order resumed.');
    } catch (error) {
      await refreshAfterOwnershipError(error);
    } finally {
      setAction(null);
    }
  }

  async function saveUnknownEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected || !recoverableRun) return;
    const title = editTitle.trim();
    const instructions = editInstructions.trim();
    if (!title || !instructions || (title === selected.title && instructions === selected.instructions)) return;
    setAction('edit-unknown'); setFormError(null); setNotice(null);
    try {
      await editUnknownWorkOrder(projectId, selected.id, {
        title, instructions, expected_version: selected.version,
      });
      const [items, runItems] = await Promise.all([listWorkOrders(projectId), listRuns(projectId)]);
      const saved = items.find((item) => item.id === selected.id);
      if (!saved || saved.status !== 'unknown' || saved.version <= selected.version ||
        saved.title !== title || saved.instructions !== instructions ||
        !runItems.some((run) => run.id === recoverableRun.id && run.status === 'unknown')) {
        throw new ApiError('Saved work order did not confirm the edit. Review its current state before retrying.', null);
      }
      setOrders(items); setRuns(runItems); setEditingUnknown(false); setRestartAcknowledged(false);
      setNotice('Work order updated. The earlier unknown run keeps its original task snapshot.');
    } catch (error) { await refreshAfterOwnershipError(error); }
    finally { setAction(null); }
  }

  async function addFilesToSelected() {
    if (!selected || !additionalFiles.length) return;
    setFileAction(true); setFileError(null);
    try {
      await uploadInputFiles(projectId, additionalFiles, 'work_order', selected.id);
      const linked = await listInputFiles(projectId, selected.id);
      if (!additionalFiles.every((item) => linked.some((file) => file.id === item.id))) {
        throw new ApiError('Saved work order did not confirm every file.', null);
      }
      setSelectedFiles(linked); setAdditionalFiles([]);
      setNotice('Work order files saved. Processing status appears below.');
    } catch (error) { setFileError(errorMessage(error)); }
    finally { setFileAction(false); }
  }

  async function transition(order: WorkOrder, target: 'ready' | 'running') {
    setAction(target);
    setFormError(null);
    setNotice(null);
    try {
      if (target === 'ready') {
        await markWorkReady(projectId, order.id);
      } else {
        await startManualRun(projectId, order.id);
      }
      const runItems = await readback(order.id, target);
      if (target === 'running' && !runItems.some((run) => run.work_order_id === order.id)) {
        throw new ApiError('The run start returned, but run readback did not find it. Check the saved state.', null);
      }
      setNotice(target === 'ready' ? 'Work order marked ready.' : 'Manual planning run recorded. No agent or provider was called.');
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      setAction(null);
    }
  }

  async function startSynthetic(order: WorkOrder) {
    setAction('synthetic');
    setFormError(null);
    setNotice(null);
    try {
      const created = await runSyntheticText(projectId, order.id);
      const [runItems, savedOutputs] = await Promise.all([
        readback(order.id, 'done'), getTextOutputs(projectId, created.run.id),
      ]);
      if (!runItems.some((run) => run.id === created.run.id && run.mode === 'synthetic_text' && run.status === 'done') ||
        created.outputs.length !== savedOutputs.length ||
        created.outputs.some((output) => !savedOutputs.some((saved) => saved.id === output.id))) {
        throw new ApiError('The fixture returned, but saved run and text readback did not confirm it. Check the saved state before retrying.', null);
      }
      onOpenRun(created.run.id);
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      setAction(null);
    }
  }

  async function startSyntheticCode(order: WorkOrder) {
    setAction('synthetic-code');
    setFormError(null);
    setNotice(null);
    try {
      const launched = await launchSyntheticCode(projectId, order.id);
      const [runItems, detail] = await Promise.all([
        readback(order.id, 'running'), getSyntheticCode(projectId, launched.run.id),
      ]);
      if (!runItems.some((run) => run.id === launched.run.id && run.mode === 'synthetic_code') ||
        detail.jobs.length === 0 || detail.jobs.some((job) => !launched.jobs.some((started) => started.id === job.id))) {
        throw new ApiError('The fixture returned, but saved runner readback did not confirm it. Check the saved state before retrying.', null);
      }
      onOpenRun(launched.run.id);
    } catch (error) {
      setFormError(errorMessage(error));
      setReload((value) => value + 1);
    } finally {
      setAction(null);
    }
  }

  async function startExternalCode(order: WorkOrder) {
    if (codingLaunchUncertain || !codingAdapter || !externalEligibility?.eligible ||
      !externalEligibility.adapters.some((adapter) => adapter.adapter_id === codingAdapter && adapter.available)) return;
    const previousRunIds = new Set(runs.filter((run) => run.work_order_id === order.id).map((run) => run.id));
    setAction('external-code');
    setFormError(null);
    setNotice(null);
    try {
      const launched = await launchExternalCode(projectId, order.id, codingAdapter);
      const [runItems, saved] = await Promise.all([
        readback(order.id, 'running'), getExternalCode(projectId, launched.run.id),
      ]);
      if (!runItems.some((run) => run.id === launched.run.id && run.mode === 'external_code') ||
        !saved.attempts.some((attempt) => launched.attempts.some((created) =>
          created.id === attempt.id && created.adapter_id === codingAdapter))) {
        throw new ApiError('The coding launch returned, but saved attempt readback did not confirm it. Inspect the run before any retry.', null);
      }
      onOpenRun(launched.run.id);
    } catch (error) {
      try {
        const [savedOrders, savedRuns] = await Promise.all([listWorkOrders(projectId), listRuns(projectId)]);
        setOrders(savedOrders);
        setRuns(savedRuns);
        const recorded = savedRuns.find((run) => run.work_order_id === order.id &&
          run.mode === 'external_code' && !previousRunIds.has(run.id));
        if (recorded) {
          await getExternalCode(projectId, recorded.id);
          onOpenRun(recorded.id);
        } else {
          setCodingLaunchUncertain(true);
          setFormError(`${errorMessage(error)} No new coding run was found in saved state; do not launch again until its outcome is known.`);
        }
      } catch (readbackError) {
        setCodingLaunchUncertain(true);
        setFormError(`${errorMessage(error)} Saved coding run readback also failed: ${errorMessage(readbackError)} Do not launch again until its outcome is known.`);
      }
    } finally {
      setAction(null);
    }
  }

  async function startOfflineProbe(order: WorkOrder) {
    const source = offlineSources.find((item) => item.id === offlineSourceId && item.kind === 'local');
    if (!offlineEligibility?.eligible || !source || offlineLaunchUncertain) return;
    const previousRunIds = new Set(runs.filter((run) => run.work_order_id === order.id).map((run) => run.id));
    setAction('offline-probe'); setFormError(null); setNotice(null);
    try {
      const launched = await launchOfflineSourceProbe(projectId, order.id, source.id);
      const [runItems, saved] = await Promise.all([readback(order.id, 'running'), getExternalCode(projectId, launched.run.id)]);
      if (!runItems.some((run) => run.id === launched.run.id && run.mode === 'external_code') ||
        !saved.attempts.some((attempt) => attempt.adapter_id === 'offline_probe' && attempt.source_id === source.id &&
          launched.attempts.some((created) => created.id === attempt.id))) {
        throw new ApiError('The offline probe returned, but saved attempt readback did not confirm it.', null);
      }
      onOpenRun(launched.run.id);
    } catch (error) {
      try {
        const [savedOrders, savedRuns] = await Promise.all([listWorkOrders(projectId), listRuns(projectId)]);
        setOrders(savedOrders); setRuns(savedRuns);
        const recorded = savedRuns.find((run) => run.work_order_id === order.id &&
          run.mode === 'external_code' && !previousRunIds.has(run.id));
        if (recorded) {
          const saved = await getExternalCode(projectId, recorded.id);
          if (!saved.attempts.some((attempt) => attempt.adapter_id === 'offline_probe' && attempt.source_id === source.id)) {
            throw new ApiError('A new run was found, but its offline source attempt did not match.', null);
          }
          onOpenRun(recorded.id);
        } else {
          setOfflineLaunchUncertain(true);
          setFormError(`${errorMessage(error)} No new offline probe run was found. Inspect saved state before retrying.`);
        }
      } catch (readbackError) {
        setOfflineLaunchUncertain(true);
        setFormError(`${errorMessage(error)} Saved offline probe readback also failed: ${errorMessage(readbackError)} Do not launch again until its outcome is known.`);
      }
    } finally { setAction(null); }
  }

  async function startLiveText(order: WorkOrder, priorUnknown?: Run) {
    const previousRunIds = new Set(runs.filter((run) => run.work_order_id === order.id).map((run) => run.id));
    setAction(priorUnknown ? 'restart-live-text' : 'live-text');
    setFormError(null);
    setNotice(null);
    try {
      const created = await runLiveText(projectId, order.id, priorUnknown ? {
        prior_run_id: priorUnknown.id, expected_version: order.version, acknowledge_unknown: true,
      } : undefined);
      const [runItems, savedOutput] = await Promise.all([
        readback(order.id, 'done'), getLiveTextOutput(projectId, created.run.id),
      ]);
      if (!runItems.some((run) => run.id === created.run.id && run.mode === 'live_text' && run.status === 'done') ||
        savedOutput?.id !== created.output.id) {
        throw new ApiError('The provider returned, but saved run and output readback did not confirm it. Inspect saved state before retrying.', null);
      }
      onOpenRun(created.run.id);
    } catch (error) {
      try {
        const [savedOrders, savedRuns] = await Promise.all([listWorkOrders(projectId), listRuns(projectId)]);
        setOrders(savedOrders);
        setRuns(savedRuns);
        const runId = error instanceof LiveRunError ? error.runId : null;
        const recorded = savedRuns.find((run) => run.id === runId && run.work_order_id === order.id) ??
          savedRuns.find((run) => run.work_order_id === order.id && run.mode === 'live_text' && !previousRunIds.has(run.id));
        if (recorded) {
          await getRun(projectId, recorded.id);
          onOpenRun(recorded.id);
        } else {
          setFormError(`${errorMessage(error)} No new live run was found in saved state.`);
        }
      } catch (readbackError) {
        setFormError(`${errorMessage(error)} Run readback also failed: ${errorMessage(readbackError)}`);
      }
    } finally {
      setAction(null);
    }
  }

  const selectedRuns = runs.filter((run) => run.work_order_id === selectedId);
  const needsOperator = (order: WorkOrder) => ['blocked', 'unknown', 'failed', 'needs_review'].includes(order.status) ||
    pendingApprovals.some((approval) => approval.work_order_id === order.id);
  const matchingOrders = orders.filter((order) => {
    const textMatches = `${order.title} ${order.instructions} ${profileName(profiles, order.profile_id)}`
      .toLocaleLowerCase().includes(search.trim().toLocaleLowerCase());
    if (!textMatches) return false;
    if (filter === 'needs') return needsOperator(order);
    if (filter === 'working') return order.status === 'running';
    if (filter === 'ready') return order.status === 'ready' || order.status === 'draft';
    if (filter === 'completed') return order.status === 'done';
    return true;
  });
  const counts = {
    all: orders.length,
    needs: orders.filter(needsOperator).length,
    working: orders.filter((order) => order.status === 'running').length,
    ready: orders.filter((order) => order.status === 'ready' || order.status === 'draft').length,
    completed: orders.filter((order) => order.status === 'done').length,
  };

  return (
    <section className="work-area" aria-labelledby="work-heading">
      <div className="view-heading">
        <div><p className="eyebrow">Project work</p><h1 id="work-heading">Work orders</h1>
          <p>See what is happening, what needs you, and the next step for every assignment.</p></div>
        <button className="button button-primary" type="button" onClick={() => { setShowForm(true); setFormError(null); }}>+ New work order</button>
      </div>
      {notice && <div className="notice" role="status">{notice}</div>}
      {showForm && <WorkOrderCreateDialog key={projectId} projectId={projectId} profiles={profiles}
        onConfirmDraft={async (id) => { await readback(id, 'draft'); }}
        onSaved={(message) => { setShowForm(false); setNotice(message); }}
        onClose={() => setShowForm(false)} />}
      {loading && <div className="loading-panel" role="status"><span className="loading-orb" />Loading work orders…</div>}
      {loadError && !loading && <div className="empty-inline"><p role="alert">{loadError}</p><button className="button button-outline" type="button" onClick={() => setReload((value) => value + 1)}>Try again</button></div>}
      {!loading && !loadError && orders.length === 0 && <div className="empty-inline">No work orders yet. Create a draft to begin planning.</div>}
      {!loading && !loadError && orders.length > 0 && (
        <div className="work-layout">
          <div className="work-queue-controls">
            <nav aria-label="Filter work orders">{([
              ['all', 'All'], ['needs', 'Needs me'], ['working', 'In progress'], ['ready', 'Ready'], ['completed', 'Completed'],
            ] as const).map(([key, label]) => <button key={key} type="button" aria-pressed={filter === key}
              onClick={() => setFilter(key)}>{label} <span>{counts[key]}</span></button>)}</nav>
            <label className="field"><span className="field-label">Search work orders</span>
              <input type="search" aria-label="Search work orders" value={search} onChange={(event) => setSearch(event.target.value)}
                placeholder="Search title, brief, or agent" /></label>
            {approvalError && <p className="quiet-note" role="status">Approval status could not be read. “Needs me” may be incomplete. {approvalError}</p>}
          </div>
          <div className="work-list" aria-label="Work orders">
            {matchingOrders.length === 0 && <p>No work orders match this view.</p>}
            {matchingOrders.map((order) => {
              const summary = pendingApprovals.some((approval) => approval.work_order_id === order.id)
                ? { now: 'An action is waiting for your approval.', next: 'Review the request in Approvals.' }
                : workOrderNow(order, runs);
              return <article key={order.id} className="work-queue-card">
                <div><span className={`status-chip work-status status-${order.status}`}>{workOrderStatusLabel(order.status)}</span>
                  <h2>{order.title}</h2><p>Assigned to {profileName(profiles, order.profile_id)}</p>
                  <WorkOrderTrigger cause={order.triggered_by} />
                  {order.status === 'blocked' && <p className="work-blocked-reason">Blocked: {order.block_reason}</p>}</div>
                <div className="work-queue-summary"><p><strong>Now</strong> {summary.now}</p><p><strong>Next</strong> {summary.next}</p></div>
                <div className="work-queue-actions">
                  <button className="button button-outline" type="button" onClick={() => onOpenTask?.(order.id)}>Open task</button>
                  <button className="button button-quiet" type="button" onClick={() => { setSelectedId(order.id); setManageOpen(true); }}>Manage</button>
                </div>
              </article>;
            })}
          </div>
          {selected && manageOpen && onOpenTask && <button className="work-management-backdrop" type="button" aria-label="Close work order management"
            onClick={() => setManageOpen(false)} />}
          {selected && <details className={`work-management panel ${onOpenTask ? 'work-management-drawer' : ''}`} open={manageOpen}>
            <summary onClick={(event) => { if (onOpenTask) event.preventDefault(); else setManageOpen((open) => !open); }}>Manage {selected.title}</summary><article className="work-detail">
            {onOpenTask && <button className="work-management-close button button-quiet" type="button" onClick={() => setManageOpen(false)}>Close ×</button>}
            <label className="field"><span className="field-label">Manage another work order</span>
              <select value={selectedId ?? ''} onChange={(event) => setSelectedId(event.target.value)}>
                {orders.map((order) => <option key={order.id} value={order.id}>{order.title}</option>)}</select></label>
            <div className="detail-top"><span className="eyebrow">Selected work order</span><span className={`status-chip status-${selected.status}`}>{workOrderStatusLabel(selected.status)}</span></div>
            <p className="detail-text">{selected.instructions}</p>
            <TaskWorkflowPanel projectId={projectId} orderId={selected.id} version={selected.version} reloadKey={reload}
              onStatusRead={synchronizeOrderStatus} onActivated={() => setReload((value) => value + 1)} />
            <WorkOrderReviewActions projectId={projectId} order={selected}
              onSaved={() => setReload((value) => value + 1)} />
            <button className="button button-outline" type="button" onClick={() => setFilePanelOpen((open) => !open)}>
              {filePanelOpen ? 'Hide input files' : 'Manage input files'}</button>
            {filePanelOpen && <div className="work-input-files"><h3>Input files</h3>
              <InputFilePicker files={additionalFiles} onChange={setAdditionalFiles} limit={8}
                disabled={fileAction} label="Add files to this work order" />
              <button className="button button-outline" type="button" disabled={fileAction || !additionalFiles.length}
                onClick={() => { void addFilesToSelected(); }}>{fileAction ? 'Saving files…' : 'Save files'}</button>
              {fileError && <p className="form-error" role="alert">{fileError}</p>}
              {selectedFiles.length > 0 && <ul>{selectedFiles.map((item) =>
                <li key={item.id}>{item.display_name} · {item.status === 'ready_text' ? 'Ready as text' :
                  item.status === 'ready_visual' ? 'Ready as image' : item.status}</li>)}</ul>}
            </div>}
            <div className="detail-facts"><div><span>Assigned profile</span><strong>{profileName(profiles, selected.profile_id)}</strong></div>
              <div><span>Runs</span><strong>{selectedRuns.length}</strong></div></div>
            {recoverableRun && <section className="work-unknown-recovery" aria-label="Unknown live-text run recovery">
              <h3>Unknown live-text run</h3>
              <p>The provider may have completed this call. The saved run and its original task stay unchanged. Editing updates this work order for a new attempt.</p>
              {!editingUnknown ? <button className="button button-outline" type="button" disabled={action !== null}
                onClick={() => { setEditTitle(selected.title); setEditInstructions(selected.instructions); setEditingUnknown(true); }}>
                Edit work order</button> : <form onSubmit={(event) => { void saveUnknownEdit(event); }}>
                <label className="field"><span className="field-label">Title for new attempt</span>
                  <input value={editTitle} maxLength={160} onChange={(event) => setEditTitle(event.target.value)} required /></label>
                <label className="field"><span className="field-label">Instructions for new attempt</span>
                  <textarea value={editInstructions} maxLength={16000} rows={4}
                    onChange={(event) => setEditInstructions(event.target.value)} required /></label>
                <div className="form-actions"><button className="button button-outline" type="button" disabled={action !== null}
                  onClick={() => setEditingUnknown(false)}>Cancel edit</button>
                  <button className="button button-primary" type="submit" disabled={action !== null || !editTitle.trim() ||
                    !editInstructions.trim() || (editTitle.trim() === selected.title && editInstructions.trim() === selected.instructions)}>
                    {action === 'edit-unknown' ? 'Saving…' : 'Save edit'}</button></div>
              </form>}
              {liveTextEligibilityLoading && <p role="status">Checking model availability…</p>}
              {liveTextEligibilityError && <p className="form-error" role="alert">Model availability: {liveTextEligibilityError}</p>}
              {liveToolRouteNote && <p className="quiet-note" role="status">{liveToolRouteNote}</p>}
              {!liveTextEligibilityLoading && !liveTextEligibilityError && !liveTextEligible && !liveToolRouteNote &&
                <p className="quiet-note">Connect a usable text model in Models and Team before starting another attempt.</p>}
              <label className="field"><input type="checkbox" checked={restartAcknowledged}
                onChange={(event) => setRestartAcknowledged(event.target.checked)} /> I understand this starts a new provider call and the earlier call may have completed.</label>
              <button className="button button-primary" type="button" disabled={action !== null || !restartAcknowledged ||
                !liveTextEligible || liveTextEligibilityLoading || editingUnknown} onClick={() => { void startLiveText(selected, recoverableRun); }}>
                {action === 'restart-live-text' ? 'Starting new attempt…' : 'Restart as new live-text attempt'}</button>
            </section>}
            {(selected.status === 'draft' || selected.status === 'ready') && <div className="work-owner-control">
              <label className="field"><span className="field-label">Reassign to profile</span>
                <select value={reassignmentProfileId || selected.profile_id} disabled={action !== null}
                  onChange={(event) => setReassignmentProfileId(event.target.value)}>
                  {profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
                </select></label>
              <button className="button button-outline" type="button" disabled={action !== null || !reassignmentProfileId || reassignmentProfileId === selected.profile_id}
                onClick={() => { void changeOwnership(selected, 'assign'); }}>{action === 'assign' ? 'Saving…' : 'Save assignment'}</button>
            </div>}
            {selected.status === 'blocked' && <p className="work-blocked-detail"><strong>Blocked reason</strong><span>{selected.block_reason}</span>
              <small>Resume returns this work order to {selected.blocked_from_status}.</small></p>}
            {(selected.status === 'draft' || selected.status === 'ready' ||
              (selected.status === 'running' && selectedRuns.some((run) => run.status === 'running' && run.mode === 'manual_planning'))) &&
              <div className="work-block-control">
              <label className="field"><span className="field-label">Reason to block</span>
                <input value={blockReason} maxLength={2000} disabled={action !== null}
                  onChange={(event) => setBlockReason(event.target.value)} placeholder="What is preventing progress?" /></label>
              <button className="button button-outline" type="button" disabled={action !== null || !blockReason.trim()}
                onClick={() => { void changeOwnership(selected, 'block'); }}>{action === 'block' ? 'Saving…' : 'Block'}</button>
            </div>}
            <div className="detail-actions">
              {selected.status === 'blocked' && <button className="button button-primary" type="button" disabled={action !== null}
                onClick={() => { void changeOwnership(selected, 'unblock'); }}>{action === 'unblock' ? 'Saving…' : 'Resume'}</button>}
              {selected.status === 'draft' && <button className="button button-primary" type="button" disabled={action !== null}
                onClick={() => { void transition(selected, 'ready'); }}>{action === 'ready' ? 'Saving…' : 'Mark ready'}</button>}
              {selected.status === 'ready' && <button className="button button-primary" type="button" disabled={action !== null}
                onClick={() => { void transition(selected, 'running'); }}>{action === 'running' ? 'Saving…' : 'Start manual run'}</button>}
              {researcherReady && eligibilityLoading && <span className="quiet-note" role="status">Checking synthetic fixture availability…</span>}
              {researcherReady && eligibilityError && <span className="form-error" role="alert">Fixture availability: {eligibilityError}</span>}
              {researcherReady && eligibility && !eligibility.eligible && <span className="quiet-note">Synthetic text fixture unavailable: {eligibility.reason ?? 'This work order is not eligible.'}</span>}
              {researcherReady && eligibility?.eligible && <button className="button button-outline" type="button" disabled={action !== null}
                onClick={() => { void startSynthetic(selected); }}>{action === 'synthetic' ? 'Running local fixture…' : 'Run synthetic text fixture'}</button>}
              {liveTextCandidate && selected.status === 'ready' && liveTextEligibilityLoading && <span className="quiet-note" role="status">Checking live text availability…</span>}
              {liveTextCandidate && selected.status === 'ready' && liveTextEligibilityError && <span className="form-error" role="alert">Live text availability: {liveTextEligibilityError}</span>}
              {liveTextCandidate && selected.status === 'ready' && liveToolRouteNote && <span className="quiet-note" role="status">{liveToolRouteNote}</span>}
              {liveTextCandidate && selected.status === 'ready' && !liveTextEligibilityLoading && !liveTextEligibilityError && !liveTextEligible &&
                !liveToolRouteNote && <span className="quiet-note">Live text unavailable for this model. Check its connection, catalog, and text runtime on the Models page.</span>}
              {liveTextCandidate && selected.status === 'ready' && liveTextEligible && <button className="button button-primary" type="button" disabled={action !== null}
                onClick={() => { void startLiveText(selected); }}>{action === 'live-text' ? 'Calling provider…' : 'Run live text with provider'}</button>}
              {engineerReady && <div className="external-code-control">
                <h3>Delegate coding</h3>
                <p className="quiet-note">Codex CLI or Claude Code works in a disposable fixture repository using your local CLI login and host access. OneFoundry saves the result for review. The Models-page text account is separate.</p>
                {externalEligibilityLoading && <p className="quiet-note" role="status">Checking coding agent availability…</p>}
                {externalEligibilityError && <p className="form-error" role="alert">Coding agent availability: {externalEligibilityError}</p>}
                {externalEligibility && <>
                  <label className="field"><span className="field-label">Coding backend</span>
                    <select value={codingAdapter} disabled={action !== null} onChange={(event) => setCodingAdapter(event.target.value as CodingAdapterId | '')}>
                      <option value="">Choose a coding backend</option>
                      {externalEligibility.adapters.map((adapter) => <option key={adapter.adapter_id} value={adapter.adapter_id}>
                        {adapter.adapter_id === 'codex_cli' ? 'Codex CLI' : 'Claude Code'}{adapter.available ? '' : ' · unavailable'}
                      </option>)}
                    </select></label>
                  {externalEligibility.reason && <p className="quiet-note">External coding unavailable: {externalEligibility.reason}</p>}
                  {externalEligibility.adapters.map((adapter) => !adapter.available && <p className="quiet-note" key={adapter.adapter_id}>
                    {adapter.adapter_id === 'codex_cli' ? 'Codex CLI' : 'Claude Code'}: {adapter.reason ?? 'Not configured for this work order.'}
                  </p>)}
                  <button className="button button-primary" type="button" disabled={action !== null || codingLaunchUncertain || !codingAdapter ||
                    !externalEligibility.eligible || !externalEligibility.adapters.some((adapter) =>
                      adapter.adapter_id === codingAdapter && adapter.available)}
                    onClick={() => { void startExternalCode(selected); }}>{action === 'external-code' ? 'Queueing coding agent…' : 'Run with coding agent'}</button>
                </>}
              </div>}
              {engineerReady && <details className="fixture-diagnostics"><summary>Diagnostics · offline synthetic code fixture</summary>
                <p className="quiet-note">This scripted fixture tests the Engineer → QA handoff. It does not call Codex CLI, Claude Code, or another coding provider.</p>
                {codeEligibilityLoading && <span className="quiet-note" role="status">Checking synthetic code fixture availability…</span>}
                {codeEligibilityError && <span className="form-error" role="alert">Code fixture availability: {codeEligibilityError}</span>}
                {codeEligibility && !codeEligibility.eligible && <span className="quiet-note">Synthetic code fixture unavailable: {codeEligibility.reason ?? 'This work order is not eligible.'}</span>}
                {codeEligibility?.eligible && <button className="button button-outline" type="button" disabled={action !== null}
                  onClick={() => { void startSyntheticCode(selected); }}>{action === 'synthetic-code' ? 'Queueing local fixture…' : 'Run synthetic Engineer → QA fixture'}</button>}
              </details>}
              {engineerReady && <details className="fixture-diagnostics"><summary>Diagnostics · offline local source check</summary>
                <p className="quiet-note">Checks the exact saved revision of a local repository in a disposable runner. It does not call Codex CLI, Claude Code, or another coding provider, and does not merge changes.</p>
                {offlineLoading && <p role="status">Checking offline source probe eligibility…</p>}
                {offlineError && <p className="form-error" role="alert">Offline source check: {offlineError}</p>}
                {offlineEligibility?.reason && <p className="quiet-note">Offline source check unavailable: {offlineEligibility.reason}</p>}
                {offlineSources.length === 0 && !offlineLoading && <p className="quiet-note">No saved local source. Add one in Integrations.</p>}
                {offlineSources.length > 0 && <label className="field"><span className="field-label">Saved local source</span>
                  <select value={offlineSourceId} disabled={action !== null} onChange={(event) => setOfflineSourceId(event.target.value)}>
                    <option value="">Choose a local source</option>
                    {offlineSources.map((source) => <option key={source.id} value={source.id}>{source.display_name} · {source.branch}</option>)}
                  </select></label>}
                {offlineSourceId && <p className="quiet-note">Selected source: {offlineSources.find((source) => source.id === offlineSourceId)?.relative_path} · branch {offlineSources.find((source) => source.id === offlineSourceId)?.branch} · saved version</p>}
                <button className="button button-outline" type="button" disabled={action !== null || offlineLaunchUncertain || !offlineEligibility?.eligible || !offlineSourceId}
                  onClick={() => { void startOfflineProbe(selected); }}>{action === 'offline-probe' ? 'Queueing offline source probe…' : 'Run offline source probe'}</button>
              </details>}
              {engineerReady && <SnapshotCodeLaunch key={selected.id} projectId={projectId} orderId={selected.id} onOpenRun={onOpenRun} />}
              {selectedRuns.map((run) => <button key={run.id} className="button button-outline" type="button" onClick={() => onOpenRun(run.id)}>
                View run timeline · {run.status}</button>)}
            </div>
            {formError && <p className="form-error" role="alert">{formError}</p>}
            <p className="quiet-note">Live text sends this work order and selected memory to its configured provider. External coding sends the task to the selected coding system and may incur usage. Manual runs record planning only. Synthetic fixtures use local scripted inputs; fixture changes remain in a disposable repository until a dedicated merge approval.</p>
          </article></details>}
        </div>
      )}
    </section>
  );
}
