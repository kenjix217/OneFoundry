import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, fillStarterProfiles, getProject, listProfiles, updateProfile, updateProjectBrief, type Profile, type Project } from './api';
import { listCatalogModels, listModelAccounts, listModelProviders, type CatalogModel, type ModelAccount, type ProviderDescriptor } from './modelApi';
import { createWorkOrder, listRuns, listWorkOrders, type Run, type WorkOrder } from './workApi';
import { addLocalSource, discoverLocalRepositories, listProjectSources, listSourceRoots, type LocalRepositoryDiscovery, type ProjectSource, type SourceRoot } from './sourceApi';
import { InputFilePicker } from './InputFilePicker';
import { listInputFiles, uploadInputFiles, type InputFileMeta, type PendingInputFile } from './inputFileApi';

export type SetupDestination = 'team' | 'models' | 'integrations' | 'work' | 'activity';
type State = { project: Project; accounts: ModelAccount[]; providers: ProviderDescriptor[]; models: CatalogModel[]; profiles: Profile[]; orders: WorkOrder[]; runs: Run[] };
const labels = ['Project brief', 'Starter team', 'Model connection', 'Project repository', 'First work order', 'Saved outcome'];
const starters = ['Engineer', 'QA', 'Researcher', 'GTM', 'Sales'] as const;
function message(error: unknown) { return error instanceof ApiError ? error.message : 'Could not confirm saved setup state. Check the local service and refresh before retrying.'; }
function coreProgress(state: State): boolean[] {
  return [Boolean(state.project.goal && state.project.desired_outcome),
    starters.every((name) => state.profiles.some((profile) => profile.name === name && profile.instructions.trim())),
    state.orders.length > 0, state.runs.some((run) => run.status === 'done' || run.status === 'failed')];
}
async function readState(id: string, signal?: AbortSignal): Promise<State> {
  const [project, accounts, providers, models, profiles, orders, runs] = await Promise.all([
    getProject(id, signal), listModelAccounts(id, signal), listModelProviders(id, signal), listCatalogModels(id, signal),
    listProfiles(id, signal), listWorkOrders(id, signal), listRuns(id, '', signal),
  ]);
  return { project, accounts, providers, models, profiles, orders, runs };
}

