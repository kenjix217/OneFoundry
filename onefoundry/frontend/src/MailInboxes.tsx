import { useEffect, useState, type FormEvent } from 'react';

import { ApiError } from './api';
import type { Connector } from './connectorApi';
import {
  createApiMailAccount, createMailAccount, listMailAccounts, probeMailAccount, removeMailAccount, setMailEnabled,
  type MailAccount, type NewMailAccount,
} from './mailApi';

const ERRORS: Record<string, string> = {
  authentication_failed: 'The server rejected the username or password. Use an app-specific password if two-factor sign-in is on.',
  connection_failed: 'Could not reach the server. Check the server name and port.',
  tls_failed: 'The server certificate could not be verified.',
  folder_unavailable: 'The folder does not exist or cannot be opened.',
  uidvalidity_missing: 'The server did not identify the folder; it may not support standard IMAP.',
  fetch_failed: 'Reading new mail failed; it will be retried on the next check.',
  search_failed: 'Searching for new mail failed; it will be retried on the next check.',
  credential_unavailable: 'The saved password is missing. Remove and add the mailbox again.',
};

const EMPTY: NewMailAccount = { label: '', host: '', port: 993, username: '', folder: 'INBOX', password: '' };

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : 'The result is unknown. Refresh before retrying.';
}

function stamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'never';
}

