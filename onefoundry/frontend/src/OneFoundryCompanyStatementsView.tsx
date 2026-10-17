import { useEffect, useState, type FormEvent } from 'react';

import { ApiError } from './api';
import {
  createCompanyStatement, decideCompanyStatement, listCompanyStatements,
  type CompanyStatement,
} from './onefoundryOrganizationApi';

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Company fact outcome is uncertain. Reload before retrying.';
}

export function OneFoundryCompanyStatementsView({ projectId, onChanged }: {
  projectId: string; onChanged?: () => void;
}) {
  const [rows, setRows] = useState<CompanyStatement[]>([]);
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void listCompanyStatements(projectId).then((saved) => { if (active) setRows(saved); })
      .catch((reason: unknown) => { if (active) setError(errorText(reason)); });
    return () => { active = false; };
  }, [projectId]);

  async function refresh() {
    const saved = await listCompanyStatements(projectId);
    setRows(saved);
    onChanged?.();
    return saved;
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError(null);
    try {
      const created = await createCompanyStatement(projectId, body.trim());
      const saved = await refresh();
      if (!saved.some((item) => item.id === created.id && item.state === 'draft')) {
        throw new ApiError('Company fact was not confirmed by readback.', null);
      }
      setBody('');
    } catch (reason) { setError(errorText(reason)); }
    finally { setBusy(false); }
  }

  async function decide(row: CompanyStatement, action: 'approve' | 'retire') {
    if (busy) return;
    setBusy(true); setError(null);
    try {
      await decideCompanyStatement(projectId, row.id, action, row.version);
      const saved = await refresh();
      if (!saved.some((item) => item.id === row.id && item.state === (action === 'approve' ? 'approved' : 'retired'))) {
        throw new ApiError('Company fact decision was not confirmed.', null);
      }
    } catch (reason) {
      setError(errorText(reason));
      try { await refresh(); } catch { /* Preserve the original error. */ }
    } finally { setBusy(false); }
  }

  return <section className="onefoundry-card" aria-label="Company facts">
    <h2>Company facts</h2>
    <p>Only approved, current statements can support product claims in a draft.</p>
    {error && <p role="alert" className="onefoundry-error">{error}</p>}
    <form onSubmit={(event) => { void save(event); }}>
      <label htmlFor="company-fact-body">Product statement</label>
      <textarea id="company-fact-body" value={body} maxLength={2000} required
        onChange={(event) => setBody(event.target.value)} />
      <button className="button button-primary" type="submit" disabled={busy}>Save fact</button>
    </form>
    {rows.length === 0 && <p>No company facts saved.</p>}
    <ul>{rows.map((row) => <li key={row.id}>
      <strong>{row.body}</strong><small>Version {row.version} · {row.state} · {row.is_current ? 'current' : 'superseded'}</small>
      {row.state === 'draft' && row.is_current && <button type="button" disabled={busy}
        onClick={() => { void decide(row, 'approve'); }}>Approve fact</button>}
      {row.state === 'approved' && <button type="button" disabled={busy}
        onClick={() => { void decide(row, 'retire'); }}>Retire fact</button>}
    </li>)}</ul>
  </section>;
}
