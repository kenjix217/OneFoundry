import './ZohoMail.css';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError } from './api';
import { listRuns, type Run } from './workApi';
import { enableZoho, listZohoConnections, probeZoho, proposeZoho, saveZohoCredentials, saveZohoEndpoint, saveZohoSetup, startZohoSignIn, type ZohoConnection } from './zohoMailApi';

function failure(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Could not confirm Zoho Mail state. Refresh before retrying.';
}
const statusLabels = { needs_credentials: 'Credentials needed', unverified: 'Check connection', ready: 'Ready', failed: 'Connection needs attention' };

export function ZohoMail({ projectId, onAuthorize = (path) => window.location.assign(path) }: {
  projectId: string; onAuthorize?: (path: string) => void;
}) {
  const activeProject = useRef(projectId);
  activeProject.current = projectId;
  const callbackResult = window.location.hash.split('?')[0] === `#project/${projectId}/connections`
    ? new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('zoho') : null;
  const [rows, setRows] = useState<ZohoConnection[] | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [label, setLabel] = useState('Zoho Mail');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [credentialId, setCredentialId] = useState('');
  const [serverUrl, setServerUrl] = useState('');
  const [token, setToken] = useState('');
  const [mailboxId, setMailboxId] = useState('');
  const [connectionId, setConnectionId] = useState('');
  const [runId, setRunId] = useState('');
  const [operation, setOperation] = useState<'draft' | 'send'>('draft');
  const [recipient, setRecipient] = useState('');
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    setRows(null); setRuns([]); setError(null); setNotice(null);
    setBusy(false);
    setCredentialId(''); setServerUrl(''); setToken(''); setMailboxId(''); setConnectionId(''); setRunId('');
    setRecipient(''); setSubject(''); setBody('');
    void listZohoConnections(projectId, controller.signal).then((saved) => {
      if (!controller.signal.aborted) setRows(saved);
    }).catch((problem: unknown) => { if (!controller.signal.aborted) setError(failure(problem)); });
    return () => controller.abort();
  }, [projectId]);

  const ready = rows?.filter((row) => row.status === 'ready' && row.enabled && row.credential_present) ?? [];
  const hasReady = ready.length > 0;
  useEffect(() => {
    if (!hasReady) return;
    const controller = new AbortController();
    void listRuns(projectId, undefined, controller.signal).then((saved) => {
      if (!controller.signal.aborted) setRuns(saved.filter((run) => run.mode === 'manual_planning' && run.status === 'running'));
    }).catch((problem: unknown) => { if (!controller.signal.aborted) setError(failure(problem)); });
    return () => controller.abort();
  }, [projectId, hasReady]);

  async function perform(action: () => Promise<void>) {
    const originProject = projectId;
    setBusy(true); setError(null); setNotice(null);
    try { await action(); }
    catch (problem) { if (activeProject.current === originProject) setError(failure(problem)); }
    finally { if (activeProject.current === originProject) setBusy(false); }
  }
  async function update(action: () => Promise<ZohoConnection>, message: string) {
    await action();
    if (activeProject.current !== projectId) return;
    const saved = await listZohoConnections(projectId);
    if (activeProject.current !== projectId) return;
    setRows(saved);
    setNotice(message);
  }
  function setup(event: FormEvent) {
    event.preventDefault();
    void perform(() => update(() => saveZohoSetup(projectId, label), 'Zoho setup saved. Add your existing server in Advanced connection settings, then choose Connect to Zoho.'));
  }
  function credentials(event: FormEvent) {
    event.preventDefault();
    const selected = rows?.find((row) => row.id === credentialId);
    if (!selected) return;
    void perform(async () => {
      try {
        await update(() => token ? saveZohoCredentials(projectId, selected, serverUrl, token, mailboxId)
          : saveZohoEndpoint(projectId, selected, serverUrl, mailboxId),
        token ? 'Credentials saved securely. Check the connection before turning access on.' : 'Zoho server saved. Choose Connect to Zoho to sign in.');
        if (activeProject.current === projectId) setCredentialId('');
      } finally { if (activeProject.current === projectId) { setServerUrl(''); setToken(''); } }
    });
  }
  function signin(row: ZohoConnection) {
    void perform(async () => {
      const path = await startZohoSignIn(projectId, row);
      if (activeProject.current === projectId) onAuthorize(path);
    });
  }
  function propose(event: FormEvent) {
    event.preventDefault();
    void perform(async () => {
      await proposeZoho(projectId, { connection_id: connectionId, run_id: runId, operation, to_address: recipient, subject, body });
      if (activeProject.current !== projectId) return;
      setNotice('Your exact mail proposal is waiting in Approvals. Zoho has not been changed.');
      setRecipient(''); setSubject(''); setBody('');
    });
  }

  return <section className="skill-library zoho-mail" aria-labelledby="zoho-mail-title">
    <h2 id="zoho-mail-title">Zoho Mail</h2>
    <p>Connect your existing Zoho mail server. Read and search mail; save drafts and send only after approval.</p>
    <p className="field-hint">Use Connect to Zoho to sign in with your existing account. OneFoundry handles the token securely. Access stays off until you enable it.</p>
    {error && <p role="alert">{error}</p>}
    {callbackResult === 'failed' && <p role="alert">Zoho sign-in was not completed or expired. Choose Connect to Zoho to try again.</p>}
    {callbackResult === 'ready' && rows?.some(row => row.status === 'ready') && <p role="status">Signed in to Zoho. Review your mailbox and connection status below.</p>}
    {callbackResult === 'check' && <p role="status">Zoho authorization saved. Check the connection details below before enabling access.</p>}
    {notice && <p role="status">{notice}</p>}
    {rows === null ? <p>Loading Zoho connections…</p> : <>
      {!rows.length && <p>No Zoho connection configured.</p>}
      <div className="integration-list">{rows.map((row) => <article key={row.id} className="zoho-connection">
        <h3>{row.label}</h3><p>{statusLabels[row.status]} · Access {row.enabled ? 'on' : 'off'}</p>
        {row.from_address && <p>{row.from_address}</p>}
        {row.last_error_code && <p role="alert">{row.last_error_code.replaceAll('_', ' ')}. Reconnect to Zoho and check again.</p>}
        {!row.endpoint_present && <p className="field-hint">Add your existing Zoho server in Advanced connection settings first.</p>}
        {row.oauth_pending && <p className="field-hint">Finish sign-in in Zoho. Connect again if you closed that page.</p>}
        <div className="button-row">
          <button className="button button-primary" type="button" disabled={busy || !row.endpoint_present} onClick={() => signin(row)}>{row.credential_present ? 'Reconnect to Zoho' : 'Connect to Zoho'}</button>
          <button className="button button-quiet" type="button" disabled={busy} onClick={() => { setCredentialId(row.id); setServerUrl(''); setToken(''); setMailboxId(''); }}>Advanced connection settings</button>
          <button className="button button-quiet" type="button" disabled={busy || !row.credential_present} onClick={() => void perform(() => update(() => probeZoho(projectId, row), 'Connection check saved. Review its status before turning access on.'))}>Check Zoho connection</button>
          <button className="button button-quiet" type="button" disabled={busy || (!row.enabled && (row.status !== 'ready' || !row.credential_present))} onClick={() => void perform(() => update(() => enableZoho(projectId, row), row.enabled ? 'Zoho access turned off.' : 'Zoho access turned on. Every draft and send still needs approval.'))}>{row.enabled ? 'Turn access off' : 'Turn access on'}</button>
        </div>
      </article>)}</div>
      <form onSubmit={setup} className="skill-create"><h3>Add Zoho connection</h3>
        <label className="field">Connection label<input required maxLength={80} value={label} onChange={(event) => setLabel(event.target.value)} /></label>
        <button className="button button-primary" disabled={busy} type="submit">Save Zoho setup</button>
      </form>
    </>}
    {credentialId && <form className="skill-create" onSubmit={credentials} autoComplete="off">
      <h3>Advanced connection settings</h3><p>Save your existing Zoho server here, then choose Connect to Zoho. Normal sign-in does not require copying an access token.</p>
      <label className="field">Private Zoho MCP URL<input type="password" autoComplete="off" required maxLength={2048} value={serverUrl} onChange={(event) => setServerUrl(event.target.value)} /></label>
      <label className="field">Zoho MCP OAuth access token (optional, manual setup)<input type="password" autoComplete="off" maxLength={4096} value={token} onChange={(event) => setToken(event.target.value)} /></label>
      <label className="field">Mailbox account ID (optional)<input pattern="[0-9]*" maxLength={30} value={mailboxId} onChange={(event) => setMailboxId(event.target.value)} /></label>
      <p className="field-hint">Leave the account ID blank if this authorization has one mailbox. Tokens obtained by normal sign-in renew securely when Zoho permits renewal.</p>
      <button className="button button-primary" disabled={busy} type="submit">{token ? 'Save credentials' : 'Save Zoho server'}</button>
      <button className="button button-quiet" disabled={busy} type="button" onClick={() => { setCredentialId(''); setServerUrl(''); setToken(''); setMailboxId(''); }}>Cancel</button>
    </form>}
    {hasReady && <form className="skill-create" onSubmit={propose}>
      <h3>Prepare mail for approval</h3>
      <label className="field">Zoho connection<select required value={connectionId} onChange={(event) => setConnectionId(event.target.value)}><option value="">Choose connection</option>{ready.map((row) => <option key={row.id} value={row.id}>{row.label}</option>)}</select></label>
      <label className="field">Running manual work order<select required value={runId} onChange={(event) => setRunId(event.target.value)}><option value="">Choose work order</option>{runs.map((run) => <option key={run.id} value={run.id}>{run.work_order_title}</option>)}</select></label>
      <label className="field">After approval<select value={operation} onChange={(event) => setOperation(event.target.value as 'draft' | 'send')}><option value="draft">Save in Zoho Drafts</option><option value="send">Send email</option></select></label>
      <label className="field">To<input required type="email" value={recipient} onChange={(event) => setRecipient(event.target.value)} /></label>
      <label className="field">Subject<input required maxLength={200} value={subject} onChange={(event) => setSubject(event.target.value)} /></label>
      <label className="field">Body<textarea required maxLength={12000} value={body} onChange={(event) => setBody(event.target.value)} /></label>
      <button className="button button-primary" disabled={busy} type="submit">Request mail approval</button>
    </form>}
  </section>;
}
