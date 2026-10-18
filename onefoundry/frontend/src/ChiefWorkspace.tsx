import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import type { Profile, Project } from './api';
import { ApiError } from './api';
import { changeChiefConversation, lookupChiefMessage, sendChiefMessage, type ChiefMessageRequest, type ChiefReceipt } from './chiefWorkspaceApi';
import { useChiefWorkspace } from './useChiefWorkspace';
import { ChiefAgentPanel } from './ChiefAgentPanel';
import { ChiefAgentRecovery } from './ChiefAgentRecovery';
import { ChiefConversationText, isPublicAgentMessage, publicMessage } from './ChiefConversationText';
import { ChiefApprovals } from './ChiefApprovals';
import { ChiefDeliverables } from './ChiefDeliverables';
import { ChiefProgressCard } from './ChiefProgressCard';
import { WorkOrderReviewActions } from './WorkOrderReviewActions';
import { InputFilePicker } from './InputFilePicker';
import { checkInputIdsReady, uploadInputFiles, type PendingInputFile } from './inputFileApi';
import { TaskSessionActivity } from './TaskSessionActivity';
import { getTeamSession } from './taskSessionApi';
import { TaskCapabilityReadiness } from './TaskCapabilityReadiness';
import { ChiefSidebar } from './ChiefSidebar';
import { ConversationAgentSelect } from './ConversationAgentSelect';
import { TaskMessageActions } from './TaskMessageActions';
import { useConversationFollow } from './useConversationFollow';
import type { View } from './workspaceNavigation';

type Props = { project: Project; profiles: Profile[]; initialOrderId: string | null;
  onSelect: (id: string | null) => void; onMenu: () => void; onNavigate: (view: View) => void;
  settingsContent?: ReactNode; integrationsOpen?: boolean };
type PendingIdentity = { id: string; hash: string; orderId: string | null; expectedVersion?: number; expectedSequence?: number; profileId?: string };
async function digest(body: string) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body)))].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}
const secret = /\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|xoxb-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})\b|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S+|\bBearer\s+[A-Za-z0-9._~-]{12,}/i;

