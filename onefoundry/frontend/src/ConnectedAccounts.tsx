import { useEffect, useState, type FormEvent } from 'react';

import { ApiError } from './api';
import {
  createConnector, listConnectors, pollSignIn, removeConnector, startSignIn, signOutEverywhere,
  type Connector, type ConnectorFeature, type NewConnector, type SignIn,
} from './connectorApi';

const ERRORS: Record<string, string> = {
  sign_in_declined: 'Sign-in was declined.',
  sign_in_expired: 'The sign-in code expired. Start again.',
  sign_in_failed: 'Sign-in failed. Check the app registration and try again.',
  device_start_failed: 'Microsoft did not accept the app registration. Check the client ID and that public client flows are allowed.',
  code_exchange_failed: 'Google did not accept the sign-in. Check the client ID, secret, and redirect.',
  no_refresh_token: 'Google did not return a long-lived sign-in. Remove OneFoundry from your Google account permissions and sign in again.',
  refresh_rejected: 'The saved sign-in was revoked or expired. Sign in again.',
  signout_pending: 'A sign-out request was submitted. Its outcome is unconfirmed; check your Microsoft account sessions.',
  signout_outcome_unknown: 'The sign-out outcome is unconfirmed. The request has not been retried.',
  signout_rejected: 'Microsoft rejected signing out everywhere. Check the granted permission before reconnecting.',
  signed_out_everywhere: 'Microsoft accepted signing out all apps and browsers. Sign in again to reconnect OneFoundry.',
};

const EMPTY: NewConnector = { provider: 'microsoft', label: '', client_id: '', tenant: 'common', client_secret: '', features: ['mail'] };

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : 'The result is unknown. Refresh before retrying.';
}

