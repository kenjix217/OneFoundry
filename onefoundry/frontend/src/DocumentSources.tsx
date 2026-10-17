import { useEffect, useState, type FormEvent } from 'react';

import { ApiError } from './api';
import type { Connector } from './connectorApi';
import { DocumentActions } from './DocumentActions';
import { DocumentSearch } from './DocumentSearch';
import {
  createDocumentSource, dispatchDocumentWrite, listDocumentSources, listDocumentWrites, probeDocumentSource,
  removeDocumentSource, setDocumentSourceEnabled, type DocumentSource, type DocumentWrite,
} from './documentApi';

const ERRORS: Record<string, string> = {
  folder_unavailable: 'The folder was not found or is not a folder this account can read.',
  authentication_failed: 'The account was refused. Sign it in again above.',
  connection_failed: 'Could not reach the provider; it will be retried.',
  credential_unavailable: 'The account needs to sign in again.',
  cursor_expired: 'The change history expired; watching restarted from now.',
};

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : 'The result is unknown. Refresh before retrying.';
}

export function DocumentSources({ projectId, connectors }: { projectId: string; connectors: Connector[] }) {
  const [sources, setSources] = useState<DocumentSource[]>([]);
  const [writes, setWrites] = useState<DocumentWrite[]>([]);
  const [provider, setProvider] = useState<'onedrive' | 'gdrive'>('onedrive');
  const [label, setLabel] = useState('');
  const [account, setAccount] = useState('');
  const [folder, setFolder] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const usable = connectors.filter((item) => item.status === 'connected' && item.features.includes('files')
    && item.provider === (provider === 'onedrive' ? 'microsoft' : 'google'));

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([listDocumentSources(projectId, controller.signal), listDocumentWrites(projectId, controller.signal)])
      .then(([nextSources, nextWrites]) => { setSources(nextSources); setWrites(nextWrites); }).catch((failure: unknown) => {
        if (!controller.signal.aborted) setError(message(failure));
      });
    return () => controller.abort();
  }, [projectId]);

  async function act(work: () => Promise<string>) {
    setBusy(true); setError(null); setNotice(null);
    try { setNotice(await work()); setSources(await listDocumentSources(projectId)); setWrites(await listDocumentWrites(projectId)); }
    catch (failure) { setError(message(failure)); }
    finally { setBusy(false); }
  }

  function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void act(async () => {
      const created = await createDocumentSource(projectId, { label: label.trim(), provider, connector_account_id: account, folder_ref: folder.trim() });
      setLabel(''); setFolder('');
      return `Saved “${created.label}”. Check the folder before turning it on.`;
    });
  }

  return <section className="models-section" aria-label="Watched folders">
    <h2>Watched folders</h2>
    <p>Watch one OneDrive, SharePoint, or Google Drive folder. New or changed files directly inside it are saved as project files (Google Docs, Sheets, and Slides as Office files), their text is extracted locally, and each becomes an event. Work created from that event gets the file attached. Other folders are never read.</p>
    {notice && <p className="notice" role="status">{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {sources.length === 0 ? <p>No folders watched.</p> : <ul className="automation-rules">
      {sources.map((item) => <li key={item.id}>
        <p><strong>{item.label}</strong> · {item.provider === 'onedrive' ? 'OneDrive/SharePoint' : 'Google Drive'} · {item.folder_name ?? item.folder_ref}
          {' · '}{item.status === 'ready' ? (item.enabled ? 'On' : 'Ready, off') : item.status === 'failed' ? 'Check failed' : 'Not checked yet'}</p>
        {item.last_error_code && <p className="form-error">{ERRORS[item.last_error_code] ?? item.last_error_code}</p>}
        <button type="button" className="button button-quiet" disabled={busy} onClick={() => { void act(async () => {
          const saved = await probeDocumentSource(projectId, item.id);
          return saved.status === 'ready' ? `Folder “${saved.folder_name ?? ''}” found. Changes from now on will become events once it is on.` : 'The folder check failed.'; }); }}>Check folder</button>
        <button type="button" className="button button-quiet" disabled={busy || (!item.enabled && item.status !== 'ready')}
          onClick={() => { void act(async () => { await setDocumentSourceEnabled(projectId, item.id, !item.enabled, item.version);
            return item.enabled ? 'Folder turned off.' : 'Folder turned on.'; }); }}>{item.enabled ? 'Turn off' : 'Turn on'}</button>
        <button type="button" className="button button-quiet" disabled={busy}
          onClick={() => { void act(async () => { await removeDocumentSource(projectId, item.id, item.version); return 'Folder removed.'; }); }}>Remove</button>
      </li>)}</ul>}
    <DocumentSearch projectId={projectId} sources={sources} />
    <DocumentActions projectId={projectId} />
    <h3>Document writes</h3>
    <p>Agents with the Propose document write capability can propose one new text, Markdown, or CSV file for a folder whose account has write access. Approve or reject it in Approvals, then send it here. Each approved write is sent once, never overwrites a file, and is read back to confirm it.</p>
    {writes.length === 0 ? <p>No document writes proposed.</p> : <ul className="automation-rules">
      {writes.map((item) => <li key={item.id}>
        <p><strong>{item.file_name}</strong> · {item.state === 'pending' ? 'Waiting for approval' : item.state === 'approved' ? 'Approved, not sent'
          : item.state === 'done' ? `Written and verified${item.readback?.verified ? '' : ' (unverified)'}` : item.state === 'unknown'
          ? `Outcome unknown (${item.error_code ?? 'no detail'}); not retried` : item.state === 'failed' ? `Failed (${item.error_code ?? ''})`
          : item.state === 'rejected' ? 'Rejected' : 'Sending'}</p>
        {item.state === 'approved' && <button type="button" className="button button-outline" disabled={busy}
          onClick={() => { void act(async () => { const sent = await dispatchDocumentWrite(projectId, item.id);
            return sent.state === 'done' ? `“${sent.file_name}” was written and read back.` : `“${sent.file_name}” finished as ${sent.state}.`; }); }}>
          Write exact file once</button>}
      </li>)}</ul>}
    <form className="skill-create" onSubmit={add}>
      <h3>Watch a folder</h3>
      <label className="field"><span className="field-label">Drive</span>
        <select value={provider} onChange={(event) => { setProvider(event.target.value === 'gdrive' ? 'gdrive' : 'onedrive'); setAccount(''); }}>
          <option value="onedrive">OneDrive / SharePoint (Microsoft account)</option><option value="gdrive">Google Drive (Google account)</option>
        </select></label>
      {usable.length === 0 ? <p>Connect a {provider === 'onedrive' ? 'Microsoft' : 'Google'} account with file access above first.</p> : <>
        <label className="field"><span className="field-label">Account</span>
          <select required value={account} onChange={(event) => setAccount(event.target.value)}>
            <option value="">Choose an account</option>
            {usable.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select></label>
        <label className="field"><span className="field-label">Name</span>
          <input required maxLength={80} value={label} onChange={(event) => setLabel(event.target.value)} placeholder="e.g. Contracts" /></label>
        <label className="field"><span className="field-label">Folder</span>
          <input required maxLength={400} value={folder} onChange={(event) => setFolder(event.target.value)}
            placeholder={provider === 'onedrive' ? '/Contracts (path from your OneDrive root)' : 'Folder link or ID from Google Drive'} /></label>
        <button className="button button-primary" type="submit" disabled={busy || !account}>Save folder</button></>}
    </form>
  </section>;
}