export function SetupView({ project, onNavigate, onBriefSaved }: { project: Pick<Project, 'id' | 'name'>; onNavigate: (view: SetupDestination, runId?: string) => void; onBriefSaved?: (project: Project) => void }) {
  const [saved, setSaved] = useState<State | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [step, setStep] = useState(0);
  const chosen = useRef(false);
  const loadedProjectId = useRef(project.id);
  const [goal, setGoal] = useState('');
  const [outcome, setOutcome] = useState('');
  const [modelId, setModelId] = useState('');
  const [setupEfforts, setSetupEfforts] = useState<Record<string, string>>({});
  const [title, setTitle] = useState('');
  const [instructions, setInstructions] = useState('');
  const [profileId, setProfileId] = useState('');
  const [busy, setBusy] = useState(false);
  const [repositoryState, setRepositoryState] = useState<{ roots: SourceRoot[]; local: LocalRepositoryDiscovery; sources: ProjectSource[] } | null>(null);
  const [repositoryError, setRepositoryError] = useState<string | null>(null);
  const [repositoryRefresh, setRepositoryRefresh] = useState(0);
  const [sourceChoice, setSourceChoice] = useState('');
  const [projectFiles, setProjectFiles] = useState<PendingInputFile[]>([]);
  const [projectInputFiles, setProjectInputFiles] = useState<InputFileMeta[]>([]);
  const [workFiles, setWorkFiles] = useState<PendingInputFile[]>([]);
  const [createdOrderId, setCreatedOrderId] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    if (loadedProjectId.current !== project.id) { chosen.current = false; loadedProjectId.current = project.id; }
    setSaved(null); setError(null);
    void readState(project.id, controller.signal).then((state) => {
      if (controller.signal.aborted) return;
      setSaved(state); setGoal(state.project.goal); setOutcome(state.project.desired_outcome);
      if (!chosen.current) {
        const first = coreProgress(state).findIndex((value) => !value);
        setStep(first < 0 ? 5 : [0, 1, 4, 5][first]);
      }
    }).catch((failure: unknown) => { if (!(failure instanceof DOMException && failure.name === 'AbortError')) setError(message(failure)); });
    return () => controller.abort();
  }, [project.id, refresh]);

  useEffect(() => {
    const controller = new AbortController();
    void listInputFiles(project.id).then((items) => {
      if (!controller.signal.aborted) setProjectInputFiles(items);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(`Project files unavailable: ${message(failure)}`);
    });
    return () => controller.abort();
  }, [project.id, refresh]);

  useEffect(() => {
    if (step !== 3) return;
    const controller = new AbortController();
    setRepositoryState(null); setRepositoryError(null);
    void Promise.all([listSourceRoots(controller.signal), discoverLocalRepositories(controller.signal), listProjectSources(project.id, controller.signal)])
      .then(([roots, local, sources]) => { if (!controller.signal.aborted) setRepositoryState({ roots, local, sources }); })
      .catch((failure: unknown) => {
        if (!(failure instanceof DOMException && failure.name === 'AbortError')) setRepositoryError(message(failure));
      });
    return () => controller.abort();
  }, [project.id, step, repositoryRefresh]);

  async function confirm(predicate: (state: State) => boolean, success: string): Promise<State> {
    const state = await readState(project.id); setSaved(state);
    if (!predicate(state)) throw new ApiError('The write returned, but saved state did not confirm it. Review the current state before retrying.', null);
    setNotice(success);
    return state;
  }
  async function perform(action: () => Promise<void>) {
    setBusy(true); setError(null); setNotice(null);
    try { await action(); }
    catch (failure) {
      try { setSaved(await readState(project.id)); } catch { /* Preserve the write error. */ }
      setError(`${message(failure)} Check saved state before retrying.`);
    } finally { setBusy(false); }
  }
  function choose(index: number) { chosen.current = true; setStep(index); setError(null); setNotice(null); }
  const readyModels = saved?.models.filter((model) => model.category === 'text' && model.available &&
    saved.accounts.some((account) => account.id === model.account_id && account.catalog_valid && (account.auth_kind === 'none' || account.credential_present)) &&
    saved.providers.some((provider) => provider.id === model.provider && provider.text_runtime_available)) ?? [];
  const selectedModel = readyModels.find((model) => model.id === modelId);
  const pending = saved?.accounts.filter((account) => account.auth_kind === 'account_runtime' && !account.credential_present) ?? [];
  const discovered = saved?.accounts.filter((account) => account.catalog_valid && account.model_count > 0) ?? [];
  const completed = saved?.runs.filter((run) => run.status === 'done' || run.status === 'failed') ?? [];
  const unresolved = saved?.runs.filter((run) => run.status === 'unknown') ?? [];
  const coreDone = saved ? coreProgress(saved) : [false, false, false, false];
  const done = saved ? [coreDone[0], coreDone[1],
    ['Researcher', 'GTM'].every((name) => saved.profiles.some((profile) => profile.name === name && profile.model && profile.model_account_id &&
      readyModels.some((model) => model.model_id === profile.model && model.account_id === profile.model_account_id))),
    Boolean(repositoryState?.sources.length), coreDone[2], coreDone[3]] : [false, false, false, false, false, false];
  const count = coreDone.filter(Boolean).length;

  return <section className="setup-page" aria-labelledby="setup-title">
    <div className="setup-intro"><p className="eyebrow">Guided project setup</p><h1 id="setup-title">Launch {project.name}</h1>
      <p>Write a brief, prepare the starter roles, and save a first assignment. You can record manual work without Codex, an AI model, or a repository. Add those connections later when you need them.</p>
      {saved && <strong className="setup-count" aria-label={`${count} of 4 core steps recorded`}>{count} <span>/ 4 core steps recorded</span></strong>}</div>
    {!saved && !error && <p className="setup-readback" role="status">Reading saved setup state…</p>}
    {error && <div className="setup-readback" role="alert"><p>{error}</p><button className="button button-outline" type="button" onClick={() => setRefresh((value) => value + 1)}>Refresh saved state</button></div>}
    {saved && <>
      <nav className="setup-path" aria-label="Setup steps">{labels.map((label, index) =>
        <button key={label} type="button" className={`setup-path-step ${step === index ? 'active' : ''} ${done[index] ? 'done' : ''}`}
          aria-current={step === index ? 'step' : undefined} onClick={() => choose(index)}><span>{done[index] ? '✓' : `0${index + 1}`}</span>{label}</button>)}</nav>
      <div className="setup-detail panel"><p className="eyebrow">Step {step + 1} of 6 · {step === 2 || step === 3 ? 'Optional' : done[step] ? 'Saved' : 'To do'}</p>
        {notice && <p className="notice" role="status">{notice}</p>}
        {step === 0 && <><h2>Set the project direction</h2><p>Describe why this project exists and what a useful result looks like. These fields provide context for your first work order.</p>
          <form onSubmit={(event: FormEvent) => { event.preventDefault(); void perform(async () => {
            const fields = { goal: goal.trim(), desired_outcome: outcome.trim(), expected_version: saved.project.version };
            const written = await updateProjectBrief(project.id, fields);
            const confirmed = await confirm((state) => state.project.version === written.version && state.project.goal === fields.goal && state.project.desired_outcome === fields.desired_outcome, 'Project brief saved and confirmed.');
            onBriefSaved?.(confirmed.project);
          }); }}>
            <label className="field"><span className="field-label">Project goal</span><textarea required maxLength={2000} rows={3} value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="What should this team achieve?" /></label>
            <label className="field"><span className="field-label">Desired outcome</span><textarea required maxLength={2000} rows={3} value={outcome} onChange={(event) => setOutcome(event.target.value)} placeholder="What result would make the work useful?" /></label>
            <button className="button button-primary" disabled={busy || !goal.trim() || !outcome.trim()} type="submit">{busy ? 'Saving…' : 'Save project brief'}</button></form>
          {done[0] && <div className="setup-saved"><strong>Saved brief</strong><p>{saved.project.goal}</p><small>Desired outcome: {saved.project.desired_outcome}</small></div>}
          <form onSubmit={(event: FormEvent) => { event.preventDefault(); void perform(async () => {
            await uploadInputFiles(project.id, projectFiles, 'project');
            const linked = await listInputFiles(project.id);
            if (!projectFiles.every((item) => linked.some((file) => file.id === item.id)))
              throw new ApiError('Saved project did not confirm every file.', null);
            setProjectInputFiles(linked); setProjectFiles([]);
            setNotice('Project files saved. Processing status appears below.');
          }); }}>
            <InputFilePicker files={projectFiles} onChange={setProjectFiles} limit={30}
              disabled={busy} label="Add project files" />
            <button className="button button-outline" type="submit" disabled={busy || !projectFiles.length}>Save project files</button>
          </form>
          {projectInputFiles.length > 0 && <div className="setup-saved"><strong>Project files</strong><ul>
            {projectInputFiles.map((item) => <li key={item.id}>{item.display_name} · {item.status}</li>)}
          </ul><button className="button button-outline" type="button" onClick={() => { void listInputFiles(project.id).then(setProjectInputFiles); }}>Refresh file status</button></div>}
          </>}
        {step === 1 && <><h2>Give the team a useful starting point</h2><p>These five role instructions can be edited in Team. Fill any missing starters, or add a custom agent profile for another specialty.</p>
          <div className="setup-role-list">{starters.map((name) => {
            const role = saved.profiles.find((profile) => profile.name === name);
            return <article key={name}><div><strong>{name}</strong><span className="status-chip status-muted">{role?.instructions.trim() ? 'Instructions saved' : 'Needs instructions'}</span></div><p>{role?.instructions.trim() || 'Starter instructions available. Fill missing instructions to save them.'}</p></article>;
          })}</div><div className="setup-actions"><button className="button button-primary" disabled={busy || done[1]} type="button" onClick={() => { void perform(async () => {
            await fillStarterProfiles(project.id);
            await confirm((state) => starters.every((name) => state.profiles.some((profile) => profile.name === name && profile.instructions.trim())), 'Starter instructions saved and confirmed.');
          }); }}>Fill missing starter instructions</button><button className="button button-outline" type="button" onClick={() => onNavigate('team')}>Open Team to customize or add</button></div></>}
        {step === 2 && <><h2>Connect a text model</h2><p>This step is optional. No model or Codex account is needed to create a project, save a work order, or record a manual planning run. The starter roles are assignment labels until you connect a model.</p>
          <p>To add AI text later, connect a provider in Models, finish sign-in if required, discover its catalog, then assign a model to each role in Team. Setup does not call a provider.</p>
          <button className="button button-primary" type="button" onClick={() => choose(4)}>Continue without AI or a repository</button>
          <div className="setup-connection"><p><strong>{saved.accounts.length} accounts saved</strong>{pending.length > 0 ? ` · ${pending.length} sign-in pending` : ''}</p><p>{discovered.length} accounts with a discovered catalog · {readyModels.length} selectable text models with a supported route</p></div>
          {pending.length > 0 && <p className="setup-attention">Finish sign-in for {pending.map((account) => account.label).join(', ')} in Models. A pending account cannot be assigned.</p>}
          {saved.accounts.length > 0 && discovered.length === 0 && <p className="setup-attention">Use Discover models on the Models page after the connection is ready.</p>}
          <button className="button button-outline" type="button" onClick={() => onNavigate('models')}>Open Models to connect or discover</button>
          {readyModels.length > 0 && <div className="setup-bind"><h3>Assign Researcher and GTM</h3><p>Select a discovered text model and choose reasoning separately for each agent. Their saved settings will be checked.</p>
            <label className="field"><span className="field-label">Available text model</span><select value={modelId} onChange={(event) => {
              const choice = readyModels.find((item) => item.id === event.target.value);
              setModelId(event.target.value);
              setSetupEfforts(Object.fromEntries(['Researcher', 'GTM'].map((name) => {
                const profile = saved.profiles.find((item) => item.name === name);
                const effort = profile && choice && profile.model === choice.model_id && profile.model_account_id === choice.account_id &&
                  choice.reasoning_efforts.includes(profile.reasoning_effort ?? '') ? profile.reasoning_effort ?? '' : '';
                return [name, effort];
              })));
            }}><option value="">Choose a model</option>{readyModels.map((model) => <option key={model.id} value={model.id}>{model.display_name} · {saved.accounts.find((account) => account.id === model.account_id)?.label}</option>)}</select></label>
            {selectedModel?.provider === 'openai_codex_runtime' && selectedModel.reasoning_efforts.length > 0 && ['Researcher', 'GTM'].map((name) =>
              <label className="field" key={name}><span className="field-label">{name} reasoning effort</span>
                <select aria-label={`${name} reasoning effort`} value={setupEfforts[name] ?? ''}
                  onChange={(event) => setSetupEfforts((current) => ({ ...current, [name]: event.target.value }))}>
                  <option value="">Use model default</option>
                  {selectedModel.reasoning_efforts.map((effort) => <option key={effort} value={effort}>{effort === 'xhigh' ? 'Extra high (xhigh)' : effort[0].toUpperCase() + effort.slice(1)}</option>)}
                </select>
              </label>)}
            <button className="button button-outline" type="button" onClick={() => onNavigate('team')}>Set up each agent in Team</button>
            <button className="button button-primary" type="button" disabled={busy || !selectedModel} onClick={() => { if (!selectedModel) return; void perform(async () => {
              for (const name of ['Researcher', 'GTM']) {
                const profile = saved.profiles.find((item) => item.name === name);
                if (!profile) throw new ApiError(`${name} profile is missing. Check Team before assigning a model.`, null);
                const effort = selectedModel.provider === 'openai_codex_runtime' &&
                  selectedModel.reasoning_efforts.includes(setupEfforts[name]) ? setupEfforts[name] : null;
                if (profile.model === selectedModel.model_id && profile.model_account_id === selectedModel.account_id &&
                  (profile.reasoning_effort ?? null) === effort) continue;
                await updateProfile(project.id, profile.id, { model: selectedModel.model_id,
                  model_account_id: selectedModel.account_id, reasoning_effort: effort });
                const current = await listProfiles(project.id);
                if (!current.some((item) => item.id === profile.id && item.model === selectedModel.model_id &&
                  item.model_account_id === selectedModel.account_id && (item.reasoning_effort ?? null) === effort))
                  throw new ApiError(`${name} model and reasoning settings could not be confirmed. Check Team before retrying.`, null);
              }
              await confirm((state) => ['Researcher', 'GTM'].every((name) => state.profiles.some((item) => item.name === name &&
                item.model === selectedModel.model_id && item.model_account_id === selectedModel.account_id &&
                (item.reasoning_effort ?? null) === (selectedModel.provider === 'openai_codex_runtime' &&
                  selectedModel.reasoning_efforts.includes(setupEfforts[name]) ? setupEfforts[name] : null))),
              'Both model and reasoning settings saved and confirmed.');
            }); }}>Assign model to Researcher and GTM</button></div>}</>}
        {step === 3 && <><h2>Select a project repository</h2><p>This step is optional for manual planning. Add a repository only when a work order needs source context or an eligible offline source check. Selection saves a branch and revision; it does not start a coding run.</p>
          <button className="button button-primary" type="button" onClick={() => choose(4)}>Continue without a repository</button>
          <p className="setup-attention">To add a checkout from another folder, open a terminal on the computer running OneFoundry. From the <code>onefoundry/</code> installation folder, run <code>python3 scripts/local_repositories.py add /absolute/path/to/repository</code>. The command mounts only that checkout read-only and restarts the local app. Refresh this page, then choose the discovered repository. The browser cannot mount host folders itself.</p>
          {!repositoryState && !repositoryError && <p role="status">Reading saved project sources…</p>}
          {repositoryError && <div className="setup-attention" role="alert"><p>{repositoryError}</p><button className="button button-outline" type="button" onClick={() => setRepositoryRefresh((value) => value + 1)}>Retry source readback</button></div>}
          {repositoryState && <><p>{repositoryState.roots.some((root) => root.available) && repositoryState.local.available
            ? `${repositoryState.local.repositories.length} local repositories discovered under the allowed root${repositoryState.local.truncated ? ' (limit reached)' : ''}.`
            : 'No local repository folder is mounted. You can continue without one. To add a local checkout, the person running OneFoundry must mount its folder read-only into the app, then refresh this page.'}</p>
            {repositoryState.local.repositories.length > 0 && <form onSubmit={(event: FormEvent) => { event.preventDefault(); void perform(async () => {
              const root = repositoryState.roots.find((item) => item.available);
              if (!root) throw new ApiError('The local repository root is unavailable.', null);
              const written = await addLocalSource(project.id, root.id, sourceChoice);
              const sources = await listProjectSources(project.id);
              if (!sources.some((item) => item.id === written.id && item.revision === written.revision)) throw new ApiError('The selected repository was not confirmed in saved state.', null);
              setRepositoryState({ ...repositoryState, sources }); setNotice('Project repository saved and confirmed.');
            }); }}>
              <label className="field"><span className="field-label">Local repository</span><select value={sourceChoice} onChange={(event) => setSourceChoice(event.target.value)} required>
                <option value="">Choose a repository</option>{repositoryState.local.repositories.map((candidate) => <option key={candidate.relative_path} value={candidate.relative_path}>{candidate.display_name} · {candidate.branch}{candidate.dirty ? ' · uncommitted changes' : ''}</option>)}</select></label>
              <button className="button button-primary" type="submit" disabled={busy || !sourceChoice}>Select local repository</button>
            </form>}
            {repositoryState.sources.length > 0 ? <div className="setup-saved"><strong>{repositoryState.sources.length} repositories selected</strong>
              <ul>{repositoryState.sources.map((source) => <li key={source.id}>{source.display_name} · {source.branch} · {source.availability === 'unavailable' ? 'Unavailable' : 'Verified at selection'}</li>)}</ul></div>
              : <p className="setup-attention">No repository is saved for this project yet.</p>}
            <div className="setup-actions"><button className="button button-primary" type="button" onClick={() => onNavigate('integrations')}>Open repositories to select</button>
              <button className="button button-outline" type="button" onClick={() => setRepositoryRefresh((value) => value + 1)}>Refresh selected repositories</button></div></>}
        </>}
        {step === 4 && <><h2>Write the first work order</h2><p>Use the project brief to give one role a concrete assignment. Saving creates a draft; it does not call a provider or start a run.</p>
          {done[0] && <div className="setup-saved"><strong>Project context</strong><p>{saved.project.goal}</p><small>Desired outcome: {saved.project.desired_outcome}</small><p><button className="button button-outline" type="button" onClick={() => {
            if (!title.trim()) setTitle(saved.project.goal.slice(0, 160));
            setInstructions((current) => current.trim() ? current : `Project goal: ${saved.project.goal}\nDesired outcome: ${saved.project.desired_outcome}\n\nTask: `);
          }}>Use project brief in draft</button></p></div>}
          <form onSubmit={(event: FormEvent) => { event.preventDefault(); void perform(async () => {
            const context = done[0] ? `Project goal: ${saved.project.goal}\nDesired outcome: ${saved.project.desired_outcome}` : '';
            const task = instructions.trim();
            const workInstructions = context && !task.startsWith(context) ? `${context}\n\nTask: ${task}` : task;
            const created = createdOrderId
              ? (await listWorkOrders(project.id)).find((order) => order.id === createdOrderId)
              : await createWorkOrder(project.id, { title: title.trim(), instructions: workInstructions, profile_id: profileId });
            if (!created) throw new ApiError('Saved work order unavailable. Refresh before retrying files.', null);
            setCreatedOrderId(created.id);
            await confirm((state) => state.orders.some((order) => order.id === created.id && order.status === 'draft' && order.title === title.trim() && order.instructions === workInstructions && order.profile_id === profileId), 'Draft work order saved and confirmed.');
            if (workFiles.length) {
              await uploadInputFiles(project.id, workFiles, 'work_order', created.id);
              const linked = await listInputFiles(project.id, created.id);
              if (!workFiles.every((item) => linked.some((file) => file.id === item.id)))
                throw new ApiError('Saved work order did not confirm every file.', null);
            }
            setTitle(''); setInstructions(''); setProfileId(''); setWorkFiles([]); setCreatedOrderId(null);
          }); }}>
            <label className="field"><span className="field-label">Title</span><input required maxLength={160} value={title} disabled={createdOrderId !== null} onChange={(event) => setTitle(event.target.value)} placeholder="What should this role deliver?" /></label>
            <label className="field"><span className="field-label">Instructions</span><textarea required maxLength={16000} rows={5} value={instructions} disabled={createdOrderId !== null} onChange={(event) => setInstructions(event.target.value)} placeholder="Scope, expected result, and boundaries" /></label>
            <label className="field"><span className="field-label">Assign to role</span><select required value={profileId} disabled={createdOrderId !== null} onChange={(event) => setProfileId(event.target.value)}><option value="">Choose a role</option>{saved.profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</select></label>
            <InputFilePicker files={workFiles} onChange={setWorkFiles} limit={8}
              disabled={busy || createdOrderId !== null} label="First work order files" />
            {createdOrderId && <p role="status">Draft saved. Retry the selected files or add them later in Work orders.</p>}
            <button className="button button-primary" type="submit" disabled={busy || !title.trim() || !instructions.trim() || !profileId}>{busy ? 'Saving…' : createdOrderId ? 'Retry files' : 'Save draft work order'}</button></form>
          {saved.orders.length > 0 && <div className="setup-saved"><strong>{saved.orders.length} work orders saved</strong><p>Open Work orders to review the draft and choose how to proceed.</p><button className="button button-outline" type="button" onClick={() => onNavigate('work')}>Open Work orders</button></div>}</>}
        {step === 5 && <><h2>Inspect a saved outcome</h2><p>No model or repository is needed for manual planning. In Work orders, mark the draft ready and start a manual run. Then open Activity, record its outcome and reason, and return here to inspect the saved timeline. Setup does not start a run.</p>
          {completed.length > 0 ? <div className="setup-outcomes">{completed.map((run) => <button key={run.id} className="setup-outcome" type="button" onClick={() => onNavigate('activity', run.id)}><strong>{run.work_order_title || 'Work order'}</strong><span>{run.status} · {run.mode}</span><small>{run.outcome_reason || 'Open saved timeline'}</small></button>)}</div>
            : <p className="setup-attention">{unresolved.length > 0 ? 'No confirmed outcome yet. An unknown run needs review in Activity.' : saved.runs.length > 0 ? 'A run is still in progress. Open Activity to follow it.' : 'No saved run outcome yet. Your draft remains safe until you choose an action in Work orders.'}</p>}
          {unresolved.length > 0 && <div className="setup-outcomes"><h3>Unresolved runs</h3>{unresolved.map((run) => <button key={run.id} className="setup-outcome" type="button" onClick={() => onNavigate('activity', run.id)}><strong>{run.work_order_title || 'Work order'}</strong><span>unknown · needs review</span><small>Open Activity to reconcile this run before treating it as an outcome.</small></button>)}</div>}
          <div className="setup-actions"><button className="button button-outline" type="button" onClick={() => onNavigate('work')}>Open Work orders</button><button className="button button-outline" type="button" onClick={() => onNavigate('activity')}>Open Activity</button></div></>}
      </div>
      <div className="setup-footer"><button className="button button-outline" type="button" disabled={step === 0} onClick={() => choose(step - 1)}>Back</button><button className="button button-primary" type="button" disabled={step === 5} onClick={() => choose(step + 1)}>Next</button><button className="setup-refresh" type="button" onClick={() => setRefresh((value) => value + 1)}>Refresh saved progress</button></div>
    </>}
  </section>;
}