export function ConnectedAccounts({ projectId, onChange }: { projectId: string; onChange?: (accounts: Connector[]) => void }) {
  const [accounts, setAccounts] = useState<Connector[]>([]);
  const [form, setForm] = useState<NewConnector>(EMPTY);
  const [pending, setPending] = useState<{ id: string; signIn: SignIn } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmSignout, setConfirmSignout] = useState<string | null>(null);

  async function reload() {
    const items = await listConnectors(projectId);
    setAccounts(items); onChange?.(items);
  }

  useEffect(() => {
    const controller = new AbortController();
    void listConnectors(projectId, controller.signal).then((items) => { setAccounts(items); onChange?.(items); })
      .catch((failure: unknown) => { if (!controller.signal.aborted) setError(message(failure)); });
    return () => controller.abort();
  }, [projectId, onChange]);

  async function act(work: () => Promise<string | null>) {
    setBusy(true); setError(null); setNotice(null);
    try { setNotice(await work()); await reload(); }
    catch (failure) { setError(message(failure)); }
    finally { setBusy(false); }
  }

  function toggle(feature: ConnectorFeature) {
    const has = form.features.includes(feature);
    const next = has ? form.features.filter((item) => item !== feature && !(feature === 'files' && item === 'files_write'))
      : [...form.features, feature, ...(feature === 'files_write' && !form.features.includes('files') ? ['files' as const] : [])];
    setForm({ ...form, features: next });
  }

  function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void act(async () => {
      const fields: NewConnector = { ...form, label: form.label.trim(), client_id: form.client_id.trim(),
        ...(form.provider === 'google' ? {} : { client_secret: undefined }) };
      const created = await createConnector(projectId, fields);
      setForm(EMPTY);
      return `Saved “${created.label}”. Choose Sign in to connect it.`;
    });
  }

  return <section className="models-section" aria-label="Connected accounts">
    <h2>Microsoft and Google accounts</h2>
    <p>Connect a Microsoft 365 or Google account you registered yourself, then use it for mail and documents. One sign-in per account; OneFoundry asks only for the access you tick. Tokens stay in the local protected credential store.</p>
    {notice && <p className="notice" role="status">{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {accounts.length === 0 ? <p>No accounts connected.</p> : <ul className="automation-rules">
      {accounts.map((item) => <li key={item.id}>
        <p><strong>{item.label}</strong> · {item.provider === 'microsoft' ? 'Microsoft' : 'Google'} · {item.features.join(', ')} · {item.status === 'connected' ? 'Connected' : item.status === 'failed' ? 'Needs sign-in' : 'Not signed in'}</p>
        {item.last_error_code && <p className="form-error">{ERRORS[item.last_error_code] ?? item.last_error_code}</p>}
        {pending?.id === item.id && pending.signIn.kind === 'device' && <p className="notice" role="status">
          Open <a href={pending.signIn.verification_uri} target="_blank" rel="noreferrer">{pending.signIn.verification_uri}</a> and enter <strong>{pending.signIn.user_code}</strong>, then choose Check sign-in.</p>}
        <button type="button" className="button button-quiet" disabled={busy}
          onClick={() => { void act(async () => {
            const signIn = await startSignIn(projectId, item.id);
            if (signIn.kind === 'redirect') { window.location.assign(signIn.authorize_url); return 'Opening Google sign-in…'; }
            setPending({ id: item.id, signIn });
            return null;
          }); }}>Sign in</button>
        {pending?.id === item.id && <button type="button" className="button button-quiet" disabled={busy}
          onClick={() => { void act(async () => { const saved = await pollSignIn(projectId, item.id);
            if (saved.status === 'connected') { setPending(null); return 'Account connected.'; }
            if (saved.status === 'failed') { setPending(null); return null; }
            return 'Still waiting for you to finish signing in.'; }); }}>Check sign-in</button>}
        <button type="button" className="button button-quiet" disabled={busy}
          onClick={() => { void act(async () => { await removeConnector(projectId, item.id, item.version);
            return item.provider === 'microsoft' ? 'Local account and credential removed. Microsoft consent is managed separately.' : 'Account removed.'; }); }}>Remove</button>
        {item.provider === 'microsoft' && <p className="field-hint">Removal deletes OneFoundry’s local credential.
          {' '}<a href="https://myapplications.microsoft.com/" target="_blank" rel="noreferrer">Manage Microsoft app consent</a>.</p>}
        {item.provider === 'microsoft' && item.features.includes('sign_out_everywhere') && item.status === 'connected' &&
          <button type="button" className="button button-quiet" disabled={busy} onClick={() => setConfirmSignout(item.id)}>Sign out everywhere</button>}
        {confirmSignout === item.id && <section aria-label="Confirm sign out everywhere">
          <p>This signs this Microsoft work or school account out of every app and browser, including apps outside OneFoundry. You will need to sign in again.</p>
          <button type="button" className="button button-outline" disabled={busy} onClick={() => setConfirmSignout(null)}>Cancel</button>
          <button type="button" className="button button-primary" disabled={busy} onClick={() => { const requestId = crypto.randomUUID();
            setConfirmSignout(null); void act(() => signOutEverywhere(projectId, item, requestId)); }}>Confirm sign out of all apps and browsers</button>
        </section>}
      </li>)}</ul>}
    <form className="skill-create" onSubmit={add}>
      <h3>Add an account</h3>
      <label className="field"><span className="field-label">Provider</span>
        <select value={form.provider} onChange={(event) => setForm({ ...form, provider: event.target.value === 'google' ? 'google' : 'microsoft',
          features: form.features.filter((feature) => feature !== 'sign_out_everywhere') })}>
          <option value="microsoft">Microsoft 365 / Outlook.com</option><option value="google">Google Workspace / Gmail</option>
        </select></label>
      <p className="field-hint">{form.provider === 'microsoft'
        ? 'Register an app in Microsoft Entra, allow public client flows, and copy its Application (client) ID. Use tenant "common" for any account, or your tenant ID.'
        : 'Create a Google Cloud OAuth client of type Desktop app and copy its client ID and secret. Publish the consent screen (or use an internal Workspace app): clients left in Testing mode sign you out after seven days.'}</p>
      <label className="field"><span className="field-label">Name</span>
        <input required maxLength={80} value={form.label} onChange={(event) => setForm({ ...form, label: event.target.value })} placeholder="e.g. Work Microsoft 365" /></label>
      <label className="field"><span className="field-label">Client ID</span>
        <input required maxLength={200} value={form.client_id} onChange={(event) => setForm({ ...form, client_id: event.target.value })} /></label>
      {form.provider === 'microsoft' ? <label className="field"><span className="field-label">Tenant</span>
        <input required maxLength={100} value={form.tenant} onChange={(event) => setForm({ ...form, tenant: event.target.value })} /></label>
        : <label className="field"><span className="field-label">Client secret</span>
          <input required type="password" maxLength={500} autoComplete="new-password" value={form.client_secret ?? ''}
            onChange={(event) => setForm({ ...form, client_secret: event.target.value })} /></label>}
      <fieldset className="field"><legend className="field-label">Access</legend>
        {([['mail', 'Read mail'], ['files', 'Read files'], ['files_write', 'Write files (each write still needs your approval)']] as [ConnectorFeature, string][]).map(([feature, label]) =>
          <label key={feature} className="checkbox-row"><input type="checkbox" checked={form.features.includes(feature)} onChange={() => toggle(feature)} />{label}</label>)}
        {form.provider === 'microsoft' && <label className="checkbox-row"><input type="checkbox" checked={form.features.includes('sign_out_everywhere')}
          onChange={() => toggle('sign_out_everywhere')} />Allow the separate Sign out everywhere action (work or school accounts only; affects every app and browser)</label>}
      </fieldset>
      <button className="button button-primary" type="submit" disabled={busy || form.features.length === 0}>Save account</button>
    </form>
  </section>;
}
