import { useEffect, useMemo, useState } from 'react';

import type { Profile } from './api';
import { getConversationById, listConversations, type Conversation, type ConversationSummary } from './conversationApi';
import type { Run, WorkOrder } from './workApi';
import { workOrderStatusLabel } from './workOrderActivity';
import { visibleReply } from './taskConversationPresentation';

type Props = { projectId: string; order: WorkOrder; orders: WorkOrder[]; runs: Run[]; profiles: Profile[];
  reloadKey?: number;
  onOpenTask: (orderId: string) => void; onOpenRun: (runId: string) => void };

function relatedOrders(selected: WorkOrder, orders: WorkOrder[]): WorkOrder[] {
  const byId = new Map(orders.map((order) => [order.id, order]));
  let root = selected;
  const seen = new Set<string>([selected.id]);
  while (root.parent_work_order_id && byId.has(root.parent_work_order_id) && !seen.has(root.parent_work_order_id)) {
    root = byId.get(root.parent_work_order_id)!;
    seen.add(root.id);
  }
  const family = [root];
  const included = new Set<string>([root.id]);
  for (let index = 0; index < family.length; index += 1) {
    for (const order of orders) {
      if (order.parent_work_order_id === family[index].id && !included.has(order.id)) {
        family.push(order);
        included.add(order.id);
      }
    }
  }
  return family;
}

function replyStatus(status: string): string {
  return status === 'done' ? 'Saved reply' : status === 'running' ? 'Working' :
    status === 'failed' ? 'Failed' : 'Outcome unknown';
}

function runStatus(status: Run['status']): string {
  return status === 'done' ? 'Completed' : status === 'running' ? 'Working' :
    status === 'failed' ? 'Failed' : 'Outcome unknown';
}

function requestSource(source: Conversation['turns'][number]['source_kind']): string {
  return source === 'system_followup' ? 'Project follow-up' : source === 'system_triage' ? 'System triage' :
    source === 'discord' ? 'Operator via Discord' : source === 'wechat' ? 'Operator via WeChat' : 'Operator request';
}

export function TaskTeamView({ projectId, order, orders, runs, profiles, reloadKey = 0, onOpenTask, onOpenRun }: Props) {
  const family = useMemo(() => relatedOrders(order, orders), [order, orders]);
  const [expandedOrderId, setExpandedOrderId] = useState<string | null>(null);
  const [threads, setThreads] = useState<ConversationSummary[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [loading, setLoading] = useState(false);
  const [conversationLoading, setConversationLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const conversationAgent = conversation ?
    profiles.find((profile) => profile.id === conversation.profile_id)?.name ?? 'Unknown agent' : null;

  useEffect(() => {
    if (!expandedOrderId) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setConversation(null);
    void listConversations(projectId, expandedOrderId, controller.signal).then((savedThreads) => {
      if (controller.signal.aborted) return;
      setThreads(savedThreads);
      setThreadId(savedThreads[0]?.id ?? null);
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Saved chats could not be read.');
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [projectId, expandedOrderId, reloadKey]);

  useEffect(() => {
    if (!expandedOrderId || !threadId) return;
    const controller = new AbortController();
    setConversationLoading(true);
    setConversation(null);
    setError(null);
    void getConversationById(projectId, expandedOrderId, threadId, controller.signal).then((saved) => {
      if (!controller.signal.aborted) setConversation(saved);
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Saved chat could not be read.');
    }).finally(() => { if (!controller.signal.aborted) setConversationLoading(false); });
    return () => controller.abort();
  }, [projectId, expandedOrderId, threadId, reloadKey]);

  function toggleChats(orderId: string) {
    setExpandedOrderId((prior) => prior === orderId ? null : orderId);
    setThreadId(null);
    setThreads([]);
    setConversation(null);
    setError(null);
  }

  return <section className="task-team" aria-label="Team work and saved chats">
    <h2>Team work</h2>
    <p className="task-team-intro">Saved assignments, each agent’s task chat, and recorded runs for this work order.</p>
    {family.length === 1 && <p className="task-team-empty">No team assignments are saved for this task. A plan or proposed owner in a chat reply is not a handoff.</p>}
    <div className="task-team-list">{family.map((item, index) => {
      const agent = profiles.find((profile) => profile.id === item.profile_id)?.name ?? 'Unknown agent';
      const itemRuns = runs.filter((run) => run.work_order_id === item.id);
      const expanded = expandedOrderId === item.id;
      const brief = item.instructions.replace(/\s+/g, ' ').trim();
      return <article className="task-team-card" aria-label={item.title} key={item.id}>
        <div className="task-team-card-heading"><div>
          <small>{index === 0 ? 'Main task' : 'Saved assignment'}</small>
          <h3>{item.title}</h3>
          <p>{agent} · {workOrderStatusLabel(item.status)}</p>
        </div><span className={`status-chip status-${item.status}`}>{workOrderStatusLabel(item.status)}</span></div>
        <p className="task-team-instructions">{brief.length > 240 ? `${brief.slice(0, 240).trimEnd()}…` : item.instructions}</p>
        {brief.length > 240 && <details className="task-team-full-brief"><summary>Full assignment</summary>
          <pre>{item.instructions}</pre></details>}
        {item.acceptance_checks.length > 0 && <p className="task-team-checks">Completion check: {item.acceptance_checks.join('; ')}</p>}
        <div className="task-team-actions">
          <button type="button" className="button button-outline" onClick={() => onOpenTask(item.id)}>Open task</button>
          <button type="button" className="button button-outline" aria-expanded={expanded} onClick={() => toggleChats(item.id)}>
            {expanded ? 'Hide saved chats' : 'View saved chats'}</button>
          {itemRuns.map((run, runIndex) => <button key={run.id} type="button" className="button button-outline"
            onClick={() => onOpenRun(run.id)}>Open run {runIndex + 1} · {runStatus(run.status)}</button>)}
        </div>
        {expanded && <div className="task-team-conversations" aria-label={`${item.title} saved chats`}>
          {loading && <p role="status">Loading saved chats…</p>}
          {conversationLoading && <p role="status">Loading selected chat…</p>}
          {error && <p className="form-error" role="alert">Saved chats unavailable: {error}</p>}
          {!loading && !error && threads.length === 0 && <p>No saved chats for this assignment.</p>}
          {threads.length > 1 && <div className="task-team-thread-choices" aria-label="Saved chat choices">
            {threads.map((thread) => <button key={thread.id} type="button" aria-current={threadId === thread.id ? 'page' : undefined}
              onClick={() => setThreadId(thread.id)}>{thread.title}</button>)}
          </div>}
          {conversation && !loading && !error && <>
            <h4>{conversation.title} · {conversationAgent}</h4>
            {conversation.turns.length === 0 && <p>This chat has no saved turns.</p>}
            {conversation.turns.map((turn) => <div className="task-team-turn" key={turn.id}>
              <p><strong>{requestSource(turn.source_kind)}</strong> · <span>{turn.operator_text}</span></p>
              <p><strong>{conversationAgent}</strong>
                {' '}· {replyStatus(turn.status)}{visibleReply(turn) && <> · <span>{visibleReply(turn)}</span></>}</p>
            </div>)}
          </>}
        </div>}
      </article>;
    })}</div>
    <p className="task-team-boundary">Assignments and saved task chats are shown here. Direct agent-to-agent messages are not recorded by this version.</p>
  </section>;
}
