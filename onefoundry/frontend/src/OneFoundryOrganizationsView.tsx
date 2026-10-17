import { useEffect, useState, type FormEvent } from 'react';

import { ApiError, type Profile } from './api';
import { OneFoundryCompanyStatementsView } from './OneFoundryCompanyStatementsView';
import { OneFoundryDraftView } from './OneFoundryDraftView';
import {
  addClaim, addSource, createOrganization, getOrganization, listOrganizations,
  qualifyOrganization, reviewClaim, requestLinkedOrder, importResearchCandidates,
  type BusinessMode, type Detail, type Organization,
} from './onefoundryOrganizationApi';
import { runLiveText } from './workApi';

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Save result is uncertain. Reload the saved record before retrying.';
}

export function OneFoundryOrganizationsView({ projectId, mode = 'records', profiles = [], focusOrganizationId = null }: {
  projectId: string; mode?: BusinessMode; profiles?: Profile[]; focusOrganizationId?: string | null;
}) {
  const [items, setItems] = useState<Organization[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [excerpt, setExcerpt] = useState('');
  const [claimText, setClaimText] = useState('');
  const [locator, setLocator] = useState('');
  const [profileId, setProfileId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setSelectedId(focusOrganizationId); setDetail(null); setError(null);
    void listOrganizations(projectId).then((rows) => { if (active) setItems(rows); })
      .catch((reason: unknown) => { if (active) setError(errorText(reason)); });
    return () => { active = false; };
  }, [projectId, focusOrganizationId]);

  useEffect(() => {
    if (!selectedId) return;
    let active = true;
    void getOrganization(projectId, selectedId).then((saved) => { if (active) setDetail(saved); })
      .catch((reason: unknown) => { if (active) setError(errorText(reason)); });
    return () => { active = false; };
  }, [projectId, selectedId]);

  async function refresh(id: string) {
    const [saved, rows] = await Promise.all([getOrganization(projectId, id), listOrganizations(projectId)]);
    setDetail(saved); setItems(rows);
    return saved;
  }

  async function save(action: () => Promise<void>, confirm: (saved: Detail) => boolean): Promise<boolean> {
    if (!selectedId || busy) return false;
    setBusy(true); setError(null);
    try {
      await action();
      const saved = await refresh(selectedId);
      if (!confirm(saved)) throw new ApiError('Saved state was not confirmed. Reload before retrying.', null);
      return true;
    } catch (reason) {
      setError(errorText(reason));
      try { await refresh(selectedId); } catch { /* Keep the original error visible. */ }
      return false;
    } finally { setBusy(false); }
  }

  async function submitOrganization(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError(null);
    try {
      const created = await createOrganization(projectId, name.trim());
      const saved = await refresh(created.id);
      if (saved.organization.id !== created.id) throw new ApiError('Organization was not confirmed by readback.', null);
      setSelectedId(created.id); setName('');
    } catch (reason) { setError(errorText(reason)); }
    finally { setBusy(false); }
  }

  const currentSource = detail?.sources.find((source) => !source.superseded_by);
  async function requestOrder(step: 'research' | 'draft') {
    if (!detail || !profileId) { setError('Select a profile for this work order.'); return; }
    const before = detail.workflow_revision;
    const idempotencyKey = crypto.randomUUID();
    await save(async () => {
      await requestLinkedOrder(projectId, detail.organization.id, step, profileId, before, idempotencyKey);
    }, (saved) => saved.workflow_revision > before && saved.linked_order?.step_key === step);
  }

  async function startLinkedRun() {
    if (!detail?.linked_order) return;
    const before = detail.linked_order.work_order_id;
    await save(async () => { await runLiveText(projectId, before); },
      (saved) => saved.linked_order?.work_order_id === before && Boolean(saved.linked_order.run_id));
  }

  return <section className="onefoundry-organizations" aria-label="Organizations">
    <div className="section-heading"><div><p className="eyebrow">Evidence records</p><h1>Organizations</h1>
      <p>Save observed sources and review factual claims before making a qualification decision.</p></div></div>
    {error && <p role="alert" className="onefoundry-error">{error}</p>}
    <div className="onefoundry-grid">
      <section className="onefoundry-card" aria-label="Organization list">
        <h2>Districts</h2>
        <form onSubmit={(event) => { void submitOrganization(event); }}>
          <label htmlFor="district-name">District name</label>
          <input id="district-name" value={name} onChange={(event) => setName(event.target.value)} maxLength={160} required />
          <button className="button button-primary" type="submit" disabled={busy}>Add district</button>
        </form>
        {items.length === 0 && <p>No districts saved in this project.</p>}
        <ul>{items.map((item) => <li key={item.id}><button type="button" aria-current={selectedId === item.id ? 'page' : undefined}
          onClick={() => { if (selectedId === item.id) {
            void refresh(item.id).catch((reason: unknown) => setError(errorText(reason)));
          } else {
            setSelectedId(item.id); setDetail(null); setError(null);
          } }}>{item.display_name}</button></li>)}</ul>
      </section>
      {detail && <section className="onefoundry-card onefoundry-detail" aria-label="District detail">
        <h2>{detail.organization.display_name}</h2>
        <p>Qualification: <strong>{detail.organization.qualification}</strong></p>
        <p role="status">Next action: <strong>{detail.next_action.label}</strong>. {detail.next_action.reason}</p>
        {detail.blocked_reason && <p>Workflow block: <strong>{detail.blocked_reason}</strong></p>}
        {(mode === 'manual' || mode === 'event' || mode === 'inbox') && <div aria-label="Business workflow">
          <h3>Work order</h3>
          <label htmlFor="onefoundry-profile">Profile for this order</label>
          <select id="onefoundry-profile" value={profileId} onChange={(event) => setProfileId(event.target.value)}>
            <option value="">Select a project profile</option>
            {profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
          </select>
          {detail.next_action.action_key === 'research' && <button type="button" disabled={busy}
            onClick={() => { void requestOrder('research'); }}>Research district</button>}
          {detail.next_action.action_key === 'draft' && <button type="button" disabled={busy}
            onClick={() => { void requestOrder('draft'); }}>Draft outreach</button>}
          {(detail.next_action.action_key === 'start_research' || detail.next_action.action_key === 'start_draft') &&
            <button type="button" disabled={busy} onClick={() => { void startLinkedRun(); }}>
              {detail.next_action.label}
            </button>}
          {detail.linked_order && <p>Linked {detail.linked_order.step_key} order: {detail.linked_order.status}.
            {detail.linked_order.run_id && <> Run: {detail.linked_order.run_status}.</>}</p>}
          {(mode === 'event' || mode === 'inbox') && detail.intent && <p role="status">Draft continuation: {detail.intent.state}.
            {' '}Queued {new Date(detail.intent.created_at).toLocaleString()}.
            {detail.intent.error_code && <> Reason: {detail.intent.error_code}.</>}
          </p>}
          {detail.linked_order?.step_key === 'research' && detail.linked_order.run_status === 'done' &&
            <button type="button" disabled={busy} onClick={() => { void save(
              () => importResearchCandidates(projectId, detail.organization.id, detail.linked_order!.run_id!),
              (saved) => saved.claims.length > detail.claims.length,
            ); }}>Import research candidates</button>}
          {detail.next_action.action_key === 'reconcile' && <p>Review the saved run in Activity. No resend is available from this page.</p>}
          {detail.budget && <p>Budget: {detail.budget.reservations.filter((item) => item.kind === 'linked_order').length}
            /{detail.budget.linked_orders_limit} orders; {detail.budget.reservations.filter((item) => item.kind === 'model_attempt').length}
            /{detail.budget.model_attempts_limit} model attempts.
            {detail.budget.reservations.some((item) => item.state === 'unknown') && ' Usage unknown; reconcile before continuing.'}
          </p>}
          {detail.budget?.effective_context_bytes_limit !== undefined && <p>Prompt limit: {
            detail.budget.effective_context_bytes_limit === null ? 'Unlimited'
              : `${detail.budget.effective_context_bytes_limit.toLocaleString()} bytes`}</p>}
        </div>}
        <h3>Sources</h3>
        {detail.sources.length === 0 && <p>No observed source saved.</p>}
        <ul>{detail.sources.map((source) => <li key={source.id}>
          <span>{source.source_kind === 'public_excerpt' ? source.official_url : 'Verified input file'}</span>
          <small>Observed {new Date(source.observed_at).toLocaleDateString()}</small>
          {source.excerpt && <blockquote>{source.excerpt}</blockquote>}
          {source.superseded_by && <small>Superseded</small>}
        </li>)}</ul>
        <form onSubmit={(event) => {
          event.preventDefault();
          const before = detail.workflow_revision;
          void save(() => addSource(projectId, detail.organization.id, {
            official_url: url.trim(), excerpt: excerpt.trim(), observed_at: new Date().toISOString(),
            expected_workflow_revision: before,
          }), (saved) => saved.workflow_revision > before && saved.sources.some((source) => source.official_url === url.trim()))
            .then((confirmed) => { if (confirmed) { setUrl(''); setExcerpt(''); } });
        }}>
          <h3>Add observed source</h3>
          <label htmlFor="source-url">Official HTTPS URL</label><input id="source-url" type="url" required value={url} onChange={(event) => setUrl(event.target.value)} />
          <label htmlFor="source-excerpt">Observed excerpt</label><textarea id="source-excerpt" required maxLength={8192} value={excerpt} onChange={(event) => setExcerpt(event.target.value)} />
          <button className="button button-primary" type="submit" disabled={busy}>Save source</button>
        </form>
        {currentSource && <form onSubmit={(event) => {
          event.preventDefault();
          const source = currentSource;
          void save(() => addClaim(projectId, detail.organization.id, {
            source_id: source.id, claim_text: claimText.trim(), locator: locator.trim(), expected_source_revision: source.revision,
          }), (saved) => saved.claims.some((claim) => claim.source_id === source.id && claim.claim_text === claimText.trim()))
            .then((confirmed) => { if (confirmed) { setClaimText(''); setLocator(''); } });
        }}>
          <h3>Propose claim from source</h3>
          <label htmlFor="claim-text">Exact claim text from excerpt</label><textarea id="claim-text" required maxLength={2000} value={claimText} onChange={(event) => setClaimText(event.target.value)} />
          <label htmlFor="claim-locator">Locator in source</label><input id="claim-locator" required maxLength={500} value={locator} onChange={(event) => setLocator(event.target.value)} />
          <button className="button button-primary" type="submit" disabled={busy}>Propose claim</button>
        </form>}
        <h3>Claims</h3>
        {detail.claims.length === 0 && <p>No claims proposed.</p>}
        <ul>{detail.claims.map((claim) => <li key={claim.id}>
          <strong>{claim.claim_text}</strong><small>{claim.review_state} · {claim.fresh ? 'current' : 'stale'} · {claim.verification_state}</small>
          {claim.review_state === 'candidate' && claim.fresh && <div className="onefoundry-actions">
            <button type="button" disabled={busy} onClick={() => { void save(() => reviewClaim(projectId, claim.id, 'approved', claim.revision),
              (saved) => saved.claims.some((item) => item.id === claim.id && item.review_state === 'approved')); }}>Approve claim</button>
            <button type="button" disabled={busy} onClick={() => { void save(() => reviewClaim(projectId, claim.id, 'rejected', claim.revision),
              (saved) => saved.claims.some((item) => item.id === claim.id && item.review_state === 'rejected')); }}>Reject claim</button>
          </div>}
        </li>)}</ul>
        {detail.organization.qualification === 'unreviewed' && detail.claims.some((claim) => claim.review_state === 'approved' && claim.fresh) &&
          <div className="onefoundry-actions">
            <button type="button" disabled={busy} onClick={() => { void save(() => qualifyOrganization(projectId, detail.organization.id, 'qualified', detail.organization.revision),
              (saved) => saved.organization.qualification === 'qualified'); }}>Qualify district</button>
            <button type="button" disabled={busy} onClick={() => { void save(() => qualifyOrganization(projectId, detail.organization.id, 'declined', detail.organization.revision),
              (saved) => saved.organization.qualification === 'declined'); }}>Decline district</button>
          </div>}
        {(mode === 'manual' || mode === 'event' || mode === 'inbox') && <OneFoundryDraftView projectId={projectId} detail={detail} onSaved={setDetail} />}
      </section>}
    </div>
    {(mode === 'manual' || mode === 'event' || mode === 'inbox') && <OneFoundryCompanyStatementsView projectId={projectId}
      onChanged={() => { if (selectedId) void refresh(selectedId); }} />}
  </section>;
}
