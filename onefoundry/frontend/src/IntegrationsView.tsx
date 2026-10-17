import { GitHubSourceCheck } from './GitHubSourceCheck';
import { useEffect, useState, type FormEvent } from 'react';

import { ApiError } from './api';
import {
  addGitHubAccount, addGitHubSource, addLocalSource, discoverLocalRepositories, listGitHubAccounts, listGitHubRepos,
  confirmGitHubFixtureFetchProposal, confirmGitHubFixtureSnapshotProposal,
  getGitHubFixtureFetch, getGitHubFixtureSnapshot, getGitHubStageProposal,
  listProjectSources, listSourceRoots,
  proposeGitHubFixtureFetch, proposeGitHubFixtureSnapshot, proposeGitHubStage,
  removeGitHubAccount, removeProjectSource,
  type GitHubAccount, type GitHubRepo, type LocalRepositoryDiscovery, type ProjectSource, type SourceRoot,
} from './sourceApi';
import { listRuns, type Run } from './workApi';

type Snapshot = { roots: SourceRoot[]; localRepositories: LocalRepositoryDiscovery; sources: ProjectSource[]; accounts: GitHubAccount[] };
function message(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Could not confirm saved integration state. Check the local service and refresh.';
}
function localPathProblem(path: string): string | null {
  if (!path || path === '.') return null;
  if (path.startsWith('/') || path.includes('\\') || path.split('/').some((part) => !part || part === '.' || part === '..') ||
      [...path].some((char) => char.charCodeAt(0) < 32)) {
    return 'Enter a path inside the configured root, such as districtAI, or "." when the root is the repository. Absolute paths like /home/... cannot be used here.';
  }
  return null;
}
async function read(projectId: string, signal?: AbortSignal): Promise<Snapshot> {
  const [roots, localRepositories, sources, accounts] = await Promise.all([
    listSourceRoots(signal), discoverLocalRepositories(signal), listProjectSources(projectId, signal), listGitHubAccounts(projectId, signal),
  ]);
  return { roots, localRepositories, sources, accounts };
}

