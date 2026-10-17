import { TeamArtifactsPanel } from './TeamArtifactsPanel';
import { TaskWorkflowPanel } from './TaskWorkflowPanel';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';

import { ApiError, type Profile } from './api';
import { acknowledgeUnknownTurn, createConversation, getConversation, getConversationById, getConversationTurnByClient, listConversations,
  openConversation, sendConversationTurn, type Conversation, type ConversationSummary, type ConversationTurn } from './conversationApi';
import { listRuns, listWorkOrders, type Run, type WorkOrder } from './workApi';
import { workOrderNow, workOrderStatusLabel } from './workOrderActivity';
import { WorkOrderEvidenceView } from './WorkOrderEvidenceView';
import { TaskTeamView } from './TaskTeamView';
import { TaskSessionView } from './TaskSessionView';
import { TaskChatSizeControls, useTaskChatSizing } from './TaskChatSizing';
import { getTeamSession } from './taskSessionApi';
import { TaskTurnWorkLog } from './TaskTurnWorkLog';
import { rejectedAction, visibleReply } from './taskConversationPresentation';
import { useTaskStartWork } from './useTaskStartWork';
import { WorkOrderReviewActions } from './WorkOrderReviewActions';
import { TaskMessageActions } from './TaskMessageActions';
import { useConversationFollow } from './useConversationFollow';
import { ProjectPlanPanel } from './ProjectPlanPanel';
import { InputFilePicker } from './InputFilePicker';
import { checkInputIdsReady, listInputFiles, uploadInputFiles,
  type InputFileMeta, type PendingInputFile } from './inputFileApi';
import { clearSessionKey, readSessionKey } from './brandStorage';

type Props = { projectId: string; profiles: Profile[]; onOpenWorkOrders: () => void; onOpenModels: () => void;
  onOpenAutomation?: () => void;
  initialOrderId?: string | null; embedded?: boolean; onOpenRun?: (runId: string) => void;
  onManageOrder?: (orderId: string) => void; onOpenActivity?: (orderId: string) => void;
  onOpenTask?: (orderId: string) => void };
type PendingSend = { projectId: string; orderId: string; conversationId: string; profileId: string;
  clientTurnId: string; expectedSequence: number; textHash: string; attachmentIds: string[]; discussionOnly?: boolean };
const OBVIOUS_SECRET = /(?:\b(?:sk-[A-Za-z0-9_-]{12,}|sk_live_[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|xoxb-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})\b|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*\S+|\bBearer\s+[A-Za-z0-9._~-]{12,})/i;

function pendingKey(projectId: string, orderId: string): string {
  return `onefoundry:task-chat-pending:${projectId}:${orderId}`;
}

function readPending(projectId: string, orderId: string): PendingSend | null {
  try {
    const raw = readSessionKey(pendingKey(projectId, orderId));
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
    const item = value as Record<string, unknown>;
    if (item.projectId !== projectId || item.orderId !== orderId ||
        !['conversationId', 'profileId', 'clientTurnId'].every((key) => typeof item[key] === 'string') ||
        typeof item.textHash !== 'string' || !/^[a-f0-9]{64}$/.test(item.textHash) ||
        !Number.isSafeInteger(item.expectedSequence) || (item.expectedSequence as number) < 0) return null;
    if (item.attachmentIds !== undefined && (!Array.isArray(item.attachmentIds) ||
        item.attachmentIds.some((id: unknown) => typeof id !== 'string'))) return null;
    if (item.discussionOnly !== undefined && typeof item.discussionOnly !== 'boolean') return null;
    return { ...item, attachmentIds: item.attachmentIds ?? [] } as PendingSend;
  } catch { return null; }
}

function writePending(value: PendingSend | null, projectId: string, orderId: string): boolean {
  try {
    if (value) window.sessionStorage.setItem(pendingKey(projectId, orderId), JSON.stringify(value));
    else clearSessionKey(pendingKey(projectId, orderId));
    return true;
  } catch { return false; }
}

async function hashText(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'The request failed. Check saved state before trying again.';
}

function displayTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function turnOutcome(turn: ConversationTurn): string {
  if (rejectedAction(turn)) return 'Action rejected';
  if (turn.discussion_only && turn.status === 'done') return 'Discussion reply';
  if (turn.status === 'done') return 'Answered';
  if (turn.status === 'failed') return 'Failed';
  if (turn.status === 'unknown') return 'Outcome unknown';
  return 'In progress';
}

function chatState(status: Conversation['status']): string {
  return status === 'unknown' ? 'Outcome uncertain' : status === 'busy' ? 'In progress' : 'Saved';
}

