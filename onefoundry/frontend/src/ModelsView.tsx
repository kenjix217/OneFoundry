import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';

import { ApiError } from './api';
import {
  addModelAccount, listCatalogModels, listModelAccounts, refreshModelAccount, removeModelAccount,
  replaceModelCredential, listModelProviders, type CatalogModel, type ModelAccount, type ModelProvider,
  type ProviderDescriptor, startCodexConnection, finishCodexConnection, getCodexChallenge, type CodexChallenge,
  startGrokConnection, finishGrokConnection, getGrokChallenge, restartCodexConnection, restartGrokConnection,
  reconnectGrokConnection,
} from './modelApi';

const CATEGORIES = ['text', 'image', 'audio', 'embedding', 'realtime', 'other'];

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : 'An unexpected error occurred. Check saved state before retrying.';
}

export function ModelsView({ projectId }: {projectId: string}) {
  const [providers, setProviders] = useState<ProviderDescriptor[]>([]);
  const [accounts, setAccounts] = useState<ModelAccount[]>([]);
  const [models, setModels] = useState<CatalogModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [provider, setProvider] = useState<ModelProvider>('openai');
  const [label, setLabel] = useState('');
  const [endpoint, setEndpoint] = useState('http://ollama:11434');
  const addSecret = useRef<HTMLInputElement>(null);
  const replaceSecret = useRef<HTMLInputElement>(null);
  const [changing, setChanging] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [codexChallenge, setCodexChallenge] = useState<CodexChallenge | null>(null);
  const [savedGrokAccount, setSavedGrokAccount] = useState<string | null>(null);
  const recoveredPendingAccounts = useRef<Set<string>>(new Set());

  useEffect(() => { setCodexChallenge(null); setSavedGrokAccount(null); recoveredPendingAccounts.current.clear(); }, [projectId]);

  const load = useCallback(async (signal?: AbortSignal) => {
    const [nextProviders, nextAccounts, nextModels] = await Promise.all([
      listModelProviders(projectId, signal), listModelAccounts(projectId, signal), listCatalogModels(projectId, signal),
    ]);
    setProviders(nextProviders);
    setProvider((current) => nextProviders.some((entry) => entry.id === current && entry.connectable)
      ? current : nextProviders.find((entry) => entry.connectable)?.id ?? current);
    setAccounts(nextAccounts);
    setModels(nextModels);
  }, [projectId]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void load(controller.signal).catch((failure: unknown) => {
      if (failure instanceof DOMException && failure.name === 'AbortError') return;
      setError(errorMessage(failure));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (loading || busy !== null || codexChallenge) return;
    const pending = accounts.filter((account) => account.auth_kind === 'account_runtime' && !account.credential_present);
    if (pending.length === 0) return;
    let cancelled = false;
    const resume = async () => {
      let lastFailure: unknown;
      for (const account of pending) {
        if (cancelled) return;
        try {
          const challenge = account.provider === 'xai'
            ? await getGrokChallenge(projectId, account.id)
            : await getCodexChallenge(projectId, account.id);
          if (!cancelled) { setError(null); setCodexChallenge(challenge); }
          return;
        } catch (failure) {
          lastFailure = failure;
          if (account.provider !== 'xai' || !(failure instanceof ApiError && failure.status === 409 &&
            failure.message === 'Grok account has saved tokens; complete the saved connection') ||
            recoveredPendingAccounts.current.has(account.id)) continue;
          recoveredPendingAccounts.current.add(account.id);
          try {
            await finishGrokConnection(projectId, account.id);
            if (cancelled) return;
            await load();
            if (!cancelled) {
              setSavedGrokAccount(null);
              setError(null);
              setNotice('Grok account sign-in saved. Discover models to inspect the catalog; live inference access is unverified.');
            }
            return;
          } catch (recoveryFailure) {
            lastFailure = recoveryFailure;
            if (!cancelled) setSavedGrokAccount(account.id);
          }
        }
      }
      if (!cancelled && lastFailure) setError(errorMessage(lastFailure));
    };
    void resume();
    return () => { cancelled = true; };
  }, [accounts, busy, codexChallenge, load, loading, projectId]);

  useEffect(() => {
    const challenge = codexChallenge;
    if (!challenge || (challenge.account.provider !== 'openai_codex_runtime' && challenge.account.provider !== 'xai')) return;
    const grok = challenge.account.provider === 'xai';
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      if (cancelled) return;
      if (Date.now() >= challenge.expires_at * 1000) {
        setNotice(null);
        setError(`${grok ? 'Grok' : 'Codex'} sign-in code expired. Start fresh sign-in on this account.`);
        return;
      }
      try {
        if (grok) await finishGrokConnection(projectId, challenge.account.id);
        else await finishCodexConnection(projectId, challenge.account.id);
        if (cancelled) return;
        await load();
        if (!cancelled) {
          setCodexChallenge(null);
          setError(null);
          setNotice(grok
            ? 'Grok account sign-in saved. Discover models to inspect the catalog; live inference access is unverified.'
            : 'Codex device sign-in saved. Discover models to inspect the catalog; subscription inference access is unverified.');
        }
      } catch (failure) {
        if (cancelled) return;
        const pendingMessage = grok ? 'Complete Grok sign-in, then check again' : 'Complete Codex sign-in, then check again';
        if (!(failure instanceof ApiError && failure.status === 409 && failure.message === pendingMessage)) {
          setNotice(null);
          setError(errorMessage(failure));
          return;
        }
        let intervalSeconds = challenge.interval_seconds;
        if (grok) {
          try {
            const current = await getGrokChallenge(projectId, challenge.account.id);
            if (cancelled) return;
            intervalSeconds = current.interval_seconds;
          } catch (readFailure) {
            if (!cancelled) setError(errorMessage(readFailure));
            return;
          }
        }
        timer = setTimeout(() => { void poll(); }, Math.max(1, intervalSeconds) * 1000);
      }
    };
    void poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [codexChallenge, load, projectId]);

  async function addAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const apiKey = addSecret.current?.value ?? '';
    const selectedProvider = providers.find((entry) => entry.id === provider);
    if (!selectedProvider?.connectable) { setError('This provider has no OneFoundry connection yet.'); return; }
    if (addSecret.current) addSecret.current.value = '';
    setBusy('add'); setError(null); setNotice(null);
    let connectionStarted = false;
    try {
      if (selectedProvider.auth_kind === 'account_runtime') {
        setCodexChallenge(null);
        const challenge = provider === 'xai'
          ? await startGrokConnection(projectId, label.trim())
          : await startCodexConnection(projectId, label.trim());
        setCodexChallenge(challenge);
      } else {
        await addModelAccount(projectId, selectedProvider.auth_kind === 'none'
          ? { provider, label: label.trim(), endpoint: endpoint.trim() }
          : { provider, label: label.trim(), api_key: apiKey });
      }
      connectionStarted = true;
      setLabel('');
      await load();
      setNotice(selectedProvider.auth_kind === 'account_runtime'
        ? selectedProvider.id === 'openai_codex_runtime'
          ? 'Use the code in the new account card to sign in. Codex connects automatically after verification.'
          : 'Use the code in the new account card to sign in. Grok connects automatically after verification.'
        : 'Model connection saved. Choose Discover models to fetch its catalog.');
    } catch (failure) {
      setError(failure instanceof ApiError && (failure.status === 502 || failure.status === 409 || failure.status === 503) &&
        selectedProvider.auth_kind === 'account_runtime' && !connectionStarted
        ? failure.message
        : failure instanceof ApiError && failure.status === 422
          ? 'Connection details were rejected. Check the key or local endpoint.'
          : 'Could not save the model connection. Check the saved state before retrying.');
    } finally { if (addSecret.current) addSecret.current.value = ''; setBusy(null); }
  }

  async function showAccountCode(account: ModelAccount) {
    if (codexChallenge?.account.id === account.id) return;
    setBusy(account.id); setError(null);
    setCodexChallenge(null);
    try { setCodexChallenge(account.provider === 'xai'
      ? await getGrokChallenge(projectId, account.id)
      : await getCodexChallenge(projectId, account.id)); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(null); }
  }

  async function restartAccount(account: ModelAccount) {
    setBusy(account.id); setError(null); setNotice(null); setCodexChallenge(null); setSavedGrokAccount(null);
    try {
      const challenge = account.provider === 'xai'
        ? await restartGrokConnection(projectId, account.id)
        : await restartCodexConnection(projectId, account.id);
      setCodexChallenge(challenge);
      await load();
      setNotice(`Fresh sign-in code is shown in ${account.label}'s account card. ${account.provider === 'xai' ? 'Grok' : 'Codex'} connects automatically after verification.`);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(null); }
  }

  async function reconnectGrok(account: ModelAccount) {
    setBusy(account.id); setError(null); setNotice(null); setCodexChallenge(null);
    try {
      const challenge = await reconnectGrokConnection(projectId, account.id);
      setCodexChallenge(challenge);
      await load();
      setNotice(`Fresh sign-in code is shown in ${account.label}'s account card. Grok connects automatically after verification.`);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(null); }
  }

  async function completeSavedGrok(account: ModelAccount) {
    setBusy(account.id); setError(null); setNotice(null);
    try {
      await finishGrokConnection(projectId, account.id);
      await load();
      setSavedGrokAccount(null);
      setNotice('Grok account sign-in saved. Discover models to inspect the catalog; live inference access is unverified.');
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(null); }
  }

  async function rotate(account: ModelAccount) {
    const secret = replaceSecret.current?.value ?? '';
    if (!secret.trim()) return;
    if (replaceSecret.current) replaceSecret.current.value = '';
    setBusy(account.id); setError(null); setNotice(null);
    try {
      await replaceModelCredential(projectId, account.id, secret);
      setChanging(null);
      await load();
      setNotice(`${account.label} credential replaced. Discover models again if access changed.`);
    } catch (failure) {
      setError(failure instanceof ApiError && failure.status === 422
        ? 'API key was rejected. Enter a valid key and try again.'
        : 'Could not replace the API key. Check the saved state before retrying.');
    } finally { if (replaceSecret.current) replaceSecret.current.value = ''; setBusy(null); }
  }

  async function discover(account: ModelAccount) {
    setBusy(account.id); setError(null); setNotice(null);
    try {
      const refreshed = await refreshModelAccount(projectId, account.id);
      await load();
      setNotice(`${account.label} model catalog refreshed: ${refreshed.models.length} models returned by the provider.`);
    } catch (failure) {
      const message = errorMessage(failure);
      try {
        await load();
        setError(`${message} Saved catalog state was reloaded; check its current availability.`);
      } catch (reloadFailure) {
        setAccounts((current) => current.map((item) => item.id === account.id ? { ...item, catalog_valid: false } : item));
        setModels((current) => current.map((item) => item.account_id === account.id ? { ...item, available: false } : item));
        setError(`${message} Could not reload saved catalog state: ${errorMessage(reloadFailure)}`);
      }
    } finally { setBusy(null); }
  }

  async function revoke(account: ModelAccount) {
    setBusy(account.id); setError(null); setNotice(null);
    try {
      await removeModelAccount(projectId, account.id);
      if (codexChallenge?.account.id === account.id) setCodexChallenge(null);
      await load();
      setRemoving(null); setChanging(null);
      setNotice(`Removed ${account.label}.`);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(null); }
  }

  const grouped = useMemo(() => {
    const q = search.trim().toLowerCase();
    return providers.map((entry) => ({
      ...entry,
      categories: CATEGORIES.map((category) => ({
        category,
        models: models.filter((model) => model.provider === entry.id && model.category === category &&
          (!q || `${model.display_name} ${model.model_id} ${model.category}`.toLowerCase().includes(q))),
      })).filter((group) => group.models.length > 0),
    })).filter((entry) => entry.categories.length > 0);
  }, [models, providers, search]);

  const selectedProvider = providers.find((entry) => entry.id === provider);
  const providerName = (id: ModelProvider) => providers.find((entry) => entry.id === id)?.name ?? id;

  return <div className="models-page">
    <div className="view-heading">
      <div><p className="eyebrow">Provider connections</p><h1>AI connections</h1>
        <p>Keep model connections for this project, then choose discovered models for each agent profile. Each model shows whether it can run work in this installation.</p></div>
    </div>
    {notice && <p className="notice" role="status">{notice}</p>}
    {error && <div className="form-error" role="alert">{error} <button type="button" onClick={() => { setLoading(true); void load().then(() => setError(null)).catch((failure: unknown) => setError(errorMessage(failure))).finally(() => setLoading(false)); }}>Reload saved state</button></div>}
    {loading ? <p role="status">Loading model accounts…</p> : <div className="model-panels">
      <section className="panel models-section" aria-labelledby="accounts-heading">
        <div className="section-heading"><div><p className="eyebrow">01 / Model connections</p><h2 id="accounts-heading">Connect a provider</h2><p>Connect an AI account, discover its models, then assign a model to each agent in Team. Unavailable connections are marked in the provider list.</p></div></div>
        <form className="model-add-form" onSubmit={(event) => { void addAccount(event); }}>
          <label className="field"><span className="field-label">Provider</span><select value={provider} onChange={(event) => setProvider(event.target.value as ModelProvider)}>
            {providers.map((entry) => <option key={entry.id} value={entry.id} disabled={!entry.connectable}>{entry.name}{entry.connectable ? '' : entry.id === 'xai' ? ' · OAuth client ID needed' : ' · adapter unavailable'}</option>)}
          </select></label>
          {selectedProvider?.text_runtime_available && selectedProvider.id !== 'openai' && selectedProvider.id !== 'grok' && selectedProvider.id !== 'ollama' && selectedProvider.auth_kind === 'api_key' && <p className="field-hint">This provider uses its advertised OpenAI-compatible API. OneFoundry has offline contract tests; live behavior depends on your provider account and model.</p>}
          <label className="field"><span className="field-label">Account label</span><input value={label} onChange={(event) => setLabel(event.target.value)} maxLength={80} required placeholder="e.g. Company API account" /></label>
          {selectedProvider?.auth_kind === 'none'
            ? <label className="field"><span className="field-label">Local endpoint</span><input value={endpoint} onChange={(event) => setEndpoint(event.target.value)} required placeholder="http://ollama:11434" /></label>
            : selectedProvider?.auth_kind === 'api_key' ? <label className="field"><span className="field-label">API key</span><input ref={addSecret} type="password" autoComplete="off" required placeholder="Paste API key" /></label> : null}
          {selectedProvider?.id === 'ollama' && <p className="field-hint">Start the optional local service with <code>docker compose --profile local-model up -d ollama</code>, then install a model there. Host-run OneFoundry can also use a reachable loopback Ollama endpoint.</p>}
          {selectedProvider?.id === 'openai_codex_runtime' && <p className="field-hint">Codex uses a separate ChatGPT device sign-in. OneFoundry stores this connection in its own protected volume and does not change your local Codex login. This legacy connection does not verify permission for the current ChatGPT plan inference route.</p>}
          {selectedProvider?.id === 'xai' && <p className="field-hint">Grok subscription uses xAI device sign-in. Configure XAI_OAUTH_CLIENT_ID in <code>onefoundry/.env</code> to enable it; the API-key Grok provider is separate.</p>}
          <button className="button button-primary" type="submit" disabled={busy !== null || !selectedProvider?.connectable}>{busy === 'add' ? 'Saving…' : selectedProvider?.id === 'xai' ? 'Connect Grok account' : selectedProvider?.auth_kind === 'account_runtime' ? 'Connect Codex account' : 'Add connection'}</button>
        </form>
        {accounts.length === 0 ? <p className="quiet-note">No model connections yet. Add one above, then click Discover models. Discovery makes a provider request only when you click that button.</p> :
          <div className="model-accounts">{accounts.map((account) => <article className="model-account" key={account.id}>
            <div className="model-account-head"><div><small>{providerName(account.provider)} · {account.auth_kind === 'none' ? 'Local endpoint' : account.auth_kind === 'account_runtime' ? account.provider === 'xai' ? 'xAI account' : 'ChatGPT account' : 'API key'}</small><h3>{account.label}</h3></div>
              <span className={`status-chip ${account.auth_kind === 'none' || account.credential_present ? 'status-soft' : 'status-muted'}`}>{account.auth_kind === 'none' ? 'Keyless local' : account.auth_kind === 'account_runtime' ? account.credential_present ? account.provider === 'openai_codex_runtime' ? 'Device signed in' : 'Connected' : 'Sign-in pending' : account.credential_present ? 'Key stored' : 'Key missing'}</span></div>
            {account.endpoint && <p>Endpoint: {account.endpoint}</p>}
            <p>{providers.find((entry) => entry.id === account.provider)?.text_runtime_available
              ? (['openai', 'grok', 'ollama', 'openai_codex_runtime', 'xai', 'anthropic', 'gemini'].includes(account.provider) ? 'Text task path implemented · live generation unverified' : 'Compatible text adapter · live generation unverified')
              : 'Catalog only · text runtime unavailable'}</p>
            <p>{account.model_count} discovered models · {account.last_refreshed_at ? `Updated ${new Date(account.last_refreshed_at).toLocaleString()}` : 'Never discovered'}{!account.catalog_valid && account.last_refreshed_at ? ' · Refresh needed before selection' : ''}</p>
            {account.provider === 'openai_codex_runtime' && account.credential_present && <p className="field-hint">Device sign-in and model discovery do not confirm subscription inference access. Review the saved inference diagnostic before starting another attempt.</p>}
            {codexChallenge?.account.id === account.id && <div className="model-inline-form" role="status"><p>Open <a href={codexChallenge.verification_url} target="_blank" rel="noreferrer">{account.provider === 'xai' ? 'xAI Grok' : 'OpenAI Codex'} sign-in</a> and enter code <strong>{codexChallenge.user_code}</strong>. This account connects automatically after verification. This code expires at {new Date(codexChallenge.expires_at * 1000).toLocaleTimeString()}.</p></div>}
            <div className="model-account-actions">
              {account.auth_kind === 'account_runtime' && !account.credential_present && <>
                <button className="button button-outline" type="button" disabled={busy !== null} onClick={() => { void showAccountCode(account); }}>Show sign-in code</button>
                <button className="button button-outline" type="button" disabled={busy !== null} onClick={() => { void restartAccount(account); }}>Start fresh sign-in</button>
                {account.id === savedGrokAccount && <button className="button button-outline" type="button" disabled={busy !== null} onClick={() => { void completeSavedGrok(account); }}>Complete saved sign-in</button>}
              </>}
              {account.provider === 'xai' && account.credential_present && <button className="button button-outline" type="button" disabled={busy !== null} onClick={() => { void reconnectGrok(account); }}>Sign in again</button>}
              <button className="button button-outline" type="button" disabled={busy !== null || account.auth_kind !== 'none' && !account.credential_present} onClick={() => { void discover(account); }}>{busy === account.id ? 'Working…' : 'Discover models'}</button>
              {account.auth_kind === 'api_key' && <button className="button button-quiet" type="button" disabled={busy !== null} onClick={() => { setChanging(changing === account.id ? null : account.id); setRemoving(null); }}>Replace key</button>}
              <button className="button button-quiet" type="button" disabled={busy !== null} onClick={() => { setRemoving(removing === account.id ? null : account.id); setChanging(null); }}>Remove account</button>
            </div>
            {changing === account.id && <div className="model-inline-form"><label className="field"><span className="field-label">New API key for {account.label}</span><input ref={replaceSecret} type="password" autoComplete="off" /></label>
              <button className="button button-primary" type="button" disabled={busy !== null} onClick={() => { void rotate(account); }}>Save replacement</button></div>}
            {removing === account.id && <div className="model-remove-confirm"><p>Remove {account.label} and its discovered models? Bound profiles must be changed first.</p><button className="button button-outline" type="button" onClick={() => setRemoving(null)}>Cancel</button><button className="button button-primary" type="button" disabled={busy !== null} onClick={() => { void revoke(account); }}>Confirm removal</button></div>}
          </article>)}</div>}
      </section>
      <section className="panel models-section" aria-labelledby="catalog-heading">
        <div className="section-heading"><div><p className="eyebrow">02 / Discovered catalog</p><h2 id="catalog-heading">Browse models</h2><p>“Text” shows the task OneFoundry can route today: text input and text output. It does not describe every input or output a model may support. OneFoundry does not yet send images or video through these connections.</p></div></div>
        {models.length === 0 ? <p className="quiet-note">No models discovered yet. Add a connection and click Discover models to populate profile choices.</p> : <>
          <label className="field model-search"><span className="field-label">Search models</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Name, ID, or category" /></label>
          {grouped.length === 0 ? <p className="quiet-note">No models match this search.</p> : grouped.map((entry) => <div className="model-provider-group" key={entry.id}>
            <h3>{entry.name}</h3>{entry.categories.map((group) => <div key={group.category}><h4>{group.category}</h4><div className="model-catalog-grid">{group.models.map((model) => <article key={model.id} className="model-catalog-card">
              <strong>{model.display_name}</strong><code>{model.model_id}</code><span className={`status-chip ${model.available ? 'status-soft' : 'status-muted'}`}>{model.available ? model.category === 'text' && entry.text_runtime_available ? 'OneFoundry text tasks' : 'Catalog ready' : 'Unavailable'}</span>
              <small>{accounts.find((account) => account.id === model.account_id)?.label ?? 'Unknown account'}</small>
              {model.reasoning_efforts.length > 0 && <small>Reasoning choices: {model.reasoning_efforts.join(', ')} · choose in Team</small>}
            </article>)}</div></div>)}
          </div>)}
        </>}
      </section>
    </div>}
  </div>;
}