export function IntegrationsView({ projectId, onOpenCoding }: { projectId: string; onOpenCoding?: () => void }) {
  const [saved, setSaved] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [rootId, setRootId] = useState('');
  const [relativePath, setRelativePath] = useState('');
  const [manualLocalPath, setManualLocalPath] = useState(false);
  const [accountLabel, setAccountLabel] = useState('');
  const [token, setToken] = useState('');
  const [accountId, setAccountId] = useState('');
  const [repos, setRepos] = useState<GitHubRepo[] | null>(null);
  const [repoName, setRepoName] = useState('');
  const [branch, setBranch] = useState('');
  const [confirmAccountId, setConfirmAccountId] = useState<string | null>(null);
  const [confirmSourceId, setConfirmSourceId] = useState<string | null>(null);
  const [stageRunId, setStageRunId] = useState('');
  const [stageRuns, setStageRuns] = useState<Run[] | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setSaved(null); setError(null); setRepos(null); setStageRuns(null); setStageRunId('');
    void read(projectId, controller.signal).then((state) => {
      if (!controller.signal.aborted) {
        setSaved(state);
        setRootId((current) => state.roots.some((item) => item.id === current && item.available)
          ? current : state.roots.find((item) => item.available)?.id ?? '');
        setAccountId((current) => state.accounts.some((item) => item.id === current)
          ? current : state.accounts[0]?.id ?? '');
      }
    }).catch((failure: unknown) => {
      if (!(failure instanceof DOMException && failure.name === 'AbortError')) setError(message(failure));
    });
    return () => controller.abort();
  }, [projectId, refresh]);

  async function perform(action: () => Promise<void>) {
    setBusy(true); setError(null); setNotice(null);
    try { await action(); }
    catch (failure) { setError(`${message(failure)} Check saved state before retrying.`); }
    finally { setBusy(false); }
  }
  const availableRoots = saved?.roots.filter((item) => item.available) ?? [];
  const manualEntry = manualLocalPath || saved?.localRepositories.repositories.length === 0;
  const pathProblem = manualEntry ? localPathProblem(relativePath.trim()) : null;
  const selectedRepo = repos?.find((item) => item.full_name === repoName);
  const manualRuns = stageRuns?.filter((item) => item.status === 'running' && item.mode === 'manual_planning') ?? [];

  return <section className="integrations-page" aria-labelledby="integrations-title">
    <div className="view-heading"><div><p className="eyebrow">Project connections</p><h1 id="integrations-title">Repositories</h1>
      <p>Choose repository sources for this project. A saved source records the exact revision checked at selection.</p></div></div>
    {!saved && !error && <p className="setup-readback" role="status">Reading saved sources and connections…</p>}
    {error && <div className="setup-readback" role="alert"><p>{error}</p><button className="button button-outline" type="button" onClick={() => setRefresh((value) => value + 1)}>Refresh saved state</button></div>}
    {saved && <>
      <section className="integrations-panel panel" aria-labelledby="local-sources-title">
        <div className="integrations-panel-head"><div><p className="eyebrow">01 / On this device</p><h2 id="local-sources-title">Local Sources</h2></div>
          <span className="status-chip status-muted">Read-only selection</span></div>
        <p>Choose a discovered Git repository from OneFoundry's read-only local source root. Saving it records the current branch and revision for this project; it does not launch a coding runner.</p>
        {availableRoots.length === 0 || !saved.localRepositories.available ? <p className="setup-attention">No local repository is mounted. This is optional for manual projects. To add one, run <code>python3 scripts/local_repositories.py add /absolute/path/to/repository</code> from the <code>onefoundry/</code> installation folder on the computer running Docker, then refresh this page.</p> :
          <form className="integrations-form" onSubmit={(event: FormEvent) => { event.preventDefault(); void perform(async () => {
            const path = relativePath.trim();
            const problem = manualEntry ? localPathProblem(path) : null;
            if (problem) throw new ApiError(problem, null);
            const written = await addLocalSource(projectId, rootId, path);
            const sources = await listProjectSources(projectId);
            if (!sources.some((item) => item.id === written.id && item.revision === written.revision)) throw new ApiError('The local source was not confirmed in saved state.', null);
            setSaved({ ...saved, sources }); setRelativePath(''); setNotice('Local repository saved and confirmed.');
          }); }}>
            <label className="field"><span className="field-label">Read-only source root</span><select value={rootId} onChange={(event) => setRootId(event.target.value)}>{availableRoots.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
            {saved.localRepositories.repositories.length > 0 && !manualLocalPath && <label className="field"><span className="field-label">Discovered repository</span><select required value={relativePath} onChange={(event) => setRelativePath(event.target.value)}>
              <option value="">Choose a local repository</option>{saved.localRepositories.repositories.map((candidate) => <option key={candidate.relative_path} value={candidate.relative_path}>{candidate.display_name} · {candidate.branch}{candidate.dirty ? ' · uncommitted changes' : ''}</option>)}</select></label>}
            {(manualLocalPath || saved.localRepositories.repositories.length === 0) && <div className="field"><label htmlFor="local-repo-path" className="field-label">Repository path within root</label><input id="local-repo-path" required value={relativePath} aria-invalid={Boolean(pathProblem)} aria-describedby="local-path-help" onChange={(event) => { setRelativePath(event.target.value); setError(null); }} placeholder="districtAI or ." /><small id="local-path-help">Use a folder name beneath the root, or "." if the root itself is the repository. A host path such as /home/ken/districtAI must first be mounted read-only into OneFoundry.</small>{pathProblem && <span className="form-error" role="alert">{pathProblem}</span>}</div>}
            {saved.localRepositories.repositories.length > 0 && <button className="button button-outline" type="button" onClick={() => { setManualLocalPath(!manualLocalPath); setRelativePath(''); }}>{manualLocalPath ? 'Choose discovered repository' : 'Enter path manually'}</button>}
            <button className="button button-primary" type="submit" disabled={busy || !rootId || !relativePath.trim() || Boolean(pathProblem)}>Add local repository</button>
          </form>}
        {saved.localRepositories.available && <p className="integrations-limit">Only mounted repositories can be added here. To add another checkout, run <code>python3 scripts/local_repositories.py add /absolute/path/to/repository</code> from the <code>onefoundry/</code> installation folder on the computer running Docker, then refresh this page. Saving a source makes it available to eligible offline source checks; it does not enable live coding.</p>}
        {saved.localRepositories.truncated && <p className="integrations-limit">Local discovery reached its 25-repository limit. Enter a relative path manually if yours is missing.</p>}
      </section>

      <section className="integrations-panel panel" aria-labelledby="github-title">
        <div className="integrations-panel-head"><div><p className="eyebrow">02 / Repository catalog</p><h2 id="github-title">GitHub</h2></div>
          <span className="status-chip status-soft">Operator connection</span></div>
        <p>Connect a GitHub repository account for this project, then explicitly load repositories and select a branch. This is separate from a GitHub Copilot model connection. The token is never shown after submission.</p>
        <form className="integrations-form" onSubmit={(event: FormEvent) => { event.preventDefault(); void perform(async () => {
          const submittedToken = token; setToken('');
          const written = await addGitHubAccount(projectId, accountLabel.trim(), submittedToken);
          const accounts = await listGitHubAccounts(projectId);
          if (!accounts.some((item) => item.id === written.id && item.credential_present)) throw new ApiError('The GitHub connection was not confirmed in saved state.', null);
          setSaved({ ...saved, accounts }); setAccountLabel(''); setAccountId(written.id); setNotice('GitHub token saved; use Load repositories to verify access.');
        }); }}>
          <label className="field"><span className="field-label">Connection label</span><input required maxLength={80} value={accountLabel} onChange={(event) => setAccountLabel(event.target.value)} placeholder="Company GitHub" /></label>
          <label className="field"><span className="field-label">GitHub token</span><input required type="password" autoComplete="new-password" value={token} onChange={(event) => setToken(event.target.value)} /></label>
          <button className="button button-outline" type="submit" disabled={busy || !accountLabel.trim() || !token.trim()}>Save GitHub connection</button>
        </form>
        {saved.accounts.length > 0 && <div className="integrations-repo-picker">
          <ul className="integrations-sources" aria-label="Saved GitHub connections">{saved.accounts.map((item) => <li key={item.id}>
            <strong>{item.label}</strong><span>{item.credential_present ? 'Credential stored' : 'Credential unavailable'}</span>
            {confirmAccountId === item.id ? <div className="integrations-row-actions"><span>Disconnect this GitHub account? Saved sources must be removed first.</span>
              <button className="button button-outline" type="button" disabled={busy} onClick={() => setConfirmAccountId(null)}>Cancel</button>
              <button className="button button-outline" type="button" disabled={busy} onClick={() => { void perform(async () => {
                await removeGitHubAccount(projectId, item.id);
                const accounts = await listGitHubAccounts(projectId);
                if (accounts.some((account) => account.id === item.id)) throw new ApiError('The GitHub connection remains in saved state.', null);
                setSaved({ ...saved, accounts }); setConfirmAccountId(null); setRepos(null);
                setAccountId(accounts[0]?.id ?? ''); setNotice('GitHub connection removed and confirmed.');
              }); }}>Disconnect</button></div> : <button className="button button-outline" type="button" onClick={() => setConfirmAccountId(item.id)}>Disconnect</button>}
          </li>)}</ul>
          <label className="field"><span className="field-label">Saved connection</span><select value={accountId} onChange={(event) => { setAccountId(event.target.value); setRepos(null); setRepoName(''); setBranch(''); }}>
            {saved.accounts.map((item) => <option key={item.id} value={item.id}>{item.label} · {item.credential_present ? 'Credential stored' : 'Credential unavailable'}</option>)}</select></label>
          <button className="button button-outline" type="button" disabled={busy || !accountId || !saved.accounts.some((item) => item.id === accountId && item.credential_present)} onClick={() => { void perform(async () => {
            const listed = await listGitHubRepos(projectId, accountId); setRepos(listed); setRepoName(''); setBranch('');
            setNotice(`${listed.length} GitHub repositories returned for selection.`);
          }); }}>Load repositories</button>
          <p className="integrations-limit">Showing up to 50 repositories. Enter owner and repo manually if yours is not listed.</p>
          {repos && <>{repos.length === 0 && <p className="setup-attention">No repositories were returned for this connection. Enter owner/repo manually if you know it.</p>}
            <form className="integrations-form" onSubmit={(event: FormEvent) => { event.preventDefault(); void perform(async () => {
              const written = await addGitHubSource(projectId, accountId, repoName, branch.trim());
              const sources = await listProjectSources(projectId);
              if (!sources.some((item) => item.id === written.id && item.revision === written.revision)) throw new ApiError('The GitHub source was not confirmed in saved state.', null);
              setSaved({ ...saved, sources }); setNotice('GitHub repository saved at its confirmed revision.');
            }); }}>
              <label className="field"><span className="field-label">Repository (owner/repo)</span><input required list="github-repositories" value={repoName} onChange={(event) => {
                const chosen = repos.find((item) => item.full_name === event.target.value);
                setRepoName(event.target.value); setBranch(chosen?.default_branch ?? '');
              }} placeholder="owner/repo" /><datalist id="github-repositories">{repos.map((item) => <option key={item.full_name} value={item.full_name} />)}</datalist></label>
              <label className="field"><span className="field-label">Branch</span><input required value={branch} onChange={(event) => setBranch(event.target.value)} placeholder={selectedRepo?.default_branch ?? 'main'} /></label>
              <button className="button button-primary" type="submit" disabled={busy || !repoName || !branch.trim()}>Select repository</button>
            </form></>}
        </div>}
      </section>

      <section className="integrations-panel panel" aria-labelledby="selected-sources-title">
        <div className="integrations-panel-head"><div><p className="eyebrow">03 / Saved for this project</p><h2 id="selected-sources-title">Selected repositories</h2></div></div>
        {saved.sources.length === 0 ? <p>No repository selected yet. Select a local or GitHub source above.</p> :
          <ul className="integrations-sources">{saved.sources.map((item) => <li key={item.id}>
            <strong>{item.display_name}</strong><span>{item.kind === 'local' ? 'Local' : 'GitHub'} · {item.branch}</span>
            <small>{item.availability === 'unavailable' ? 'Unavailable; check the saved root before use' : 'Verified at selection; currentness has not been rechecked'}</small>
            {item.kind === 'github' && <GitHubSourceCheck projectId={projectId} source={item} />}
            {item.kind === 'github' && <div className="integrations-row-actions">
              <button className="button button-outline" type="button" disabled={busy}
                onClick={() => { void perform(async () => {
                  setStageRuns(await listRuns(projectId)); setNotice('Manual work orders loaded for exact staging proposal.');
                }); }}>Load manual work orders</button>
              {stageRuns && <>
              <label className="field"><span className="field-label">Manual work order for staging proposal</span>
                <select value={stageRunId} onChange={(event) => setStageRunId(event.target.value)}>
                  <option value="">Choose a running manual work order</option>
                  {manualRuns.map((run) => <option key={run.id} value={run.id}>{run.work_order_title ?? 'Previous work order'}</option>)}
                </select></label>
              <button className="button button-outline" type="button" disabled={busy || !stageRunId}
                onClick={() => { void perform(async () => {
                  const approvalId = await proposeGitHubStage(projectId, stageRunId, item.id);
                  const confirmed = await getGitHubStageProposal(projectId, approvalId);
                  if (!confirmed.source_current || confirmed.source.saved_revision !== item.revision || confirmed.decision !== 'pending') {
                    throw new ApiError('The saved stage proposal did not match this source. Inspect the approval inbox.', null);
                  }
                  setNotice(`Exact GitHub staging proposal saved for ${item.display_name}. Review it in the Approval inbox. Stage status: not launched.`);
                }); }}>Propose exact stage</button>
              <button className="button button-outline" type="button" disabled={busy || !stageRunId}
                onClick={() => { void perform(async () => {
                  const approvalId = await proposeGitHubFixtureFetch(projectId, stageRunId, item.id);
                  const confirmed = await getGitHubFixtureFetch(projectId, approvalId);
                  if (!confirmed.source_current || confirmed.source.saved_revision !== item.revision ||
                      confirmed.decision !== 'pending' || confirmed.expired) {
                    throw new ApiError('The fixture fetch proposal did not match this source.', null);
                  }
                  confirmGitHubFixtureFetchProposal(projectId, stageRunId, item.id);
                  setNotice(`Synthetic verification-only HTTPS fixture proposal saved for ${item.display_name}. Review it in the Approval inbox. No coding checkout or real GitHub fetch.`);
                }); }}>Propose synthetic HTTPS verification</button>
              <button className="button button-outline" type="button" disabled={busy || !stageRunId}
                onClick={() => { void perform(async () => {
                  const approvalId = await proposeGitHubFixtureSnapshot(projectId, stageRunId, item.id);
                  const confirmed = await getGitHubFixtureSnapshot(projectId, approvalId);
                  if (!confirmed.source_current || confirmed.source.saved_revision !== item.revision ||
                      confirmed.decision !== 'pending' || confirmed.expired) {
                    throw new ApiError('The fixture snapshot proposal did not match this source.', null);
                  }
                  confirmGitHubFixtureSnapshotProposal(projectId, stageRunId, item.id);
                  setNotice(`Synthetic HTTPS snapshot proposal saved for ${item.display_name}. Approval allows a durable local copy from the fake fixture. No real GitHub fetch or coding launch.`);
                }); }}>Propose synthetic HTTPS snapshot</button>
              {manualRuns.length === 0 && <small>Start a manual work order before proposing staging.</small>}
              </>}
            </div>}
            {confirmSourceId === item.id ? <div className="integrations-row-actions"><span>Remove this repository from the project?</span>
              <button className="button button-outline" type="button" disabled={busy} onClick={() => setConfirmSourceId(null)}>Cancel</button>
              <button className="button button-outline" type="button" disabled={busy} onClick={() => { void perform(async () => {
                await removeProjectSource(projectId, item.id);
                const sources = await listProjectSources(projectId);
                if (sources.some((source) => source.id === item.id)) throw new ApiError('The repository remains in saved state.', null);
                setSaved({ ...saved, sources }); setConfirmSourceId(null); setNotice('Repository removed and confirmed.');
              }); }}>Remove repository</button></div> : <button className="button button-outline" type="button" onClick={() => setConfirmSourceId(item.id)}>Remove repository</button>}
          </li>)}</ul>}
      </section>
      {notice && <p className="notice" role="status">{notice}</p>}
      <div className="integrations-readiness">
        <section className="integrations-panel panel"><p className="eyebrow">Readiness</p><h2>Coding agents</h2>
          <p>Codex CLI and Claude Code run through a separate trusted local worker for an eligible Engineer work order. They use the operator's CLI login and host access. A saved repository or model account alone does not make a coding backend ready.</p>
          {onOpenCoding && <button className="button button-outline" type="button" onClick={onOpenCoding}>Open coding agents</button>}</section>
      </div>
    </>}
  </section>;
}
