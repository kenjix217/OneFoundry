import { useState } from 'react';
import type { Profile } from './api';
import type { ProfileSkill } from './skillsApi';
import { OneFoundryCapabilityStatus } from './OneFoundryCapabilityStatus';
import { OrganizationChart } from './OrganizationChart';
function initials(name: string): string { return name.split(/\s+/).slice(0, 2).map((part) => part[0] ?? '').join('').toUpperCase(); }
function profileStatus(profile: Profile): string {
  if (!profile.model) return 'Model unconfigured';
  return profile.configured ? 'Profile ready' : 'Setup incomplete';
}

function ProfileCard({ profile, skills, onEdit }: { profile: Profile; skills: ProfileSkill[] | undefined; onEdit: (profile: Profile) => void }) {
  return (
    <article className="profile-card">
      <div className="profile-card-head">
        <span className={`avatar avatar-${profile.kind}`} aria-hidden="true">{initials(profile.name)}</span>
        <span className="profile-kind">{profile.kind === 'builtin' ? 'Built-in profile' : 'Custom profile'}</span>
      </div>
      <h3>{profile.name}</h3>
      <p className="profile-subtitle">{profile.instructions.trim() || 'Add instructions to define this role.'}</p>
      <div className="profile-meta">
        <span className={`status-chip ${profile.model ? 'status-soft' : 'status-muted'}`}><span className="status-dot" />{profileStatus(profile)}</span>
        <span className="profile-model">{profile.model || 'No model selected'}</span>
      </div>
      <div className="profile-facts">
        <div><span>Memory</span><strong>{profile.memory_scope === 'company' ? 'Company + project' : 'This project'}</strong></div>
        <div><span>Autonomy</span><strong>{profile.autonomy_level}</strong></div>
        <div><span>Capabilities</span><strong>{profile.allowed_tools.length} · {profile.permissions_active ? 'active' : 'inactive'}</strong></div>
      </div>
      <p className="profile-skills">Procedures: {skills === undefined ? 'Unavailable' : skills.length === 0 ? 'None · live text can match project skills' : skills.map((item) => `${item.skill_name} v${item.version}`).join(', ')}</p>
      <details className="agent-capability-details"><summary>Capability status</summary><OneFoundryCapabilityStatus projectId={profile.project_id} profileId={profile.id} /></details>
      <button className="card-action" type="button" onClick={() => onEdit(profile)}>
        Edit profile <span aria-hidden="true">↗</span>
      </button>
    </article>
  );
}


export function TeamView({ profiles, skills, onEdit, onAdd }: { profiles: Profile[]; skills: Record<string, ProfileSkill[]>; onEdit: (profile: Profile) => void; onAdd: () => void }) {
  const [view, setView] = useState<'agents' | 'structure'>('agents');
  const [search, setSearch] = useState('');
  const shown = profiles.filter((profile) => `${profile.name} ${profile.instructions}`.toLowerCase().includes(search.toLowerCase()));
  return <section className="team-workspace"><header className="view-heading"><div><p className="eyebrow">People-shaped roles. Purposeful work.</p><h1>Your team</h1><p>Give each agent a clear role, the right tools, and room to do useful work.</p></div><button className="button button-primary" type="button" onClick={onAdd}>Add a profile <span aria-hidden="true">+</span></button></header>
    <div className="team-toolbar"><nav className="workspace-tabs" aria-label="Team views"><button type="button" aria-current={view === 'agents' ? 'page' : undefined} onClick={() => setView('agents')}>Agents <span>{profiles.length}</span></button><button type="button" aria-current={view === 'structure' ? 'page' : undefined} onClick={() => setView('structure')}>Team structure</button></nav><label className="workspace-search"><span className="sr-only">Search agents</span><input type="search" placeholder="Find an agent…" value={search} onChange={(event) => setSearch(event.target.value)} /></label></div>
    <div className="team-scroll" tabIndex={0}>{view === 'agents' ? <><div className="profile-grid">{shown.map((profile) => <ProfileCard key={profile.id} profile={profile} skills={skills[profile.id]} onEdit={onEdit} />)}</div>{shown.length === 0 && <p className="empty-inline">No agents match this search.</p>}</> : <OrganizationChart profiles={profiles} onEdit={onEdit} />}</div>
  </section>;
}
