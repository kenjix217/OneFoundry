import { useEffect, useState, type FormEvent } from 'react';

import { ApiError } from './api';
import {
  getOrganization, listCompanyStatements, saveOneFoundryDraft,
  type CompanyStatement, type Detail, type DraftSegment,
} from './onefoundryOrganizationApi';

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Draft outcome is uncertain. Reload the district before retrying.';
}

export function OneFoundryDraftView({ projectId, detail, onSaved }: {
  projectId: string; detail: Detail; onSaved: (saved: Detail) => void;
}) {
  const [statements, setStatements] = useState<CompanyStatement[]>([]);
  const [kind, setKind] = useState<DraftSegment['kind']>('plain');
  const [text, setText] = useState('');
  const [reference, setReference] = useState('');
  const [segments, setSegments] = useState<DraftSegment[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const packet = detail.context_packet;
  const orgId = detail.organization.id;

  useEffect(() => {
    let active = true;
    void listCompanyStatements(projectId).then((saved) => { if (active) setStatements(saved); })
      .catch((reason: unknown) => { if (active) setError(errorText(reason)); });
    return () => { active = false; };
  }, [projectId, packet?.id]);

  if ((!packet || detail.linked_order?.step_key !== 'draft') && !(detail.drafts?.length)) return null;

  const currentClaims = detail.claims.filter((claim) => claim.review_state === 'approved' && claim.fresh &&
    packet?.claim_ids.includes(claim.id));
  const currentStatements = statements.filter((item) => item.state === 'approved' &&
    packet?.statement_versions.some((ref) => ref.id === item.id && ref.version === item.version));

  function addSegment(event: FormEvent) {
    event.preventDefault();
    if (!text.trim()) return;
    let item: DraftSegment;
    if (kind === 'district_fact') {
      if (!currentClaims.some((claim) => claim.id === reference)) return;
      item = { kind, text: text.trim(), claim_id: reference };
    } else if (kind === 'company_fact') {
      const statement = currentStatements.find((row) => row.id === reference);
      if (!statement) return;
      item = { kind, text: text.trim(), statement_id: statement.id, statement_version: statement.version };
    } else item = { kind, text: text.trim() };
    setSegments((before) => [...before, item]); setText(''); setReference('');
  }

  async function save() {
    if (!packet || !segments.length || busy) return;
    setBusy(true); setError(null);
    try {
      const expected = detail.drafts?.[0]?.revision ?? 0;
      const created = await saveOneFoundryDraft(projectId, orgId, packet.id, segments, expected);
      const saved = await getOrganization(projectId, orgId);
      if (!saved.drafts?.some((draft) => draft.id === created.id && draft.revision === expected + 1)) {
        throw new ApiError('Draft was not confirmed by owner readback.', null);
      }
      onSaved(saved); setSegments([]);
    } catch (reason) {
      setError(errorText(reason));
      try { onSaved(await getOrganization(projectId, orgId)); } catch { /* Keep the original error. */ }
    } finally { setBusy(false); }
  }

  return <section className="onefoundry-card" aria-label="Outreach draft">
    <h2>Outreach draft</h2>
    <p>Draft only. A reference shows the saved source; review whether each sentence actually matches it.</p>
    {error && <p role="alert" className="onefoundry-error">{error}</p>}
    {packet && <small>Context saved for this draft.</small>}
    {detail.drafts?.[0] && <div aria-label="Saved draft">
      <h3>Saved revision {detail.drafts[0].revision}</h3>
      {detail.drafts[0].segments.map((segment, index) => <p key={index}>
        {segment.text} <small>{segment.kind === 'district_fact' ? 'District information' :
          segment.kind === 'company_fact' ? 'Company information' : 'Plain text'}</small>
      </p>)}
    </div>}
    {packet && detail.linked_order?.step_key === 'draft' && <>
      <form onSubmit={addSegment}>
        <h3>Add draft sentence</h3>
        <label htmlFor="draft-kind">Sentence type</label>
        <select id="draft-kind" value={kind} onChange={(event) => {
          setKind(event.target.value as DraftSegment['kind']); setReference('');
        }}>
          <option value="plain">Plain text</option>
          <option value="district_fact">District fact</option>
          <option value="company_fact">Company fact</option>
        </select>
        <label htmlFor="draft-sentence">Sentence</label>
        <textarea id="draft-sentence" required maxLength={2000} value={text}
          onChange={(event) => setText(event.target.value)} />
        {kind !== 'plain' && <>
          <label htmlFor="draft-reference">Supporting reference</label>
          <select id="draft-reference" required value={reference} onChange={(event) => setReference(event.target.value)}>
            <option value="">Select a reviewed fact</option>
            {(kind === 'district_fact' ? currentClaims.map((claim) => ({ id: claim.id, label: claim.claim_text })) :
              currentStatements.map((statement) => ({ id: statement.id, label: `${statement.body} (v${statement.version})` })))
              .map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </>}
        <button type="submit" disabled={busy}>Add sentence</button>
      </form>
      {segments.length > 0 && <div aria-label="Draft proposal">
        <h3>Proposal</h3>
        <ol>{segments.map((segment, index) => <li key={index}>{segment.text} <small>{segment.kind}</small>
          <button type="button" onClick={() => setSegments((before) => before.filter((_, i) => i !== index))}>Remove</button>
        </li>)}</ol>
        <button className="button button-primary" type="button" disabled={busy}
          onClick={() => { void save(); }}>Save cited draft</button>
      </div>}
    </>}
  </section>;
}