export function MailInboxes({ projectId, connectors = [] }: { projectId: string; connectors?: Connector[] }) {
  const [kind, setKind] = useState<'imap' | 'gmail' | 'graph'>('imap');
  const [apiLabel, setApiLabel] = useState('');
  const [connectorId, setConnectorId] = useState('');
  const usable = connectors.filter((item) => item.status === 'connected' && item.features.includes('mail')
    && item.provider === (kind === 'gmail' ? 'google' : 'microsoft'));
  const [accounts, setAccounts] = useState<MailAccount[]>([]);
  const [form, setForm] = useState<NewMailAccount>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void listMailAccounts(projectId, controller.signal).then(setAccounts).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(message(failure));
    });
    return () => controller.abort();
  }, [projectId]);

  async function act(work: () => Promise<string>) {
    setBusy(true); setError(null); setNotice(null);
    try { setNotice(await work()); setAccounts(await listMailAccounts(projectId)); }
    catch (failure) { setError(message(failure)); }
    finally { setBusy(false); }
  }

  function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void act(async () => {
      const created = await createMailAccount(projectId, { ...form, label: form.label.trim(), host: form.host.trim(), username: form.username.trim() });
      setForm(EMPTY);
      return `Saved “${created.label}”. Check the connection before turning it on.`;
    });
  }

  return <section className="models-section" aria-label="Mail inboxes">
    <h2>Mail inboxes</h2>
    <p>Connect a mailbox directly over IMAP so new mail becomes an event. This inbox connection only reads: it opens the folder read-only and never marks mail as read. Sending uses a separate outgoing SMTP account below. Your mail app (for example Outlook) keeps working as before.</p>
    {notice && <p className="notice" role="status">{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {accounts.length === 0 ? <p>No mailboxes connected.</p> : <ul className="automation-rules">
      {accounts.map((item) => <li key={item.id}>
        <p><strong>{item.label}</strong> · {item.protocol === 'imap' ? `${item.username} at ${item.host}:${item.port} · ${item.folder}`
          : item.protocol === 'gmail' ? 'Gmail inbox via Google account' : 'Inbox via Microsoft account'}</p>
        <p>{item.status === 'ready' ? (item.enabled ? 'On' : 'Ready, off') : item.status === 'failed' ? 'Connection failed' : 'Not checked yet'}
          {' · '}last checked {stamp(item.last_poll_at ?? item.last_probe_at)}</p>
        {item.last_error_code && <p className="form-error">{ERRORS[item.last_error_code] ?? item.last_error_code}</p>}
        <button type="button" className="button button-quiet" disabled={busy}
          onClick={() => { void act(async () => { const saved = await probeMailAccount(projectId, item.id);
            return saved.status === 'ready' ? 'Connection works. New mail from now on will become events once the mailbox is on.' : 'The connection check failed.'; }); }}>
          Check connection</button>
        <button type="button" className="button button-quiet" disabled={busy || (!item.enabled && item.status !== 'ready')}
          onClick={() => { void act(async () => { await setMailEnabled(projectId, item.id, !item.enabled, item.version);
            return item.enabled ? 'Mailbox turned off.' : 'Mailbox turned on.'; }); }}>
          {item.enabled ? 'Turn off' : 'Turn on'}</button>
        <button type="button" className="button button-quiet" disabled={busy}
          onClick={() => { void act(async () => { await removeMailAccount(projectId, item.id, item.version); return 'Mailbox removed.'; }); }}>
          Remove</button>
      </li>)}</ul>}
    <label className="field"><span className="field-label">Connection type</span>
      <select value={kind} onChange={(event) => { setKind(event.target.value as typeof kind); setConnectorId(''); }}>
        <option value="imap">IMAP (Zoho, Fastmail, iCloud, most hosts)</option>
        <option value="gmail">Gmail (connected Google account)</option>
        <option value="graph">Outlook.com / Microsoft 365 (connected Microsoft account)</option>
      </select></label>
    {kind !== 'imap' ? <form className="skill-create" onSubmit={(event) => { event.preventDefault();
      void act(async () => { const created = await createApiMailAccount(projectId, apiLabel.trim(), kind, connectorId); setApiLabel('');
        return `Saved “${created.label}”. Check the connection before turning it on.`; }); }}>
      <h3>Connect a mailbox through an account</h3>
      {usable.length === 0 ? <p>Connect a {kind === 'gmail' ? 'Google' : 'Microsoft'} account with mail access above first.</p> : <>
        <label className="field"><span className="field-label">Name</span>
          <input required maxLength={80} value={apiLabel} onChange={(event) => setApiLabel(event.target.value)} /></label>
        <label className="field"><span className="field-label">Account</span>
          <select required value={connectorId} onChange={(event) => setConnectorId(event.target.value)}>
            <option value="">Choose an account</option>
            {usable.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select></label>
        <button className="button button-primary" type="submit" disabled={busy || !connectorId}>Save mailbox</button></>}
    </form> : <form className="skill-create" onSubmit={add}>
      <h3>Connect a mailbox</h3>
      <p className="field-hint">Zoho Mail: server imap.zoho.com (or your region, such as imap.zoho.eu), port 993, IMAP access enabled in Zoho Mail settings, and an app-specific password if two-factor sign-in is on.</p>
      <label className="field"><span className="field-label">Name</span>
        <input required maxLength={80} value={form.label} onChange={(event) => setForm({ ...form, label: event.target.value })} placeholder="e.g. Sales inbox" /></label>
      <label className="field"><span className="field-label">IMAP server</span>
        <input required maxLength={253} value={form.host} onChange={(event) => setForm({ ...form, host: event.target.value })} placeholder="imap.zoho.com" /></label>
      <label className="field"><span className="field-label">Port</span>
        <input type="number" required min={1} max={65535} value={form.port} onChange={(event) => setForm({ ...form, port: Number(event.target.value) })} /></label>
      <label className="field"><span className="field-label">Username</span>
        <input required maxLength={320} autoComplete="off" value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} /></label>
      <label className="field"><span className="field-label">App password</span>
        <input required type="password" maxLength={512} autoComplete="new-password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} />
        <span className="field-hint">Saved only in the local protected credential store; never shown again.</span></label>
      <label className="field"><span className="field-label">Folder</span>
        <input required maxLength={200} value={form.folder} onChange={(event) => setForm({ ...form, folder: event.target.value })} /></label>
      <button className="button button-primary" type="submit" disabled={busy}>Save mailbox</button>
    </form>}
  </section>;
}