export function ChiefWorkspace({ project, profiles, initialOrderId, onSelect, onMenu, onNavigate, settingsContent, integrationsOpen = false }: Props) {
  const state = useChiefWorkspace(project.id, initialOrderId);
  const [body, setBody] = useState('');
  const [draftAgentId, setDraftAgentId] = useState('');
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [rename, setRename] = useState<string | null>(null);
  const [attach, setAttach] = useState(false);
  const [inputFiles, setInputFiles] = useState<PendingInputFile[]>([]);
  const pending = useRef<ChiefMessageRequest | null>(null);
  const restored = useRef<PendingIdentity | null>(null);
  const transcript = useConversationFollow(state.selectedId);
  const composer = useRef<HTMLTextAreaElement>(null);
  const rootRecovery = useRef<HTMLDivElement>(null);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [draftBeforeEdit, setDraftBeforeEdit] = useState<string | null>(null);
  const order = state.rows.find((item) => item.id === state.selectedId);
  const agentId = order ? order.canonical_chief ? '' : order.profile_id : draftAgentId;
  const agentName = agentId ? profiles.find((profile) => profile.id === agentId)?.name ?? order?.profile_name ?? 'Agent' : 'Chief';
  const storageKey = `onefoundry:chief-message:${project.id}`;
  function confirmed(receipt: ChiefReceipt) {
    pending.current = null;
    restored.current = null;
    try { window.sessionStorage.removeItem(storageKey); } catch { /* Exact server lookup still works. */ }
    setBody(''); setEditingMessageId(null); setDraftBeforeEdit(null); setError(null); setNotice(receipt.detail); state.setSelectedId(receipt.order_id);
    onSelect(receipt.order_id); state.refresh();
  }
  useEffect(() => {
    let stopped = false;
    let saved: PendingIdentity | null = null;
    try { saved = JSON.parse(window.sessionStorage.getItem(storageKey) ?? 'null') as PendingIdentity | null; } catch { /* Optional browser persistence. */ }
    if (saved && typeof saved.id === 'string' && typeof saved.hash === 'string' && /^[a-f0-9]{64}$/.test(saved.hash) && (saved.orderId === null || typeof saved.orderId === 'string')) {
      setReconnecting(true);
      void lookupChiefMessage(project.id, saved.id).then((receipt) => {
        if (stopped) return;
        restored.current = null; try { window.sessionStorage.removeItem(storageKey); } catch { /* Optional storage. */ } setReconnecting(false); state.setSelectedId(receipt.order_id); onSelect(receipt.order_id);
        setNotice(receipt.detail ?? 'Your previous message is saved.'); state.refresh();
      }).catch((cause: unknown) => {
        if (stopped) return;
        if (cause instanceof ApiError && cause.status === 404) { restored.current = saved; if (typeof saved?.profileId === 'string') setDraftAgentId(saved.profileId); setReconnecting(false); setError('The previous message is not recorded yet. Re-enter the same text to retry the same request safely.'); }
        else setError('The previous submission is not confirmed. Check the saved conversation before sending another request.');
      });
    }
    return () => { stopped = true; };
    // Reconnect once per project; a route change does not submit another message.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id]);
  useEffect(() => {
    if (!settingsContent && !state.resolving && !order?.archived) composer.current?.focus();
  }, [settingsContent, state.resolving, state.selectedId, order?.archived, agentId]);
  function select(id: string | null) {
    if (busy || pending.current || restored.current) return;
    if (id === null) setDraftAgentId(agentId);
    state.setSelectedId(id); state.setAgentId(null); onSelect(id); setBody(''); setEditingMessageId(null); setDraftBeforeEdit(null); setError(null); setNotice(null);
    setSidebarOpen(false); setAttach(false); setInputFiles([]); composer.current?.focus();
  }
  function selectAgent(id: string) {
    if (busy || state.resolving || reconnecting || pending.current || restored.current || id === agentId) return;
    setDraftAgentId(id); state.setSelectedId(null); state.setAgentId(null); onSelect(null);
    setEditingMessageId(null); setDraftBeforeEdit(null); setError(null); setNotice(null); setSidebarOpen(false); setAttach(false); setInputFiles([]);
  }
  async function copyMessage(text: string) {
    try { await navigator.clipboard.writeText(text); setNotice('Message copied.'); }
    catch { setError('Could not copy the message. Check clipboard access in this browser.'); }
  }
  function editMessage(id: string, text: string) {
    if (busy || reconnecting || state.resolving || pending.current || restored.current || order?.archived) return;
    setDraftBeforeEdit((prior) => prior ?? body); setBody(text); setEditingMessageId(id); setError(null); composer.current?.focus();
  }
  async function send(event: FormEvent) {
    event.preventDefault(); if (busy || reconnecting || state.resolving || !body.trim()) return;
    const text = body.trim();
    if (secret.test(text)) { setError('Remove credentials or secrets before sending.'); return; }
    if (order && !order.canonical_chief && !order.direct_agent) { setError('This is saved historical work. Select an agent above to start a new conversation; its saved history remains here.'); return; }
    if (state.selectedId && (!order || !state.view)) return;
    if (pending.current && pending.current.body !== text) { setError('Confirm the previous submission before editing or sending a different message.'); return; }
    if (restored.current && await digest(text) !== restored.current.hash) { setError('Re-enter the previous message exactly so its saved request can be retried safely.'); return; }
    if (restored.current?.orderId && (!Number.isSafeInteger(restored.current.expectedVersion) || !Number.isSafeInteger(restored.current.expectedSequence))) { setError('The previous request needs its saved conversation checked before it can be retried.'); return; }
    setBusy(true); setError(null);
    try {
      if (inputFiles.length && order) {
        const savedFiles = await uploadInputFiles(project.id, inputFiles, 'work_order', order.id);
        await checkInputIdsReady(project.id, savedFiles.map((file) => file.id)); setInputFiles([]); setAttach(false);
      }
      const data = pending.current ?? (restored.current ? { client_message_id: restored.current.id, body: text,
        ...(restored.current.profileId ? { profile_id: restored.current.profileId } : {}),
        ...(restored.current.orderId ? { order_id: restored.current.orderId, expected_version: restored.current.expectedVersion, expected_sequence: restored.current.expectedSequence } : {}) } : { client_message_id: crypto.randomUUID(), body: text,
        ...(!order && agentId ? { profile_id: agentId } : {}),
        ...(order ? { order_id: order.id, expected_version: order.version, expected_sequence: state.view?.messages.at(-1)?.sequence ?? 0 } : {}) });
      pending.current = data;
      try { window.sessionStorage.setItem(storageKey, JSON.stringify({ id: data.client_message_id, hash: await digest(text), orderId: data.order_id ?? null, expectedVersion: data.expected_version, expectedSequence: data.expected_sequence, profileId: data.profile_id })); } catch { /* In-memory identity remains stable. */ }
      confirmed(await sendChiefMessage(project.id, data));
    } catch (cause) {
      if (pending.current && cause instanceof ApiError && cause.status === null) {
        try { confirmed(await lookupChiefMessage(project.id, pending.current.client_message_id)); }
        catch { setError('Could not confirm your message. Keep this text and use Send again to check the same request.'); }
      } else {
        pending.current = null; restored.current = null; try { window.sessionStorage.removeItem(storageKey); } catch { /* Optional storage. */ }
        setError(cause instanceof Error ? cause.message : 'Message unavailable.'); state.refresh();
      }
    } finally { setBusy(false); }
  }
  async function control(action: 'pause' | 'resume' | 'archive' | 'restore' | 'rename' | 'trash', title?: string, target = order) {
    if (!target || busy || reconnecting || pending.current || restored.current) return;
    setBusy(true); setError(null);
    try {
      await changeChiefConversation(project.id, target, action, title); state.refresh(); setRename(null);
      if (action === 'trash' && target.id === state.selectedId) { state.setSelectedId(null); state.setAgentId(null); onSelect(null); setBody(''); setInputFiles([]); setAttach(false); }
      setNotice(action === 'pause' ? 'Future turns are paused. The current bounded turn can finish.' : action === 'archive' ? 'Conversation archived. Its work and history remain available.' : action === 'trash' ? 'Conversation moved to Trash.' : action === 'restore' && target.trashed ? 'Restored to Archived. Future chat turns remain paused.' : null);
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Conversation action unavailable.'); state.refresh(); }
    finally { setBusy(false); }
  }
  return <div className={`chief-workspace ${sidebarOpen ? 'chief-list-open' : ''}`}>
    <ChiefSidebar projectName={project.name} rows={state.rows} selectedId={state.selectedId} disabled={busy || reconnecting || state.resolving || pending.current !== null || restored.current !== null} settingsOpen={!!settingsContent && !integrationsOpen} integrationsOpen={integrationsOpen}
      onSelect={select} onProjects={onMenu} onSettings={() => { setSidebarOpen(false); onNavigate('setup'); }} onIntegrations={() => { setSidebarOpen(false); onNavigate('extensionHub'); }}
      onArchive={(target) => { void control('archive', undefined, target); }}
      onRestore={(target) => { void control('restore', undefined, target); }}
      onTrash={(target) => { void control('trash', undefined, target); }} />
    {settingsContent && <section className="chief-settings" aria-label={integrationsOpen ? 'Integrations' : 'Settings'}><div className="chief-settings-return"><button className="chief-list-toggle" aria-label={sidebarOpen ? 'Close conversations' : 'Open conversations'} onClick={() => setSidebarOpen(!sidebarOpen)}>☰</button><button aria-label="Back to conversation" onClick={() => { setSidebarOpen(false); onSelect(state.selectedId); }}>← Back to conversation</button></div>{(error || state.error) && <p role="alert">{error ?? state.error}</p>}{notice && <p role="status">{notice}</p>}{settingsContent}</section>}
    <section className="chief-chat" aria-label={`${agentName} conversation`} hidden={!!settingsContent}><header className="chief-chat-header"><button className="chief-list-toggle" aria-label={sidebarOpen ? 'Close conversations' : 'Open conversations'} onClick={() => setSidebarOpen(!sidebarOpen)}>☰</button>
      <div><ConversationAgentSelect profiles={profiles} value={agentId} savedName={order?.profile_name} disabled={busy || state.resolving || reconnecting || pending.current !== null || restored.current !== null} onChange={selectAgent} />
        {order && <h1>{order.title}</h1>}<small>{state.progress?.blocker ? 'Needs attention' : order?.state ?? (agentId ? `Direct conversation with ${agentName}` : 'Chief coordinates the work.')}</small></div>
      {order && <details className="chief-options"><summary aria-label="Conversation actions">···</summary><div><button disabled={busy} onClick={() => setRename(order.title)}>Rename</button>{!order.archived && <button disabled={busy} onClick={() => { void control(order.paused ? 'resume' : 'pause'); }}>{order.paused ? 'Resume' : 'Pause'}</button>}<button disabled={busy} onClick={() => { void control(order.archived ? 'restore' : 'archive'); }}>{order.archived ? 'Restore' : 'Archive'}</button>{order.archived && <button disabled={busy} onClick={() => { void control('trash'); }}>Delete conversation</button>}</div></details>}
    </header>
    {order?.canonical_chief && state.view && <ChiefProgressCard progress={state.progress} error={state.progressError} status={order.status}
      onOpenAgent={(id) => { if (state.view?.session.id === id) rootRecovery.current?.querySelector<HTMLElement>('button:not(:disabled), textarea:not(:disabled)')?.focus();
        else if (state.view?.children.some((child) => child.id === id)) state.setAgentId(id);
        else setError('The affected agent could not be found in this conversation. Refresh the saved work.'); }}
      onMessageChief={() => composer.current?.focus()}
      onReviewApproval={() => transcript.current?.querySelector<HTMLElement>('.chief-approval button')?.focus()}
      onReviewDeliverables={() => transcript.current?.querySelector<HTMLElement>('#chief-deliverables button')?.focus()}
      onReviewOutcome={() => transcript.current?.querySelector<HTMLElement>('.work-review-actions textarea')?.focus()}
      onResume={order.paused && !order.archived ? () => { void control('resume'); } : undefined}
      resumeDisabled={busy || reconnecting || state.resolving || pending.current !== null || restored.current !== null} />}
    {state.view && state.view.children.length > 0 && <nav className="chief-agents" aria-label="Delegated agents">{state.view.children.map((child) => <button key={child.id} onClick={() => state.setAgentId(child.id)}>{profiles.find((item) => item.id === child.profile_id)?.name ?? 'Agent'} <span>↗</span></button>)}</nav>}
    <div className="chief-transcript" ref={transcript}>
      {order?.canonical_chief && state.view && <div ref={rootRecovery}><ChiefAgentRecovery projectId={project.id} orderId={order.id} view={state.view}
        onChanged={async () => { const saved = await getTeamSession(project.id, order.id); state.refresh(); return saved; }} /></div>}
      {!state.selectedId && !state.resolving && <div className="chief-welcome"><span>✳</span><h2>What shall we work on?</h2><p>{agentId ? `Message ${agentName} directly. Your conversation is saved when you send.` : 'Give Chief the objective. Chief plans, delegates, checks the work and brings the result here.'}</p></div>}
      {(state.resolving || state.selectedId && !state.view) && !state.error && <p role="status">Reading saved conversation…</p>}
      {state.view?.messages.filter((item) => item.kind !== 'system' &&
        (item.kind.includes('operator') || isPublicAgentMessage(item.body))).map((item) => {
        if (item.source_session_id && !item.kind.includes('operator')) {
          const child = state.view?.children.find((value) => value.id === item.source_session_id);
          const name = profiles.find((profile) => profile.id === child?.profile_id)?.name ?? 'Agent';
          return <article className="chief-message chief-agent-handoff" key={item.id}>
            <small>{name}</small><p>Delegated result saved for Chief to review.</p>
            {child && <button type="button" onClick={() => state.setAgentId(child.id)}>Open {name} result</button>}
          </article>;
        }
        const operator = item.kind.includes('operator');
        const text = operator ? item.body : publicMessage(item.body);
        return <article className={`chief-message ${operator ? 'from-operator' : ''}`} key={item.id}>
          <small>{operator ? 'You' : item.source_session_id ? profiles.find((profile) => `profile:${profile.id}` === item.actor_id)?.name ?? 'Agent' : item.kind === 'system' ? 'Saved update' : order?.canonical_chief ? 'Chief of Staff' : order?.profile_name ?? 'Agent'}</small>
          {operator ? <div className="chief-message-body">{item.body}</div> : <ChiefConversationText body={item.body} />}
          <TaskMessageActions source={operator ? 'your' : 'agent'} text={text} onCopy={(value) => { void copyMessage(value); }}
            onEdit={item.kind !== 'system' && !order?.archived && !busy && !pending.current && !restored.current ? () => editMessage(item.id, text) : undefined} />
        </article>;
      })}
      <ChiefDeliverables key={state.selectedId} projectId={project.id} files={state.files} />
      <ChiefApprovals projectId={project.id} approvals={state.approvals} onSaved={state.refresh} />
      {state.reviewOrder?.status === 'needs_review' && order?.completion_policy === 'operator_review' && <WorkOrderReviewActions projectId={project.id} order={state.reviewOrder} onSaved={state.refresh} />}
      {state.view && <details className="chief-history" onToggle={(event) => setDetailsOpen(event.currentTarget.open)}>
        <summary>Details & earlier attempts</summary>
        {detailsOpen && state.selectedId && <TaskCapabilityReadiness key={state.selectedId} projectId={project.id} orderId={state.selectedId} />}
        <TaskSessionActivity key={state.view.session.id} view={state.view} profiles={profiles} onOpenSession={state.setAgentId} />
      </details>}
    </div>
    <div className="chief-composer-wrap">{(error || state.error) && <p role="alert">{error ?? state.error} {reconnecting && <button onClick={() => window.location.reload()}>Check saved message</button>}</p>}{notice && <p role="status">{notice}</p>}
      {!notice && order?.detail && <p role="status">{order.detail}</p>}
      {order && !order.archived && !order.canonical_chief && !order.direct_agent && <p>This historical job belongs to {order.profile_name}. Select an agent above to start a new conversation.</p>}
      {rename !== null && <form className="chief-rename" onSubmit={(event) => { event.preventDefault(); void control('rename', rename.trim()); }}><label htmlFor="chief-rename">Conversation title</label><input id="chief-rename" autoFocus maxLength={160} value={rename} onChange={(event) => setRename(event.target.value)} /><button disabled={busy || !rename.trim()}>Save title</button><button type="button" onClick={() => setRename(null)}>Cancel</button></form>}
      {attach && order && <InputFilePicker files={inputFiles} onChange={setInputFiles} limit={30} disabled={busy} label="Files for this conversation" />}
      {editingMessageId && <p role="status">Revising an earlier message in the composer. Send saves a new message; the original stays in this conversation. <button type="button" onClick={() => { setBody(draftBeforeEdit ?? ''); setEditingMessageId(null); setDraftBeforeEdit(null); }}>Cancel edit</button></p>}
      {order?.archived ? <p>Archived conversation. Future chat turns are paused; active work may still finish. <button onClick={() => select(null)}>Start a new conversation</button></p> : <form className="chief-composer" onSubmit={(event) => { void send(event); }}><textarea ref={composer} aria-label={`Message ${agentName}`} placeholder={order?.status === 'failed' || order?.status === 'blocked' ? `Tell ${agentName} how to continue…` : `Message ${agentName}…`} rows={2} value={body} disabled={busy || state.resolving} onChange={(event) => setBody(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} />
        <div><button type="button" aria-label="Attach files" disabled={!order || busy} title={order ? 'Attach files to this saved conversation' : 'Send the first message to save this conversation, then attach files'} onClick={() => setAttach(!attach)}>＋</button><button className="chief-send" type="submit" disabled={busy || reconnecting || state.resolving || !body.trim() || !!state.selectedId && (!order || !state.view)}> {busy ? 'Saving…' : 'Send'} ↑</button></div>
      </form>}<small>{agentId ? `Messages go to ${agentName}. Switch to Chief for team coordination.` : 'Chief handles the team. Approval requests appear here when your decision is needed.'}</small>
    </div></section>
    {state.agentId && state.view && order && <ChiefAgentPanel key={state.agentId} projectId={project.id} orderId={order.id} sessionId={state.agentId} main={state.view} profiles={profiles} files={state.files} onClose={() => state.setAgentId(null)} />}
  </div>;
}
