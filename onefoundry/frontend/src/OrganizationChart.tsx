import type { ReactNode } from 'react';
import type { Profile } from './api';

type Props = { profiles: Profile[]; onEdit: (profile: Profile) => void };

export function isRootProfile(profile: Profile): boolean {
  return profile.reports_to_profile_id === null;
}

function profileSort(left: Profile, right: Profile): number {
  if (isRootProfile(left)) return -1;
  if (isRootProfile(right)) return 1;
  return left.name.localeCompare(right.name);
}

export function OrganizationChart({ profiles, onEdit }: Props) {
  const byId = new Map(profiles.map((profile) => [profile.id, profile]));
  const children = new Map<string | null, Profile[]>();
  for (const profile of profiles) {
    const parent = profile.reports_to_profile_id;
    const key = parent && byId.has(parent) && parent !== profile.id ? parent : null;
    children.set(key, [...(children.get(key) ?? []), profile]);
  }

  function branch(profile: Profile, ancestors: Set<string>): ReactNode {
    if (ancestors.has(profile.id)) return null;
    const next = new Set(ancestors).add(profile.id);
    const reports = (children.get(profile.id) ?? []).sort(profileSort).filter((item) => !next.has(item.id));
    return <li className="org-branch" key={profile.id}>
      <div className={`org-node ${isRootProfile(profile) ? 'org-chief' : ''}`}>
        <span className="org-node-kind">{isRootProfile(profile) ? 'Coordinator' : profile.kind === 'builtin' ? 'Specialist' : 'Custom profile'}</span>
        <strong>{profile.name}</strong>
        <button type="button" onClick={() => onEdit(profile)} aria-label={`Edit ${profile.name} reporting and profile settings`}>Edit profile</button>
      </div>
      {reports.length > 0 && <ul className="org-children">{reports.map((item) => branch(item, next))}</ul>}
    </li>;
  }

  const roots = (children.get(null) ?? []).sort(profileSort);
  return <section className="organization-chart" aria-labelledby="organization-heading">
    <div className="section-heading"><div><p className="eyebrow">Reporting lines</p><h2 id="organization-heading">Company organization</h2>
      <p>Who reports to whom in this project. The operator directs the Chief of Staff, who coordinates specialist assignments.</p></div></div>
    <div className="org-tree" role="group" aria-label="Organization chart">
      <div className="org-node org-operator"><span className="org-node-kind">Company operator</span><strong>You</strong></div>
      {roots.length > 0 && <ul className="org-children org-roots">{roots.map((profile) => branch(profile, new Set()))}</ul>}
    </div>
    <p className="org-boundary">Reporting lines organize responsibility. They do not grant tools, approvals, or autonomous delegation. Work starts through an explicit work order.</p>
  </section>;
}
