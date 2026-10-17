import { useEffect, useState, type FormEvent } from 'react';

import { listRuns, type Run } from './workApi';
import { createSmtpAccount, enableSmtpAccount, listOutboundEmails, listSmtpAccounts,
  probeSmtpAccount, proposeOutboundEmail, type NewSmtpAccount, type OutboundEmail,
  type SmtpAccount } from './smtpApi';

const EMPTY: NewSmtpAccount = {
  label: '', host: '', port: 465, security: 'ssl', username: '', from_address: '', password: '',
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Result unknown. Refresh before trying again.';
}

export function MailOutgoing({ projectId }: { projectId: string }) {
  const [accounts, setAccounts] = useState<SmtpAccount[]>([]);
  const [emails, setEmails] = useState<OutboundEmail[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [form, setForm] = useState<NewSmtpAccount>(EMPTY);
  const [accountId, setAccountId] = useState('');
  const [runId, setRunId] = useState('');
  const [recipient, setRecipient] = useState('');
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([listSmtpAccounts(projectId, controller.signal), listOutboundEmails(projectId, controller.signal),
      listRuns(projectId, '', controller.signal)]).then(([saved, sent, work]) => {
        setAccounts(saved); setEmails(sent); setRuns(work);
      }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(message(cause)); });
    return () => controller.abort();
  }, [projectId]);

  async function act(work: () => Promise<string>) {
    setBusy(true); setNotice(null); setError(null);
    try {
      setNotice(await work());
      const [saved, sent] = await Promise.all([listSmtpAccounts(projectId), listOutboundEmails(projectId)]);
      setAccounts(saved); setEmails(sent);
    } catch (cause) { setError(message(cause)); }
    finally { setBusy(false); }
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void act(async () => {
      const saved = await createSmtpAccount(projectId, form);
      setForm(EMPTY);
      return `Saved ${saved.label}. Check the connection, then turn on sending.`;
    });
  }

  function propose(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void act(async () => {
      await proposeOutboundEmail(projectId, {
        account_id: accountId, run_id: runId, to_address: recipient.trim(), subject: subject.trim(), body: body.trim(),
      });
      setRecipient(''); setSubject(''); setBody('');
      return `Your email is waiting in Approvals. Nothing has been sent.`;
    });
  }

  const ready = accounts.filter((item) => item.enabled && item.status === 'ready');
  const manualRuns = runs.filter((item) => item.status === 'running' && item.mode === 'manual_planning');
  return <section className="models-section" aria-label="Outgoing mail">
    <h2>Outgoing mail</h2>
    <p>Connect a separate SMTP account. Agents with the Propose email capability can prepare one exact email per approval. After you approve it, OneFoundry makes one SMTP submission attempt. An accepted message may still be rejected later by the recipient’s mail system.</p>
    {notice && <p className="notice" role="status">{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {accounts.length === 0 ? <p>No outgoing account connected.</p> : <ul className="automation-rules">
      {accounts.map((item) => <li key={item.id}><p><strong>{item.label}</strong> · {item.from_address} · {item.host}:{item.port} ({item.security})</p>
        <p>{item.enabled ? 'Sending on' : item.status === 'ready' ? 'Ready, off' : item.status === 'failed' ? 'Connection failed' : 'Not checked yet'}{item.last_error_code ? ` · ${item.last_error_code}` : ''}</p>
        <button type="button" className="button button-quiet" disabled={busy} onClick={() => { void act(async () => {
          const saved = await probeSmtpAccount(projectId, item.id);
          return saved.status === 'ready' ? 'SMTP login works. Turn on sending when ready.' : 'SMTP check failed.';
        }); }}>Check connection</button>
        <button type="button" className="button button-quiet" disabled={busy || (item.status !== 'ready' && !item.enabled)}
          onClick={() => { void act(async () => {
            const saved = await enableSmtpAccount(projectId, item.id, !item.enabled, item.version);
            return saved.enabled ? 'SMTP sending is on. Each email still needs approval.' : 'SMTP sending is off.';
          }); }}>{item.enabled ? 'Turn off' : 'Turn on'}</button>
      </li>)}
    </ul>}
    <form className="panel proposal-form" onSubmit={save}>
      <h3>Add SMTP account</h3>
      <label className="field"><span className="field-label">Label</span><input required maxLength={80} value={form.label} onChange={(event) => setForm({ ...form, label: event.target.value })} /></label>
      <label className="field"><span className="field-label">SMTP server</span><input required maxLength={253} placeholder="smtp.example.com" value={form.host} onChange={(event) => setForm({ ...form, host: event.target.value })} /></label>
      <label className="field"><span className="field-label">Security</span><select value={form.security} onChange={(event) => { const security = event.target.value as 'ssl' | 'starttls'; setForm({ ...form, security, port: security === 'ssl' ? 465 : 587 }); }}><option value="ssl">TLS (usually port 465)</option><option value="starttls">STARTTLS (usually port 587)</option></select></label>
      <label className="field"><span className="field-label">Port</span><input required type="number" min="1" max="65535" value={form.port} onChange={(event) => setForm({ ...form, port: Number(event.target.value) })} /></label>
      <label className="field"><span className="field-label">Username</span><input required maxLength={320} autoComplete="username" value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} /></label>
      <label className="field"><span className="field-label">From address</span><input required type="email" maxLength={320} value={form.from_address} onChange={(event) => setForm({ ...form, from_address: event.target.value })} /></label>
      <label className="field"><span className="field-label">SMTP or app password</span><input required type="password" maxLength={512} autoComplete="new-password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} /></label>
      <p className="field-hint">Password is stored in the protected local credential directory and is never returned by the API.</p>
      <button className="button button-primary" type="submit" disabled={busy}>Save SMTP account</button>
    </form>
    <form className="panel proposal-form" onSubmit={propose}>
      <h3>Prepare an email</h3><p className="field-hint">This creates a proposal in Approvals. You must approve the exact message before OneFoundry sends it.</p>
      {manualRuns.length === 0 && <p className="field-hint">Create and start a manual work order in Work orders → Task first.</p>}
      <label className="field"><span className="field-label">Outgoing account</span><select required value={accountId} onChange={(event) => setAccountId(event.target.value)}><option value="">Choose an account</option>{ready.map((item) => <option key={item.id} value={item.id}>{item.label} · {item.from_address}</option>)}</select></label>
      <label className="field"><span className="field-label">Running manual work order</span><select required value={runId} onChange={(event) => setRunId(event.target.value)}><option value="">Choose a run</option>{manualRuns.map((item) => <option key={item.id} value={item.id}>{item.work_order_title}</option>)}</select></label>
      <label className="field"><span className="field-label">To</span><input required type="email" maxLength={320} value={recipient} onChange={(event) => setRecipient(event.target.value)} /></label>
      <label className="field"><span className="field-label">Subject</span><input required maxLength={200} value={subject} onChange={(event) => setSubject(event.target.value)} /></label>
      <label className="field"><span className="field-label">Body</span><textarea required maxLength={12000} rows={5} value={body} onChange={(event) => setBody(event.target.value)} /></label>
      <button className="button button-primary" type="submit" disabled={busy || ready.length === 0 || manualRuns.length === 0}>Request email approval</button>
    </form>
    <h3>Recent email proposals</h3>
    {emails.length === 0 ? <p>No emails proposed.</p> : <ul className="automation-rules">{emails.slice(0, 10).map((item) =>
      <li key={item.id}><strong>{item.subject}</strong> · {item.to_address}<p>{item.state === 'accepted' ? 'Accepted by SMTP server' : item.state}{item.reason_code ? ` · ${item.reason_code}` : ''}</p></li>)}</ul>}
  </section>;
}
