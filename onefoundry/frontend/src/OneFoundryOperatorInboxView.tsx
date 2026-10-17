import { useEffect, useState } from 'react';

import { ApiError } from './api';
import { getOrganization } from './onefoundryOrganizationApi';
import { getInbox, getOutcomes, type Inbox, type InboxItem, type Outcomes } from './onefoundryInboxApi';

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Saved state could not be confirmed. Refresh before acting.';
}

export function OneFoundryOperatorInboxView({ projectId, onOpenOrganization, onNavigate }: {
  projectId: string; onOpenOrganization: (id: string) => void;
  onNavigate: (view: 'team' | 'work' | 'approvals' | 'activity') => void;
}) {
  const [inbox, setInbox] = useState<Inbox | null>(null);
  const [outcomes, setOutcomes] = useState<Outcomes | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    setInbox(null); setOutcomes(null); setError(null); setStale(null);
    void Promise.all([getInbox(projectId), getOutcomes(projectId)]).then(([actions, counts]) => {
      if (active) { setInbox(actions); setOutcomes(counts); }
    }).catch((reason: unknown) => { if (active) setError(errorText(reason)); });
    return () => { active = false; };
  }, [projectId, reload]);

  async function open(item: InboxItem) {
    if (busy) return;
    setBusy(true); setStale(null);
    try {
      const latest = await getInbox(projectId);
      const currentItem = [...latest.actionable, ...latest.waiting, ...latest.blocked, ...latest.unknown]
        .find((entry) => entry.subject_ref.id === item.subject_ref.id);
      if (!currentItem || currentItem.action.action_key !== item.action.action_key ||
        currentItem.action.workflow_revision !== item.action.workflow_revision ||
        currentItem.action.source_revision !== item.action.source_revision ||
        currentItem.work_order_status !== item.work_order_status ||
        currentItem.approval_pending !== item.approval_pending) {
        setStale(`${item.subject_name} changed. Refresh the inbox before acting.`);
        return;
      }
      const current = await getOrganization(projectId, item.subject_ref.id);
      if (current.workflow_revision !== item.action.workflow_revision ||
        current.next_action.source_revision !== item.action.source_revision ||
        (item.action.action_key !== 'approval' && current.next_action.action_key !== item.action.action_key)) {
        setStale(`${item.subject_name} changed. Refresh the inbox before acting.`);
        return;
      }
      if (item.action.action_key === 'approval') onNavigate('approvals');
      else onOpenOrganization(item.subject_ref.id);
    } catch (reason) { setError(errorText(reason)); }
    finally { setBusy(false); }
  }

  function section(title: string, items: InboxItem[]) {
    return <section className="onefoundry-inbox-group" aria-label={title} key={title}>
      <h2>{title} <span>{items.length}</span></h2>
      {items.length === 0 ? <p>No districts in this group.</p> : <ul>{items.map((item) => <li key={item.subject_ref.id}>
        <div><strong>{item.subject_name}</strong><p>{item.action.label}. {item.reason}</p>
          <p>Evidence: {item.evidence_state}. Work order: {item.work_order_status ?? 'none'}.</p>
          {item.blocked_reason && <p>Block: {item.blocked_reason}</p>}
          {item.due_at && <p>Due: {new Date(item.due_at).toLocaleString()}</p>}
        </div>
        <button type="button" className="button button-outline" disabled={busy} onClick={() => { void open(item); }}>
          {item.action.action_key === 'approval' ? 'Open approvals' : 'Open district'}
        </button>
      </li>)}</ul>}
    </section>;
  }

  return <section className="onefoundry-operator-inbox" aria-label="OneFoundry inbox">
    <div className="section-heading"><div><p className="eyebrow">OneFoundry</p><h1>Operator inbox</h1>
      <p>Current district actions and saved outcomes for this project.</p></div>
      <button type="button" className="button button-outline" onClick={() => setReload((value) => value + 1)}>Refresh inbox</button></div>
    <nav aria-label="Related workspace sections" className="onefoundry-inbox-links">
      {(['team', 'work', 'approvals', 'activity'] as const).map((view) => <button type="button" key={view} onClick={() => onNavigate(view)}>
        {view === 'work' ? 'Work orders' : view[0].toUpperCase() + view.slice(1)}</button>)}
    </nav>
    {error && <p role="alert">{error} <button type="button" onClick={() => setReload((value) => value + 1)}>Retry</button></p>}
    {stale && <p role="alert">{stale}</p>}
    {!error && (!inbox || !outcomes) && <p role="status">Loading saved inbox and outcomes…</p>}
    {inbox && outcomes && <>
      <section className="onefoundry-outcomes" aria-label="Saved outcomes">
        <h2>Saved outcomes</h2><dl>
          <div><dt>Reviewed research</dt><dd>{outcomes.reviewed_research}</dd></div>
          <div><dt>Qualified districts</dt><dd>{outcomes.qualified_districts}</dd></div>
          <div><dt>Saved draft revisions</dt><dd>{outcomes.saved_drafts}</dd></div>
          <div><dt>Districts with drafts</dt><dd>{outcomes.districts_with_drafts}</dd></div>
          <div><dt>Sent</dt><dd>{outcomes.sent}</dd></div>
          <div><dt>Observed tokens</dt><dd>{outcomes.observed_tokens ?? 'Unavailable'}</dd></div>
          <div><dt>Observed cost (USD)</dt><dd>{outcomes.observed_cost_usd ?? 'Unavailable'}</dd></div>
        </dl><p>{outcomes.sent_basis}</p></section>
      {section('Actionable', inbox.actionable)}
      {section('Waiting', inbox.waiting)}
      {section('Blocked', inbox.blocked)}
      {section('Unknown', inbox.unknown)}
    </>}
  </section>;
}
