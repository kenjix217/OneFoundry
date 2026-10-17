import { useEffect, useState, type FormEvent } from 'react';

import { ApiError } from './api';
import { createSkill, createSkillVersion, listSkills, listSkillVersions, type Skill, type SkillVersion } from './skillsApi';

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : 'The result is unknown. Refresh the library before retrying.';
}

export function SkillLibrary({ projectId }: { projectId: string }) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [versions, setVersions] = useState<SkillVersion[]>([]);
  const [loading, setLoading] = useState(true);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [versionError, setVersionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [content, setContent] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void listSkills(projectId, controller.signal).then((items) => {
      setSkills(items);
      setSelected((current) => current && items.some((item) => item.id === current) ? current : items[0]?.id ?? null);
      setLoading(false);
    }).catch((failure: unknown) => {
      if (failure instanceof DOMException && failure.name === 'AbortError') return;
      setError(message(failure)); setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, refresh]);

  useEffect(() => {
    if (!selected) { setVersions([]); return; }
    const controller = new AbortController();
    setVersionsLoading(true);
    setError(null);
    setVersions([]);
    void listSkillVersions(projectId, selected, controller.signal).then((items) => {
      setVersions(items.sort((a, b) => b.version - a.version));
      setVersionsLoading(false);
    }).catch((failure: unknown) => {
      if (failure instanceof DOMException && failure.name === 'AbortError') return;
      setError(message(failure)); setVersionsLoading(false);
    });
    return () => controller.abort();
  }, [projectId, selected, refresh]);

  async function addSkill(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true); setCreateError(null); setNotice(null);
    try {
      const created = await createSkill(projectId, name.trim(), description.trim());
      const readback = await listSkills(projectId);
      if (!readback.some((item) => item.id === created.id)) throw new ApiError('Skill creation returned, but library readback did not find it. Refresh before retrying.', null);
      setSkills(readback); setSelected(created.id); setName(''); setDescription('');
      setNotice('Skill created. Add its first SKILL.md version below.');
    } catch (failure) { setCreateError(message(failure)); }
    finally { setSaving(false); }
  }

  async function addVersion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    setSaving(true); setVersionError(null); setNotice(null);
    try {
      const created = await createSkillVersion(projectId, selected, content);
      const [readback, items] = await Promise.all([listSkillVersions(projectId, selected), listSkills(projectId)]);
      if (!readback.some((item) => item.id === created.id && item.sha256 === created.sha256)) {
        throw new ApiError('Version creation returned, but immutable version readback did not match. Refresh before retrying.', null);
      }
      setVersions(readback.sort((a, b) => b.version - a.version)); setSkills(items); setContent('');
      setNotice(`Version ${created.version} saved. Existing profile bindings keep their exact versions.`);
    } catch (failure) { setVersionError(message(failure)); }
    finally { setSaving(false); }
  }

  const active = skills.find((item) => item.id === selected);
  return <div className="skill-page">
    <header className="view-heading"><div><p className="eyebrow">Project procedures</p><h1>Procedures</h1>
      <p>Write reusable SKILL.md procedures and keep each version intact. Live text work can automatically match relevant project skills. Profile pins save an exact version for future runs; procedures never grant tools or permissions.</p></div>
      <button type="button" className="button button-outline" onClick={() => setRefresh((value) => value + 1)}>Refresh library</button></header>
    {notice && <p className="notice" role="status">{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    <div className="skill-layout">
      <section className="models-section skill-rail" aria-label="Project skills">
        <h2>Procedures</h2>
        {loading ? <p>Loading library…</p> : skills.length === 0 ? <p>No procedures yet. Create one to start a versioned SKILL.md.</p> :
          <div className="skill-rail-list">{skills.map((item) => <button key={item.id} type="button" aria-current={selected === item.id ? 'true' : undefined}
            className={selected === item.id ? 'active' : ''} onClick={() => { setSelected(item.id); setVersionError(null); setNotice(null); }}>
            <strong>{item.name}</strong><span>{item.latest_version === null ? 'No versions' : `Latest v${item.latest_version}`}</span></button>)}</div>}
        <form className="skill-create" onSubmit={(event) => { void addSkill(event); }}>
          <h3>Create procedure</h3>
          <label className="field"><span className="field-label">Name</span><input required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Release review" /></label>
          <label className="field"><span className="field-label">Description</span><textarea maxLength={500} rows={2} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="When this procedure helps" /></label>
          {createError && <p className="form-error" role="alert">{createError}</p>}
          <button className="button button-primary" type="submit" disabled={saving}>{saving ? 'Saving…' : 'Create procedure'}</button>
        </form>
      </section>
      <section className="models-section skill-detail" aria-label="Skill versions">
        {!active ? <div className="empty-inline">Select a procedure to view its versions.</div> : <>
          <p className="eyebrow">Saved procedure</p><h2>{active.name}</h2><p>{active.description || 'No description provided.'}</p>
          <div className="scope-note"><span aria-hidden="true">◇</span><p><strong>Procedure boundary.</strong> A SKILL.md gives instructions. Profile allowed tool IDs and Tool Gateway admission remain separate.</p></div>
          <form className="skill-version-form" onSubmit={(event) => { void addVersion(event); }}>
            <label className="field"><span className="field-label">New SKILL.md version</span>
              <textarea required maxLength={16000} rows={10} value={content} onChange={(event) => setContent(event.target.value)} placeholder={'# Procedure\n\nDescribe the steps and when to use them.'} />
              <span className="field-hint">Each save creates a new version. Earlier versions stay unchanged.</span></label>
            {versionError && <p className="form-error" role="alert">{versionError}</p>}
            <button className="button button-primary" type="submit" disabled={saving}>{saving ? 'Saving…' : 'Add immutable version'}</button>
          </form>
          <h3>Version history</h3>
          {versionsLoading ? <p>Loading versions…</p> : versions.length === 0 ? <p>No versions yet. Add the first SKILL.md above.</p> :
            <div className="skill-versions">{versions.map((item) => <details key={item.id} className="skill-version">
              <summary><strong>Version {item.version}</strong><span>{new Date(item.created_at).toLocaleString()}</span></summary>
              <pre>{item.content}</pre>
            </details>)}</div>}
        </>}
      </section>
    </div>
  </div>;
}
