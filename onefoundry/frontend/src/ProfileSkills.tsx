import { useEffect, useState } from 'react';

import { ApiError } from './api';
import { bindProfileSkill, listProfileSkills, listSkills, listSkillVersions, removeProfileSkill,
  type ProfileSkill, type Skill, type SkillVersion } from './skillsApi';

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : 'The saved state is unknown. Refresh before retrying.';
}

export function ProfileSkills({ projectId, profileId, onChanged }: {
  projectId: string; profileId: string; onChanged: () => void;
}) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [bindings, setBindings] = useState<ProfileSkill[]>([]);
  const [versions, setVersions] = useState<SkillVersion[]>([]);
  const [skillId, setSkillId] = useState('');
  const [version, setVersion] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void Promise.all([listSkills(projectId, controller.signal), listProfileSkills(projectId, profileId, controller.signal)])
      .then(([library, selected]) => { setSkills(library); setBindings(selected); setLoading(false); })
      .catch((failure: unknown) => {
        if (failure instanceof DOMException && failure.name === 'AbortError') return;
        setError(message(failure)); setLoading(false);
      });
    return () => controller.abort();
  }, [projectId, profileId, refresh]);

  useEffect(() => {
    setVersion(''); setVersions([]);
    if (!skillId) return;
    const controller = new AbortController();
    void listSkillVersions(projectId, skillId, controller.signal).then((items) => setVersions(items.sort((a, b) => b.version - a.version)))
      .catch((failure: unknown) => {
        if (failure instanceof DOMException && failure.name === 'AbortError') return;
        setError(message(failure));
      });
    return () => controller.abort();
  }, [projectId, skillId]);

  async function bind() {
    if (!skillId || !version) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      await bindProfileSkill(projectId, profileId, skillId, Number(version), bindings.find((item) => item.skill_id === skillId)?.id ?? null);
      const readback = await listProfileSkills(projectId, profileId);
      if (!readback.some((item) => item.skill_id === skillId && item.version === Number(version)
        && item.id !== bindings.find((binding) => binding.skill_id === skillId)?.id)) {
        throw new ApiError('Binding returned, but profile readback did not match. Refresh before retrying.', null);
      }
      setBindings(readback); onChanged(); setNotice('Exact procedure version pinned for future runs.');
    } catch (failure) {
      setError(message(failure));
      if (failure instanceof ApiError && failure.status === 409) setRefresh((value) => value + 1);
    }
    finally { setBusy(false); }
  }

  async function remove(skill: ProfileSkill) {
    setBusy(true); setError(null); setNotice(null);
    try {
      await removeProfileSkill(projectId, profileId, skill.skill_id, skill.id);
      const readback = await listProfileSkills(projectId, profileId);
      if (readback.some((item) => item.skill_id === skill.skill_id)) {
        throw new ApiError('Removal returned, but the procedure remains bound. Refresh before retrying.', null);
      }
      setBindings(readback); onChanged(); setNotice('Pin removed. The procedure remains in the project library for live text skill matching.');
    } catch (failure) {
      setError(message(failure));
      if (failure instanceof ApiError && failure.status === 409) setRefresh((value) => value + 1);
    }
    finally { setBusy(false); }
  }

  return <section className="profile-skills-editor" aria-label="Pinned procedures">
    <div className="profile-skills-heading"><div><span className="field-label">Pinned procedures</span>
      <p className="field-hint">Live text work can automatically match project skills. Pin a procedure here to save its exact SKILL.md version with this profile’s future runs. New library versions do not change a pin.</p></div>
      <button type="button" className="button button-quiet" onClick={() => { setError(null); setRefresh((value) => value + 1); }} disabled={busy}>Refresh</button></div>
    {loading ? <p className="field-hint">Loading procedures…</p> : <>
      {bindings.length === 0 ? <p className="field-hint">No pins for this profile. Project skills remain available for live text matching.</p> :
        <ul className="profile-bound-skills">{bindings.map((item) => <li key={item.skill_id}>
          <div><strong>{item.skill_name} · v{item.version}</strong><small>Saved version</small></div>
          <button type="button" onClick={() => { void remove(item); }} disabled={busy}>Remove</button>
        </li>)}</ul>}
      <div className="profile-skill-picker">
        <label className="field"><span className="field-label">Library procedure</span><select aria-label="Library procedure" value={skillId} onChange={(event) => setSkillId(event.target.value)}>
          <option value="">Choose a procedure</option>{skills.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
        <label className="field"><span className="field-label">Exact version</span><select aria-label="Exact version" value={version} onChange={(event) => setVersion(event.target.value)} disabled={!skillId}>
          <option value="">Choose a version</option>{versions.map((item) => <option key={item.id} value={item.version}>Version {item.version}</option>)}
        </select></label>
      </div>
      {skills.length === 0 && <p className="field-hint">Create a SKILL.md in Knowledge → Procedures first.</p>}
      {skillId && versions.length === 0 && <p className="field-hint">This procedure has no saved version yet.</p>}
      <button type="button" className="button button-outline" disabled={busy || !version} onClick={() => { void bind(); }}>Pin version</button>
    </>}
    {notice && <p className="field-hint" role="status">{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
  </section>;
}
