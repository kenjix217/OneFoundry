import { ProfileCapabilities } from './ProfileCapabilities';
import { useEffect, useState, type FormEvent } from 'react';

import { ApiError, type AutonomyLevel, type MemoryScope, type NewProfile, type Profile, type ProfileFields } from './api';
import { listCatalogModels, listModelAccounts, refreshModelAccount, type CatalogModel, type ModelAccount } from './modelApi';
import { ProfileSkills } from './ProfileSkills';
import { isRootProfile } from './OrganizationChart';

type EditorProps = {
  projectId: string;
  profile: Profile | null;
  profiles?: Profile[];
  saving: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (fields: NewProfile | ProfileFields) => Promise<void>;
  onSkillsChanged?: () => void;
};

const MAX_PROFILE_INSTRUCTIONS_CHARS = 50_000;

export function ProfileEditor({ projectId, profile, profiles = [], saving, error, onClose, onSave, onSkillsChanged }: EditorProps) {
  const [name, setName] = useState(profile?.name ?? '');
  const [modelChoice, setModelChoice] = useState(profile?.model ? 'legacy' : '');
  const [reasoningEffort, setReasoningEffort] = useState(profile?.reasoning_effort ?? '');
  const [models, setModels] = useState<CatalogModel[]>([]);
  const [accounts, setAccounts] = useState<ModelAccount[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogNotice, setCatalogNotice] = useState<string | null>(null);
  const [refreshingAccountId, setRefreshingAccountId] = useState<string | null>(null);
  const [instructions, setInstructions] = useState(profile?.instructions ?? '');
  const instructionCount = [...instructions].length;
  const instructionsTooLong = instructionCount > MAX_PROFILE_INSTRUCTIONS_CHARS;
  const [memoryScope, setMemoryScope] = useState<MemoryScope>(profile?.memory_scope ?? 'project');
  const [allowedTools, setAllowedTools] = useState<string[]>(profile?.allowed_tools ?? []);
  const [autonomyLevel, setAutonomyLevel] = useState<AutonomyLevel>(profile?.autonomy_level ?? 'manual');
  const isChief = profile !== null && isRootProfile(profile);
  const chief = profiles.find(isRootProfile);
  const profileById = new Map(profiles.map((item) => [item.id, item]));
  function validManager(candidate: Profile): boolean {
    if (!profile) return true;
    const seen = new Set<string>();
    let current: Profile | undefined = candidate;
    while (current && !seen.has(current.id)) {
      if (current.id === profile.id) return false;
      seen.add(current.id);
      current = current.reports_to_profile_id ? profileById.get(current.reports_to_profile_id) : undefined;
    }
    return true;
  }
  const managers = profiles.filter(validManager).sort((a, b) => a.name.localeCompare(b.name));
  const [reportsTo, setReportsTo] = useState(profile?.reports_to_profile_id ?? chief?.id ?? managers[0]?.id ?? '');
  const selectedModel = models.find((item) => item.id === modelChoice);
  const reasoningOptions = selectedModel?.provider === 'openai_codex_runtime' ? selectedModel.reasoning_efforts : [];
  const sameSavedModel = !!profile && (modelChoice === 'legacy' ||
    (selectedModel?.account_id === profile.model_account_id && selectedModel?.model_id === profile.model));
  const savedUnadvertisedEffort = sameSavedModel && profile?.reasoning_effort &&
    !reasoningOptions.includes(profile.reasoning_effort) ? profile.reasoning_effort : null;

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([listCatalogModels(projectId, controller.signal), listModelAccounts(projectId, controller.signal)])
      .then(([catalog, linked]) => {
        setModels(catalog);
        setAccounts(linked);
        const matching = catalog.find((item) => item.account_id === profile?.model_account_id && item.model_id === profile?.model);
        if (matching) setModelChoice(matching.id);
      }).catch((failure: unknown) => {
        if (failure instanceof DOMException && failure.name === 'AbortError') return;
        setCatalogError('Model choices are unavailable. Check the Settings → AI connections and reopen this profile.');
      });
    return () => controller.abort();
  }, [projectId, profile]);

  async function refreshModels(account: ModelAccount) {
    const previous = models.find((item) => item.id === modelChoice);
    setRefreshingAccountId(account.id);
    setCatalogError(null);
    setCatalogNotice(null);
    try {
      const refreshed = await refreshModelAccount(projectId, account.id);
      setAccounts((current) => current.map((item) => item.id === account.id ? refreshed.account : item));
      setModels((current) => [
        ...current.filter((item) => item.account_id !== account.id), ...refreshed.models,
      ]);
      const selectedSlug = previous?.account_id === account.id ? previous.model_id
        : modelChoice === 'legacy' && profile?.model_account_id === account.id ? profile.model : null;
      let selectionMissing = false;
      if (selectedSlug) {
        const replacement = refreshed.models.find((item) => item.model_id === selectedSlug);
        const savedMatch = profile?.model_account_id === account.id && profile.model === selectedSlug;
        selectionMissing = !replacement?.available;
        setModelChoice(replacement && (replacement.available || savedMatch) ? replacement.id : savedMatch ? 'legacy' : '');
      }
      setCatalogNotice(`${account.label} models refreshed: ${refreshed.models.length} returned by the provider.${selectionMissing ? ' The previous selection cannot currently be used.' : ''}`);
    } catch (failure) {
      const message = failure instanceof ApiError ? failure.message : 'Model refresh could not be confirmed.';
      setModels((current) => current.map((item) => item.account_id === account.id ? { ...item, available: false } : item));
      try {
        const [savedModels, savedAccounts] = await Promise.all([
          listCatalogModels(projectId), listModelAccounts(projectId),
        ]);
        setModels(savedModels);
        setAccounts(savedAccounts);
        if (previous) {
          const matching = savedModels.find((item) => item.account_id === previous.account_id && item.model_id === previous.model_id);
          const savedMatch = profile?.model_account_id === previous.account_id && profile.model === previous.model_id;
          setModelChoice(matching && (matching.available || savedMatch) ? matching.id : savedMatch ? 'legacy' : '');
        }
        setCatalogError(`${message} Saved catalog state was reloaded; check its current availability.`);
      } catch {
        setCatalogError(`${message} Saved catalog state could not be confirmed; this account's choices are disabled until the editor is reopened.`);
      }
    } finally {
      setRefreshingAccountId(null);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (instructionsTooLong) return;
    const selected = models.find((item) => item.id === modelChoice &&
      (item.available || (item.account_id === profile?.model_account_id && item.model_id === profile?.model)));
    const fields: ProfileFields = {
      model: selected?.model_id ?? (modelChoice === 'legacy' ? profile?.model ?? null : null),
      model_account_id: selected?.account_id ?? (modelChoice === 'legacy' ? profile?.model_account_id ?? null : null),
      reasoning_effort: reasoningOptions.includes(reasoningEffort) ||
        (savedUnadvertisedEffort === reasoningEffort && reasoningEffort !== '') ? reasoningEffort : null,
      instructions: instructions.trim(),
      memory_scope: memoryScope,
      allowed_tools: [...new Set(allowedTools)],
      autonomy_level: autonomyLevel,
      ...(isChief ? {} : { reports_to_profile_id: reportsTo }),
    };
    await onSave(profile ? fields : { ...fields, name: name.trim() });
  }

  return (
    <div className="editor-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="editor" aria-label={profile ? `Edit ${profile.name}` : 'Add custom profile'}>
        <div className="editor-topline" />
        <header className="editor-header">
          <div>
            <p className="eyebrow">Profile settings</p>
            <h2>{profile ? `Edit ${profile.name}` : 'Add a custom profile'}</h2>
            <p>Save a role’s model and working boundaries. Model catalog availability does not prove runtime availability.</p>
          </div>
          <button className="icon-button" type="button" aria-label="Close profile form" onClick={onClose}>×</button>
        </header>
        <form className="editor-form" onSubmit={(event) => { void submit(event); }}>
          {profile ? (
            <div className="field">
              <span className="field-label">Profile</span>
              <div className="readonly-field">{profile.name} <span>· {profile.kind === 'builtin' ? 'Built-in' : 'Custom'}</span></div>
            </div>
          ) : (
            <label className="field">
              <span className="field-label">Profile name</span>
              <input autoFocus required maxLength={80} value={name} onChange={(event) => setName(event.target.value)}
                placeholder="e.g. Product strategist" />
            </label>
          )}
          {isChief && <div className="field"><span className="field-label">Reports to</span>
            <div className="readonly-field">You · company operator</div></div>}
          {!isChief && <label className="field">
            <span className="field-label">Reports to</span>
            <select aria-label="Reports to" required value={reportsTo} onChange={(event) => setReportsTo(event.target.value)}>
              <option value="" disabled>Choose a manager</option>
              {managers.map((item) =>
                <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
            <span className="field-hint">This organizes the chart. It does not let this profile delegate, approve, or use tools.</span>
          </label>}
          <label className="field">
            <span className="field-label">Model</span>
            <select aria-label="Model" value={modelChoice} disabled={refreshingAccountId !== null} onChange={(event) => { setModelChoice(event.target.value); setReasoningEffort(''); }}>
              <option value="">No model yet</option>
              {modelChoice === 'legacy' && profile?.model && <option value="legacy">Previously saved: {profile.model} (connection unverified)</option>}
              {accounts.map((account) => {
                const choices = models.filter((item) => item.account_id === account.id);
                return choices.length > 0 ? <optgroup key={account.id} label={`${account.label} · ${account.provider}`}>
                  {choices.map((item) => <option key={item.id} value={item.id} disabled={!item.available}>{item.display_name} · {item.category}{item.available ? '' : ' · unavailable'}</option>)}
                </optgroup> : null;
              })}
            </select>
            <span className="field-hint">Choose a discovered model. Check Settings → AI connections for current text runtime availability.</span>
          </label>
          {accounts.length > 0 && <div className="model-refresh-actions">
            {accounts.map((account) => <button key={account.id} className="button button-outline" type="button"
              disabled={saving || refreshingAccountId !== null || (account.auth_kind !== 'none' && !account.credential_present)}
              onClick={() => { void refreshModels(account); }}>
              {refreshingAccountId === account.id ? `Refreshing ${account.label}…` : `Refresh ${account.label} models`}
            </button>)}
          </div>}
          {catalogNotice && <p className="field-hint" role="status">{catalogNotice}</p>}
          {catalogError && <p className="field-hint" role="alert">{catalogError}</p>}
          {selectedModel?.provider === 'openai_codex_runtime' && <label className="field">
            <span className="field-label">Reasoning effort</span>
            <select aria-label="Reasoning effort" value={reasoningOptions.includes(reasoningEffort) || savedUnadvertisedEffort === reasoningEffort ? reasoningEffort : ''}
              onChange={(event) => setReasoningEffort(event.target.value)} disabled={reasoningOptions.length === 0 && !savedUnadvertisedEffort}>
              <option value="">Use model default</option>
              {savedUnadvertisedEffort && <option value={savedUnadvertisedEffort} disabled>Previously saved: {savedUnadvertisedEffort} · no longer advertised</option>}
              {reasoningOptions.map((effort) => <option key={effort} value={effort}>{effort === 'xhigh' ? 'Extra high (xhigh)' : effort[0].toUpperCase() + effort.slice(1)}</option>)}
            </select>
            <span className="field-hint">Choices come from this Codex account’s discovered model catalog.</span>
          </label>}
          <label className="field">
            <span className="field-label">Instructions</span>
            <textarea value={instructions} required={!profile} rows={10} aria-invalid={instructionsTooLong}
              onChange={(event) => setInstructions(event.target.value)}
              placeholder="Describe the role’s responsibilities and working style" />
            <span className="field-hint">{instructionCount.toLocaleString('en-US')} / 50,000 characters. Text runs also have a configured prompt byte limit.</span>
            {instructionsTooLong && <span className="form-error" role="alert">Instructions exceed 50,000 characters. Shorten them before saving.</span>}
          </label>
          <div className="field-pair">
            <label className="field">
              <span className="field-label">Memory scope</span>
              <select aria-label="Memory scope" aria-describedby="memory-scope-hint" value={memoryScope} onChange={(event) => setMemoryScope(event.target.value as MemoryScope)}>
                <option value="project">This project</option>
                <option value="company">Company + this project</option>
              </select>
              <span className="field-hint" id="memory-scope-hint">Company includes company memory across projects and this project's project memory.</span>
            </label>
            <label className="field">
              <span className="field-label">Autonomy level</span>
              <select value={autonomyLevel} onChange={(event) => setAutonomyLevel(event.target.value as AutonomyLevel)}>
                <option value="manual">Manual</option>
                <option value="supervised">Supervised</option>
                <option value="bounded">Bounded</option>
              </select>
            </label>
          </div>
          <ProfileCapabilities projectId={projectId} selected={allowedTools} disabled={saving} onChange={setAllowedTools} />
          <div className="editor-note"><span className="note-spark">✳</span><p>Profile settings do not grant tool access. Connect a provider and discover models on the Settings → AI connections.</p></div>
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="editor-actions">
            <button className="button button-quiet" type="button" onClick={onClose} disabled={saving}>Cancel</button>
            <button className="button button-primary" type="submit" disabled={saving || refreshingAccountId !== null || instructionsTooLong || (!profile && !isChief && !reportsTo)}>{saving ? 'Saving…' : 'Save profile'}</button>
          </div>
        </form>
        {profile ? <ProfileSkills projectId={projectId} profileId={profile.id} onChanged={onSkillsChanged ?? (() => {})} /> :
          <p className="editor-skill-hint">Save the new profile, then reopen it to pin any SKILL.md versions you want selected for future runs. Live text work can also match project skills automatically.</p>}
      </section>
    </div>
  );
}