export function TaskChatView({ projectId, profiles, onOpenWorkOrders, onOpenModels,
  initialOrderId, embedded = false, onOpenRun = () => {}, onManageOrder = () => {}, onOpenTask = () => {},
  onOpenAutomation = () => {} }: Props) {
  const [orders, setOrders] = useState<WorkOrder[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(initialOrderId ?? null);
  const [tab, setTab] = useState<'conversation' | 'team' | 'activity' | 'results' | 'details'>('conversation');
  const sizing = useTaskChatSizing();
  const [threadMenuOpen, setThreadMenuOpen] = useState(false);
  const [ordersLoading, setOrdersLoading] = useState(true);
  const [ordersError, setOrdersError] = useState<string | null>(null);
  const [ordersReload, setOrdersReload] = useState(0);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [unopened, setUnopened] = useState(false);
  const [conversationLoading, setConversationLoading] = useState(false);
  const [conversationError, setConversationError] = useState<string | null>(null);
  const [conversationReload, setConversationReload] = useState(0);
  const [teamMode, setTeamMode] = useState<boolean | null>(null);
  const [teamSessionError, setTeamSessionError] = useState<string | null>(null);
  const [history, setHistory] = useState<ConversationSummary[]>([]);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [newChatOpen, setNewChatOpen] = useState(false);
  const [newChatTitle, setNewChatTitle] = useState('');
  const [pendingChatId, setPendingChatId] = useState<string | null>(null);
  const [viewedEarlierId, setViewedEarlierId] = useState<string | null>(null);
  const [earlierConversation, setEarlierConversation] = useState<Conversation | null>(null);
  const [earlierError, setEarlierError] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [editingTurnId, setEditingTurnId] = useState<string | null>(null);
  const [draftBeforeEdit, setDraftBeforeEdit] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [pendingSend, setPendingSend] = useState<PendingSend | null>(null);
  const [localFiles, setLocalFiles] = useState<PendingInputFile[]>([]);
  const [availableFiles, setAvailableFiles] = useState<InputFileMeta[]>([]);
  const [selectedFileIds, setSelectedFileIds] = useState<string[]>([]);
  const [fileStatus, setFileStatus] = useState<string | null>(null);
  const [filePanelOpen, setFilePanelOpen] = useState(false);
  const submissionInFlight = useRef(false);
  const lastInitialOrderId = useRef<string | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const transcriptRef = useConversationFollow(viewedEarlierId ?? conversation?.id ?? null);
  const startWork = useTaskStartWork(projectId, selectedId, (startedOrderId) => {
    if (startedOrderId !== selectedId) return;
    setDraft(''); setEditingTurnId(null); setDraftBeforeEdit(null);
    setNotice('Start request saved. Review team progress for the current attempt and results.');
    setTeamMode(true); setConversationReload((value) => value + 1); setOrdersReload((value) => value + 1);
  });

  function startSelectedWork() {
    const selected = orders.find((order) => order.id === selectedId);
    if (!selected || busy || activePending || conversation?.status === 'busy') return;
    if (localFiles.length || selectedFileIds.length) {
      setSendError('Start work cannot consume these unsaved attachments. Save them to the work order before starting.');
      return;
    }
    if (OBVIOUS_SECRET.test(draft)) {
      setSendError('Remove credentials or secret-like text before starting work.');
      return;
    }
    const message = draft.trim() || 'Start the saved work order. Follow its instructions and acceptance checks. Use admitted tools and report any missing capability.';
    void startWork.start(selected.version, message);
  }

  useEffect(() => { if (editingTurnId) composerRef.current?.focus(); }, [editingTurnId]);

  useEffect(() => {
    if (!initialOrderId) { lastInitialOrderId.current = null; return; }
    if (orders.some((order) => order.id === initialOrderId) && lastInitialOrderId.current !== initialOrderId) {
      lastInitialOrderId.current = initialOrderId;
      setSelectedId(initialOrderId);
      setTab('conversation');
      setViewedEarlierId(null);
    }
  }, [initialOrderId, orders]);

  const updatePending = useCallback((value: PendingSend | null, orderId: string) => {
    const stored = writePending(value, projectId, orderId);
    if (value && !stored) return false;
    setPendingSend(value);
    return stored;
  }, [projectId]);

  useEffect(() => {
    const controller = new AbortController();
    setOrdersLoading(true);
    setOrdersError(null);
    void listWorkOrders(projectId, controller.signal).then((items) => {
      if (controller.signal.aborted) return;
      setOrders(items);
      setSelectedId((prior) => items.some((item) => item.id === prior) ? prior :
        items.some((item) => item.id === initialOrderId) ? initialOrderId ?? null : items[0]?.id ?? null);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      setOrdersError(errorMessage(error));
    }).finally(() => { if (!controller.signal.aborted) setOrdersLoading(false); });
    setRunsError(null);
    void listRuns(projectId, '', controller.signal).then((savedRuns) => {
      if (!controller.signal.aborted) setRuns(savedRuns);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setRunsError(errorMessage(error));
    });
    return () => controller.abort();
  }, [projectId, ordersReload, initialOrderId]);

  useEffect(() => {
    if (!selectedId) { setConversation(null); setUnopened(false); return; }
    const controller = new AbortController();
    const stored = readPending(projectId, selectedId);
    setPendingSend(stored);
    setConversationLoading(true);
    setConversation(null);
    setUnopened(false);
    setConversationError(null);
    setSendError(null);
    setNotice(null);
    setEditingTurnId(null);
    setDraftBeforeEdit(null);
    if (stored) {
      void getConversationTurnByClient(projectId, selectedId, stored.clientTurnId, controller.signal).then((saved) => {
        if (controller.signal.aborted || saved.conversation_id !== stored.conversationId || saved.profile_id !== stored.profileId) return;
        if (saved.turn.status === 'done' || saved.turn.status === 'failed') {
          updatePending(null, selectedId);
          setDraft('');
          setNotice('Earlier submission reconciled from saved state.');
        }
      }).catch((error: unknown) => {
        if (controller.signal.aborted || error instanceof ApiError && error.status === 404) return;
        setSendError(`Could not check the earlier submission: ${errorMessage(error)}. New Send remains paused.`);
      });
    }
    void getConversation(projectId, selectedId, controller.signal).then((saved) => {
      if (controller.signal.aborted) return;
      setConversation(saved);
      if (stored && saved.id === stored.conversationId && saved.profile_id === stored.profileId &&
          saved.status === 'open' && saved.turns.some((item) => item.client_turn_id === stored.clientTurnId && item.status === 'unknown')) {
        updatePending(null, selectedId);
      }
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 404) setUnopened(true);
      else setConversationError(errorMessage(error));
    }).finally(() => { if (!controller.signal.aborted) setConversationLoading(false); });
    return () => controller.abort();
  }, [projectId, selectedId, conversationReload, updatePending]);

  useEffect(() => {
    if (!selectedId) { setTeamMode(false); setTeamSessionError(null); return; }
    const controller = new AbortController();
    setTeamMode(null); setTeamSessionError(null);
    void getTeamSession(projectId, selectedId, undefined, controller.signal).then((saved) => {
      if (controller.signal.aborted) return;
      setTeamMode(saved.session.execution_mode === 'team');
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 404) setTeamMode(false);
      else setTeamSessionError(errorMessage(error));
    });
    return () => controller.abort();
  }, [projectId, selectedId, conversationReload]);

  useEffect(() => { if (teamMode && tab === 'team') setTab('conversation'); }, [teamMode, tab]);

  useEffect(() => {
    if (!selectedId) { setHistory([]); return; }
    const controller = new AbortController();
    setHistoryError(null);
    void listConversations(projectId, selectedId, controller.signal).then((items) => {
      if (!controller.signal.aborted) setHistory(items);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setHistoryError(errorMessage(error));
    });
    return () => controller.abort();
  }, [projectId, selectedId, conversationReload]);

  useEffect(() => {
    if (!selectedId || !filePanelOpen) { setAvailableFiles([]); return; }
    const controller = new AbortController();
    void listInputFiles(projectId, selectedId).then((items) => {
      if (!controller.signal.aborted) setAvailableFiles(items);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setFileStatus(`Files unavailable: ${errorMessage(error)}`);
    });
    return () => controller.abort();
  }, [projectId, selectedId, conversationReload, filePanelOpen]);

  useEffect(() => {
    if (!selectedId || !viewedEarlierId) { setEarlierConversation(null); setEarlierError(null); return; }
    const controller = new AbortController();
    setEarlierConversation(null); setEarlierError(null);
    void getConversationById(projectId, selectedId, viewedEarlierId, controller.signal).then((saved) => {
      if (!controller.signal.aborted) setEarlierConversation(saved);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setEarlierError(errorMessage(error));
    });
    return () => controller.abort();
  }, [projectId, selectedId, viewedEarlierId, conversationReload]);

  useEffect(() => {
    if (!busy || !pendingSend || pendingSend.projectId !== projectId || pendingSend.orderId !== selectedId) return;
    const controller = new AbortController();
    const poll = () => {
      void getConversationTurnByClient(projectId, pendingSend.orderId, pendingSend.clientTurnId, controller.signal)
        .then((saved) => {
          if (controller.signal.aborted || saved.conversation_id !== pendingSend.conversationId ||
              saved.profile_id !== pendingSend.profileId) return;
          return getConversation(projectId, pendingSend.orderId, controller.signal).then((current) => {
            if (!controller.signal.aborted && current.id === pendingSend.conversationId &&
                current.profile_id === pendingSend.profileId) setConversation(current);
          });
        }).catch(() => { /* The send/readback path owns final errors; polling is display-only. */ });
    };
    const first = window.setTimeout(poll, 600);
    const timer = window.setInterval(poll, 2000);
    return () => { controller.abort(); window.clearTimeout(first); window.clearInterval(timer); };
  }, [busy, pendingSend, projectId, selectedId]);

  const selected = orders.find((order) => order.id === selectedId) ?? null;
  const synchronizeOrderStatus = useCallback((status: string) => {
    if (selected && selected.status !== status) setOrdersReload((value) => value + 1);
  }, [selected]);
  const profile = profiles.find((item) => item.id === selected?.profile_id) ?? null;
  const modelReady = !!profile?.model && !!profile.model_account_id && !!profile.instructions.trim();
  const ownerMatches = !!conversation && conversation.profile_id === selected?.profile_id;
  const activePending = pendingSend?.projectId === projectId && pendingSend.orderId === selectedId ? pendingSend : null;
  const pendingTurn = activePending && conversation?.id === activePending.conversationId
    ? conversation.turns.find((item) => item.client_turn_id === activePending.clientTurnId) : null;
  const latestUnknown = conversation?.status === 'unknown' && conversation.turns.at(-1)?.status === 'unknown'
    ? conversation.turns.at(-1) : null;
  const canSend = !!conversation && conversation.status === 'open' && modelReady && ownerMatches && !busy && !activePending;
  const canReenter = !!activePending && !!conversation && conversation.id === activePending.conversationId &&
    conversation.profile_id === activePending.profileId && !pendingTurn && !busy;
  const earlierThreads = history.filter((item) => item.id !== conversation?.id);
  const displayed = viewedEarlierId ? earlierConversation : conversation;
  const displayedProfile = profiles.find((item) => item.id === displayed?.profile_id);
  const selectedRunActive = runs.some((run) => run.work_order_id === selectedId && run.status === 'running');

  async function copyMessage(text: string) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard access is unavailable in this browser.');
      await navigator.clipboard.writeText(text);
      setNotice('Message copied.');
    } catch (error) {
      setSendError(`Could not copy the message: ${errorMessage(error)}`);
    }
  }

  function beginEdit(turn: ConversationTurn) {
    if (!canSend || viewedEarlierId) return;
    setDraftBeforeEdit((current) => current ?? draft);
    setDraft(turn.operator_text);
    setEditingTurnId(turn.id);
    setSendError(null);
  }
  function beginReplyRevision(turn: ConversationTurn, text: string) {
    if (!canSend || viewedEarlierId) return;
    setDraftBeforeEdit((current) => current ?? draft);
    setDraft(text);
    setEditingTurnId(turn.id);
    setSendError(null);
  }

  function cancelEdit() {
    setDraft(draftBeforeEdit ?? '');
    setEditingTurnId(null);
    setDraftBeforeEdit(null);
  }

  useEffect(() => {
    if (!selectedId || (selected?.status !== 'running' && !selectedRunActive)) return;
    const timer = window.setInterval(() => {
      void Promise.all([listWorkOrders(projectId), listRuns(projectId, '')]).then(([savedOrders, savedRuns]) => {
        setOrders(savedOrders);
        setRuns(savedRuns);
        setRunsError(null);
      }).catch((error: unknown) => setRunsError(errorMessage(error)));
    }, 10000);
    return () => window.clearInterval(timer);
  }, [projectId, selectedId, selected?.status, selectedRunActive]);

  async function openSelected() {
    if (!selected) return;
    setBusy(true); setSendError(null); setNotice(null);
    try {
      await openConversation(projectId, selected.id);
      const saved = await getConversation(projectId, selected.id);
      setConversation(saved); setUnopened(false);
      setNotice('Conversation opened and confirmed.');
    } catch (error) {
      try {
        const saved = await getConversation(projectId, selected.id);
        setConversation(saved); setUnopened(false);
        setNotice('Conversation found in saved state.');
      } catch {
        setSendError(`${errorMessage(error)} Open state could not be confirmed; refresh before trying again.`);
      }
    } finally { setBusy(false); }
  }

  async function loadOlderTurns() {
    if (!selectedId || !displayed || loadingOlder || !displayed.turns_omitted) return;
    const owner = displayed;
    setLoadingOlder(true); setOlderError(null);
    try {
      const page = await getConversationById(projectId, selectedId, owner.id, undefined, owner.turns[0].sequence);
      const merge = (current: Conversation | null): Conversation | null => {
        if (current?.id !== owner.id || current.turns[0]?.sequence !== owner.turns[0].sequence) return current;
        return { ...current, turns: [...page.turns, ...current.turns], turns_omitted: page.turns_omitted };
      };
      if (viewedEarlierId) setEarlierConversation(merge); else setConversation(merge);
    } catch (error) { setOlderError(errorMessage(error)); }
    finally { setLoadingOlder(false); }
  }

  async function startNewChat(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected || !newChatTitle.trim() || busy || activePending || conversation?.status !== 'open') return;
    const id = pendingChatId ?? crypto.randomUUID();
    setPendingChatId(id);
    setBusy(true); setSendError(null); setNotice(null);
    try {
      const saved = await createConversation(projectId, selected.id, id, newChatTitle.trim());
      setConversation(saved); setViewedEarlierId(null); setNewChatOpen(false); setNewChatTitle('');
      setPendingChatId(null); setConversationReload((value) => value + 1);
      setNotice('New chat saved under this work order. Earlier chat messages are not included.');
    } catch (error) {
      try {
        const found = (await listConversations(projectId, selected.id)).find((item) => item.id === id);
        if (!found) throw new Error('New chat was not found in saved state.');
        const saved = await getConversationById(projectId, selected.id, id);
        setConversation(saved); setViewedEarlierId(null); setNewChatOpen(false); setNewChatTitle('');
        setPendingChatId(null); setConversationReload((value) => value + 1);
        setNotice('New chat found in saved state.');
      } catch (readbackError) {
        setSendError(`${errorMessage(error)} ${errorMessage(readbackError)} Retry uses the same chat creation ID.`);
      }
    } finally { setBusy(false); }
  }

  async function submitPending(submission: PendingSend, text: string) {
    if (!selected || !conversation || selected.id !== submission.orderId ||
        conversation.id !== submission.conversationId || conversation.profile_id !== submission.profileId) return;
    if (!updatePending(submission, selected.id)) {
      setSendError('This browser could not save the submission ID. Enable session storage before sending.');
      return;
    }
    setBusy(true); setSendError(null); setNotice(null);
    try {
      const result = await sendConversationTurn(projectId, selected.id, {
        conversation_id: submission.conversationId, client_turn_id: submission.clientTurnId,
        expected_sequence: submission.expectedSequence, text, attachment_ids: submission.attachmentIds,
        ...(submission.discussionOnly === undefined ? {} : { discussion_only: submission.discussionOnly }),
      });
      setConversation(result.conversation);
      setDraft('');
      setEditingTurnId(null);
      setDraftBeforeEdit(null);
      setLocalFiles([]); setSelectedFileIds([]);
      if (result.turn.status === 'done' || result.turn.status === 'failed') updatePending(null, selected.id);
      setNotice(rejectedAction(result.turn) ? 'Action rejected. No work or project change was saved by this reply.' :
        result.turn.status === 'done' ? 'Reply saved.' : `Turn saved: ${turnOutcome(result.turn).toLowerCase()}.`);
    } catch (error) {
      try {
        const saved = await getConversationTurnByClient(projectId, selected.id, submission.clientTurnId);
        if (saved.conversation_id !== submission.conversationId || saved.profile_id !== submission.profileId) {
          throw new Error('Saved submission belongs to a different conversation.');
        }
        try { setConversation(await getConversation(projectId, selected.id)); } catch { /* Exact submission readback is authoritative. */ }
        const found = saved.turn;
        if (found.status === 'done' || found.status === 'failed') {
          updatePending(null, selected.id);
          setDraft('');
          setEditingTurnId(null);
          setDraftBeforeEdit(null);
          setSendError(found.status === 'failed'
              ? `The turn failed and is saved. ${found.reason ?? 'Review the recorded outcome before another message.'}`
              : null);
          if (found.status === 'done') setNotice(rejectedAction(found) ? 'Action rejected. No work or project change was saved by this reply.' : 'Reply found in saved state.');
        } else if (found.status === 'running') {
          setDraft('');
          setEditingTurnId(null);
          setDraftBeforeEdit(null);
          setSendError('The turn is still in progress. Refresh its saved state before continuing.');
        } else {
          setDraft('');
          setEditingTurnId(null);
          setDraftBeforeEdit(null);
          setSendError('The provider outcome is unknown. The turn is saved; do not resend it.');
        }
      } catch (readError) {
        setSendError(`${errorMessage(error)} Exact saved-state readback did not confirm a terminal turn: ${errorMessage(readError)}. Keep this submission until its outcome is known.`);
      }
    } finally { setBusy(false); }
  }

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (startWork.busy) return;
    if (!selected || !conversation || !canSend || !draft.trim() || submissionInFlight.current) return;
    const text = draft.trim();
    if (OBVIOUS_SECRET.test(text)) {
      setSendError('This message appears to contain a secret. Remove it before sending.');
      return;
    }
    submissionInFlight.current = true;
    setBusy(true);
    try {
      if (localFiles.length) {
        setFileStatus('Saving selected files locally…');
        await uploadInputFiles(projectId, localFiles, 'staged', selected.id);
        const linked = await listInputFiles(projectId, selected.id);
        if (!localFiles.every((item) => linked.some((file) => file.id === item.id))) {
          throw new ApiError('One or more files were not linked to this work order.', null);
        }
        setAvailableFiles(linked);
      }
      const attachmentIds = Array.from(new Set([...selectedFileIds, ...localFiles.map((item) => item.id)]));
      if (attachmentIds.length > 8) throw new ApiError('Choose at most eight files for one message.', null);
      await checkInputIdsReady(projectId, attachmentIds);
      setFileStatus(null);
      const textHash = await hashText(text);
      await submitPending({ projectId, orderId: selected.id, conversationId: conversation.id,
        profileId: conversation.profile_id, clientTurnId: crypto.randomUUID(),
        expectedSequence: conversation.latest_sequence ?? conversation.turns.at(-1)?.sequence ?? 0,
        textHash, attachmentIds, discussionOnly: true }, text);
    } catch (error) {
      setSendError(`Could not prepare this submission safely: ${errorMessage(error)}`);
      setFileStatus(errorMessage(error));
    } finally { submissionInFlight.current = false; setBusy(false); }
  }

  async function retryPending() {
    if (!activePending || !conversation || conversation.id !== activePending.conversationId ||
        conversation.profile_id !== activePending.profileId || pendingTurn || busy || submissionInFlight.current) return;
    if (!draft.trim()) { setSendError('Re-enter the exact original message to retry this submission.'); return; }
    submissionInFlight.current = true;
    setBusy(true);
    try {
      if (await hashText(draft.trim()) !== activePending.textHash) {
        setSendError('The message does not match the original submission. Re-enter its exact text.');
        return;
      }
      await submitPending(activePending, draft.trim());
    } catch (error) {
      setSendError(`Could not verify the original message: ${errorMessage(error)}`);
    } finally { submissionInFlight.current = false; setBusy(false); }
  }

  async function continueAfterUnknown() {
    if (!selected || !conversation || !ownerMatches || !latestUnknown || busy) return;
    const expectedId = conversation.id;
    const expectedProfileId = conversation.profile_id;
    setBusy(true); setSendError(null); setNotice(null);
    try {
      let saved: Conversation;
      try {
        saved = await acknowledgeUnknownTurn(projectId, selected.id, latestUnknown.id);
      } catch (error) {
        try { saved = await getConversation(projectId, selected.id); }
        catch {
          throw new Error(`${errorMessage(error)} Saved acknowledgement could not be confirmed; refresh before trying again.`);
        }
        if (saved.status === 'unknown') throw error;
      }
      if (saved.id !== expectedId || saved.profile_id !== expectedProfileId ||
          !saved.turns.some((item) => item.id === latestUnknown.id && item.status === 'unknown')) {
        throw new Error('Saved acknowledgement belongs to a different conversation. Refresh before continuing.');
      }
      setConversation(saved);
      if (activePending?.clientTurnId === latestUnknown.client_turn_id) updatePending(null, selected.id);
      if (saved.status === 'open') {
        setNotice('Earlier outcome remains unknown. You can send a new message; the earlier turn was not resent.');
      } else {
        setNotice('Saved conversation changed in another session. Refresh before sending another message.');
      }
    } catch (error) {
      setSendError(`${errorMessage(error)} Check saved state before continuing.`);
    } finally { setBusy(false); }
  }

  function refresh() {
    setConversationReload((value) => value + 1);
    setOrdersReload((value) => value + 1);
  }

  return <section ref={sizing.frameRef} style={sizing.style} onKeyDown={sizing.keyDown}
    role={sizing.expanded ? 'dialog' : undefined} aria-modal={sizing.expanded ? true : undefined}
    aria-labelledby={sizing.expanded ? 'task-chat-heading' : undefined}
    className={`task-chat-page ${sizing.expanded ? 'is-focus' : ''}`} aria-label={embedded ? 'Work order conversation' : 'Task'}>
    {ordersLoading && <p className="loading-panel" role="status">Loading work orders…</p>}
    {ordersError && <div className="empty-inline"><p role="alert">{ordersError}</p><button className="button button-outline" type="button" onClick={() => setOrdersReload((value) => value + 1)}>Try again</button></div>}
    {!ordersLoading && !ordersError && orders.length === 0 && <div className="empty-inline"><p>No work orders yet. Create one to start a task chat.</p>
      <button className="button button-primary" type="button" onClick={onOpenWorkOrders}>Create work order</button></div>}
    {!ordersLoading && !ordersError && orders.length > 0 && <div className={`task-chat-layout ${embedded ? 'task-chat-embedded' : ''}`}>
      <div className="task-chat-panel panel">
        {selected && <><header className="task-chat-panel-header">
          <div><button className="task-chat-back" type="button" onClick={() => { sizing.collapse(); onOpenWorkOrders(); }}>← All work orders</button>
            <p className="eyebrow">Task</p><h1 id="task-chat-heading">{selected.title}</h1>
            <span className={`status-chip status-${selected.status}`}>{workOrderStatusLabel(selected.status)}</span>
            <span className="task-chat-owner">Assigned to <strong>{profile?.name ?? 'Unknown profile'}</strong></span></div>
          <div className="task-chat-header-actions">
            <TaskChatSizeControls sizing={sizing} />
            <button className="task-chat-refresh" type="button" disabled={busy || conversationLoading} onClick={refresh} title="Refresh saved state" aria-label="Refresh saved state">↻</button>
          </div>
        </header>
        <div className="task-now" role="status"><strong>Now:</strong> {workOrderNow(selected, runs).now}
          <span><strong>Next:</strong> {workOrderNow(selected, runs).next}</span>
          {runsError && <span>Run summary unavailable: {runsError}</span>}</div>
        {['done', 'needs_review'].includes(selected.status) && <details className="task-review-controls">
          <summary>{selected.status === 'done' ? 'Reopen completed work' : 'Operator acceptance'}</summary>
          <WorkOrderReviewActions projectId={projectId} order={selected}
            onSaved={() => setOrdersReload((value) => value + 1)} />
        </details>}
        <TaskWorkflowPanel projectId={projectId} orderId={selected.id} version={selected.version}
          reloadKey={ordersReload + conversationReload} onActivated={() => setConversationReload((value) => value + 1)}
          onStatusRead={synchronizeOrderStatus} onStartWork={startSelectedWork}
          startBusy={startWork.busy || busy || !!activePending || conversation?.status === 'busy'} startError={startWork.error} />
        <nav className="task-main-tabs" aria-label="Task sections">
          {(['conversation', 'team', 'activity', 'results', 'details'] as const).filter((item) => item !== 'team' || !teamMode).map((item) =>
            <button key={item} type="button" aria-current={tab === item ? 'page' : undefined}
              onClick={() => setTab(item)}>{item === 'conversation' ? 'Conversation' : item === 'team' ? 'Team' : item === 'activity' ? 'Progress' : item === 'results' ? 'Results' : 'Assignment'}</button>)}
        </nav>
        {tab === 'conversation' && teamMode === null && !teamSessionError && <p className="loading-panel" role="status">Loading task session…</p>}
        {tab === 'conversation' && teamSessionError && <p className="form-error" role="alert">Could not check the task session: {teamSessionError}</p>}
        {tab === 'conversation' && teamMode && <TaskSessionView key={selected.id} projectId={projectId} orderId={selected.id}
          profiles={profiles} reloadKey={conversationReload} onOpenRun={onOpenRun} />}
        {tab === 'conversation' && teamMode === false && <div className="task-chat-workspace">
        <button className="task-chat-mobile-threads" type="button" aria-expanded={threadMenuOpen}
          onClick={() => setThreadMenuOpen((open) => !open)}>Conversations {threadMenuOpen ? '▴' : '▾'}</button>
        <aside className={`task-chat-threads ${threadMenuOpen ? 'is-open' : ''}`}>
          <p className="task-chat-threads-heading">Conversations</p>
        {(conversation || earlierThreads.length > 0 || historyError) && <nav className="task-chat-thread-bar" aria-label="Work order chats">
          <button type="button" aria-current={!viewedEarlierId ? 'page' : undefined} onClick={() => { setViewedEarlierId(null); setThreadMenuOpen(false); }}>
            {conversation?.title ?? 'Current chat'} · {profile?.name ?? 'assigned profile'}</button>
          {earlierThreads.map((thread) => <button key={thread.id} type="button" aria-current={viewedEarlierId === thread.id ? 'page' : undefined}
            onClick={() => { setViewedEarlierId(thread.id); setThreadMenuOpen(false); }}>{thread.title} · {profiles.find((item) => item.id === thread.profile_id)?.name ?? 'Former agent'}
            <span> · {chatState(thread.status)}</span></button>)}
          <button type="button" disabled={busy || !!activePending || conversation?.status !== 'open'}
            onClick={() => setNewChatOpen((open) => !open)}>+ New chat</button>
          {historyError && <span role="alert">Earlier threads unavailable: {historyError}</span>}
        </nav>}
        </aside>
        <div className="task-chat-conversation">
        {newChatOpen && <form className="task-chat-new" onSubmit={(event) => { void startNewChat(event); }}>
          <label htmlFor="new-chat-title">New chat name</label>
          <input id="new-chat-title" maxLength={120} value={newChatTitle} disabled={!!pendingChatId}
            onChange={(event) => setNewChatTitle(event.target.value)}
            placeholder="Example: Review source validation" />
          <button className="button button-primary" type="submit" disabled={busy || !newChatTitle.trim()}>Create chat</button>
          <button className="button button-quiet" type="button" disabled={busy} onClick={() => setNewChatOpen(false)}>Cancel</button>
          <small>The new chat starts with this work order's brief. Earlier messages stay in their chat.</small>
        </form>}
        {notice && <p className="notice" role="status">{notice}</p>}
        {sendError && <p className="form-error task-chat-message-error" role="alert">{sendError}</p>}
        {conversationLoading && <p className="task-chat-state" role="status">Loading saved conversation…</p>}
        {conversationError && <p className="task-chat-state form-error" role="alert">{conversationError}</p>}
        {viewedEarlierId && !earlierConversation && !earlierError && <p className="task-chat-state" role="status">Loading earlier role thread…</p>}
        {earlierError && <p className="task-chat-state form-error" role="alert">Earlier thread unavailable: {earlierError}</p>}
        {activePending && !conversationLoading && !conversation && <p className="task-chat-state form-error" role="alert">An earlier submission is not confirmed. It remains bound to its original conversation. Do not send a new message for this work order.</p>}
        {unopened && !viewedEarlierId && !conversationLoading && <div className="task-chat-empty"><span aria-hidden="true">✳</span><h3>Start a conversation for this task</h3>
          <p>The thread will stay attached to this work order and its assigned profile.</p>
          <button className="button button-primary" type="button" disabled={busy} onClick={() => { void openSelected(); }}>{busy ? 'Opening…' : 'Open conversation'}</button></div>}
        {displayed && !conversationLoading && <>
          {viewedEarlierId && <p className="task-chat-readonly">{displayed.title} with {displayedProfile?.name ?? 'a former profile'} · {chatState(displayed.status)}. Saved transcript only; new messages go to the current chat.</p>}
          {!!displayed.turns_omitted && <p className="task-chat-guidance">
            Showing recent turns. {displayed.turns_omitted} earlier turns remain saved.
            <button type="button" disabled={loadingOlder} onClick={() => { void loadOlderTurns(); }}>
              {loadingOlder ? 'Loading earlier turns…' : 'Load earlier turns'}</button></p>}
          {olderError && <p role="alert">Earlier turns unavailable: {olderError}</p>}
          <div className="task-chat-transcript" ref={transcriptRef} aria-label="Saved conversation turns">
            {displayed.turns.length === 0 && !(activePending && busy) && <div className="task-chat-empty"><span aria-hidden="true">✳</span><h3>Conversation ready</h3><p>Write a task question below to ask {profile?.name ?? 'the assigned profile'}.</p></div>}
            {displayed.turns.map((turn) => <div className="task-chat-turn" key={turn.id}>
              <article className="task-chat-bubble task-chat-operator"><div><strong>{turn.source_kind === 'system_followup' ? 'Project update' : 'You'}</strong><time dateTime={turn.created_at}>{displayTime(turn.created_at)}</time></div><p>{turn.operator_text}</p>
                <TaskMessageActions source="your" text={turn.operator_text} onCopy={(text) => { void copyMessage(text); }}
                  onEdit={!viewedEarlierId && canSend && turn.source_kind === 'operator_ui' ? () => beginEdit(turn) : undefined} />
              </article>
              {turn.attachments.length > 0 && <p className="task-chat-attachments">Attached: {turn.attachments.map((item) => item.display_name).join(', ')}</p>}
              <article className={`task-chat-bubble task-chat-agent status-${turn.status}`}><div><strong>{displayedProfile?.name ?? 'Agent'}</strong><span>{turnOutcome(turn)}</span></div>
                {turn.progress_updates.length > 0 && <div className="task-chat-progress" aria-label="Agent progress summaries">
                  <strong>Agent updates</strong><ol>{turn.progress_updates.map((update, index) => <li key={index}>{update}</li>)}</ol>
                </div>}
                {turn.status === 'running' && turn.progress_updates.length === 0 && <p>Waiting for an agent update…</p>}
                {turn.discussion_only && <p className="task-chat-guidance">Discussion only. This reply starts no work or project changes. Use Start work to queue the team.</p>}
                {visibleReply(turn) && <p>{visibleReply(turn)}</p>}
                {turn.automation_decision?.status === 'saved' && <p className="notice" role="status">
                  {turn.automation_decision.rule_id ? `Saved Automation rule version ${turn.automation_decision.rule_version}.` :
                    turn.automation_decision.assigned_work_order_id ? `Saved ${turn.automation_decision.assignments.length || 1} agent assignment${turn.automation_decision.assignments.length === 1 ? '' : 's'}. Run outcomes are separate.` :
                    turn.automation_decision.plan_version ? `Saved project plan version ${turn.automation_decision.plan_version}.` :
                    `Saved project goal version ${turn.automation_decision.project_version}.`}{' '}
                  {turn.automation_decision.rule_id && <button type="button" onClick={onOpenAutomation}>Open Automation</button>}
                  {turn.automation_decision.rule_id && turn.automation_decision.project_version &&
                    <span> Project goal version {turn.automation_decision.project_version} saved too.</span>}
                  {turn.automation_decision.rule_id && turn.automation_decision.plan_version &&
                    <span> Project plan version {turn.automation_decision.plan_version} saved too.</span>}
                  {!turn.automation_decision.rule_id && turn.automation_decision.plan_version &&
                    turn.automation_decision.project_version &&
                    <span> Project goal version {turn.automation_decision.project_version} saved too.</span>}
                  {turn.automation_decision.assigned_work_order_id && turn.automation_decision.assignments.length === 0 && <button type="button"
                    onClick={() => onManageOrder(turn.automation_decision!.assigned_work_order_id!)}>Open assignment</button>}
                </p>}
                {turn.automation_decision?.assignments.length ? <div className="task-chat-assignments" aria-label="Saved team assignments">
                  {turn.automation_decision.assignments.map((assignment) => <button key={assignment.id} type="button"
                    onClick={() => onManageOrder(assignment.id)}>
                    {assignment.profile_name}: {assignment.title} · {assignment.status}
                  </button>)}
                </div> : null}
                {turn.automation_decision?.status === 'rejected' && <p className="form-error" role="alert">Requested change was not saved.</p>}
                {turn.automation_decision?.status === 'unavailable' &&
                  (turn.automation_decision.error_code === 'invalid_action_proposal' ||
                    turn.automation_decision.error_code === 'invalid_decision_format' && turn.assistant_text?.trimStart().startsWith('{')) &&
                  <p className="task-chat-guidance" role="status">The proposed action was invalid. No team assignment, rule, or project change was saved.</p>}
                {visibleReply(turn) && <TaskMessageActions source="agent" text={visibleReply(turn)!}
                  onCopy={(text) => { void copyMessage(text); }}
                  onEdit={!viewedEarlierId && canSend ? () => beginReplyRevision(turn, visibleReply(turn)!) : undefined} />}
                {turn.reason && turn.status !== 'done' && <p className="task-chat-reason">{turn.reason === 'provider_outcome_unknown'
                  ? 'The provider connection ended before a complete reply was saved. Its outcome is unknown; continue the conversation without resending this message.'
                  : turn.reason.replaceAll('_', ' ')}</p>}
                <TaskTurnWorkLog turn={turn} />
                <details className="task-chat-turn-details"><summary>Reply details</summary>
                {turn.automation_decision?.validation_errors?.map((issue, index) =>
                  <small key={index}>Proposal field {issue.field}: {issue.code.replaceAll('_', ' ')}</small>)}
                {turn.assistant_text !== visibleReply(turn) && <pre>{turn.assistant_text}</pre>}
                {turn.diagnostic_code && <small>Response issue: {turn.diagnostic_code.replaceAll('_', ' ')}</small>}
                <small>{turn.provider} · {turn.model_id}
                  {turn.input_tokens !== null && turn.output_tokens !== null ? ` · ${turn.input_tokens + turn.output_tokens} tokens` : ''}
                  {turn.status === 'done' ? ` · ${turn.cost_usd === null ? 'Cost unavailable' : `Cost $${turn.cost_usd}`}` : ''}</small>
                <small>Skills: {turn.selected_skills.length === 0 ? 'No saved selection' : turn.selected_skills.map((skill) =>
                  `${skill.selection_source === 'automatic' ? 'Automatic' : 'Pinned'} · ${skill.skill_name ?? 'Saved procedure'} v${skill.version}`).join('; ')}</small>
                {turn.used_sections.length > 0 && <small>File inputs used: {turn.used_sections.map((section) =>
                  `${section.kind === 'visual' ? 'Image' : 'Section'}: ${section.source_label}`).join('; ')}
                  {turn.input_sections_omitted ? ` · ${turn.input_sections_omitted} omitted` : ''}</small>}
                </details>
              </article>
            </div>)}
            {!viewedEarlierId && activePending && busy && !pendingTurn && draft.trim() &&
              conversation?.id === activePending.conversationId && <div className="task-chat-turn" aria-label="Message being sent">
                <article className="task-chat-bubble task-chat-operator"><div><strong>You</strong><span>Sending</span></div><p>{draft.trim()}</p></article>
                <article className="task-chat-bubble task-chat-agent status-running"><div><strong>{profile?.name ?? 'Agent'}</strong><span>Working</span></div>
                  <p>Saving request and connecting to the agent…</p></article>
              </div>}
          </div>
          {!viewedEarlierId && conversation && <div className="task-chat-compose">
            {activePending && (conversation.id !== activePending.conversationId || conversation.profile_id !== activePending.profileId) &&
              <p className="task-chat-guidance" role="alert">An unconfirmed submission belongs to a different assigned profile. It cannot be replayed in this conversation.</p>}
            {activePending && !busy && conversation.id === activePending.conversationId && conversation.profile_id === activePending.profileId && !pendingTurn &&
              <p className="task-chat-guidance" role="status">Your previous message keeps its original submission ID. Ordinary Send is paused until its saved outcome is known. Re-enter its exact text to retry after a reload.
                <button type="button" disabled={busy} onClick={() => { void retryPending(); }}>Retry same submission</button></p>}
            {!modelReady && <p className="task-chat-guidance">This profile needs instructions and a connected, discovered text model. Choose a model in Team after connecting an AI account. <button type="button" onClick={onOpenModels}>Open AI connections</button></p>}
            {!ownerMatches && <p className="task-chat-guidance">This conversation belongs to the previously assigned profile. Review the work order assignment before sending.</p>}
            {conversation.status === 'busy' && <p className="task-chat-guidance">A turn is in progress. Refresh saved state to check its outcome.</p>}
            {latestUnknown && <p className="task-chat-guidance">The previous reply was not saved and may have completed. Continue in this work order without resending that message. Only confirmed replies will be used as history.{' '}
              <button type="button" disabled={busy || !ownerMatches} onClick={() => { void continueAfterUnknown(); }}>Continue conversation</button></p>}
            <form onSubmit={(event) => { void send(event); }}>
              {editingTurnId && <div className="task-chat-editing" role="status">
                <span>Revising earlier text. Send saves this as a new message; the original remains in the conversation.</span>
                <button type="button" onClick={cancelEdit}>Cancel edit</button>
              </div>}
              {filePanelOpen && <InputFilePicker files={localFiles} onChange={setLocalFiles} limit={8}
                disabled={!canSend || busy} label="Attach files to this message" status={fileStatus} />}
              {filePanelOpen && availableFiles.length > 0 && <fieldset className="task-chat-file-selection" disabled={!canSend || busy}>
                <legend>Use saved files in this message</legend>
                {availableFiles.map((item) => <label key={item.id}><input type="checkbox"
                  checked={selectedFileIds.includes(item.id)} onChange={(event) =>
                    setSelectedFileIds((ids) => event.target.checked ? [...ids, item.id] : ids.filter((id) => id !== item.id))} />
                  {item.display_name} · {item.status === 'ready_text' ? 'Ready as text' : item.status === 'ready_visual' ? 'Ready as image' : item.status}
                </label>)}
              </fieldset>}
              <div className="task-chat-compose-control">
                <button type="button" className="task-chat-attach-icon" aria-label={filePanelOpen ? 'Hide files' : 'Attach files'}
                  title={filePanelOpen ? 'Hide files' : 'Attach or use saved files'} aria-expanded={filePanelOpen}
                  onClick={() => setFilePanelOpen((open) => !open)}>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
                    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="m20 11-8.6 8.6a5 5 0 0 1-7.1-7.1L13 3.8a3.5 3.5 0 0 1 5 5L9.3 17.5a2 2 0 0 1-2.8-2.8l8.5-8.5" />
                  </svg>
                </button>
                <label className="task-chat-compose-label" htmlFor="task-chat-draft">{canReenter ? 'Re-enter original message' : `Message to ${profile?.name ?? 'agent'}`}</label>
                <textarea id="task-chat-draft" ref={composerRef} value={draft} onChange={(event) => setDraft(event.target.value)} rows={2}
                  placeholder={canReenter ? 'Re-enter the exact original text for a safe retry' : `Message ${profile?.name ?? 'agent'}…`}
                  disabled={!canSend && !canReenter} />
                <button className="button button-primary task-chat-send" type="submit" aria-label="Send message"
                  disabled={!canSend || startWork.busy || !draft.trim()}>{busy ? activePending ? 'Working…' : 'Preparing…' : 'Send'}</button>
              </div>
              <small className="task-chat-compose-note">Messages stay with this task. The reply appears here when the agent finishes.</small>
            </form>
          </div>}
        </>}
        </div>
        <aside className="task-chat-context" aria-label="Task overview">
          <h2>Task overview</h2>
          <p><strong>Now</strong>{workOrderNow(selected, runs).now}</p>
          <p><strong>Next step</strong>{workOrderNow(selected, runs).next}</p>
          <WorkOrderEvidenceView projectId={projectId} order={selected}
            profiles={profiles} mode="conversation" onOpenRun={onOpenRun} />
          <button className="button button-outline" type="button" onClick={() => setTab('activity')}>View progress →</button>
        </aside>
        </div>}
        {tab !== 'conversation' && <div className="task-chat-secondary">
          {tab === 'team' && !teamMode && <TaskTeamView projectId={projectId} order={selected} orders={orders} runs={runs}
            reloadKey={ordersReload}
            profiles={profiles} onOpenTask={onOpenTask} onOpenRun={onOpenRun} />}
          {tab === 'details' && <><ProjectPlanPanel projectId={projectId} />
            <section className="task-chat-brief"><h2>Work order brief</h2><p>{selected.instructions}</p></section>
            <p className="task-chat-boundary">Chat uses the profile’s configured text model. It does not start coding agents or approve outbound actions.</p></>}
          {tab === 'results' && teamMode && <TeamArtifactsPanel projectId={projectId} orderId={selected.id} />}
          {tab !== 'team' && !(tab === 'results' && teamMode) && <WorkOrderEvidenceView projectId={projectId} order={selected}
            profiles={profiles} mode={tab} onOpenRun={onOpenRun} />}
          {tab === 'details' && <div className="task-detail-actions"><button className="button button-outline" type="button"
            onClick={() => onManageOrder(selected.id)}>Manage assignment and run options</button></div>}
        </div>}
        </>}
        {sizing.resizeHandle}
      </div>
    </div>}
  </section>;
}
