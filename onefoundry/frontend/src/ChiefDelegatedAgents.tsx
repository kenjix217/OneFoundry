import type { Profile } from './api';
import type { TeamAssignment, TeamChild } from './taskSessionApi';

type Props = { children: TeamChild[]; assignments: TeamAssignment[]; profiles: Profile[]; onOpen: (id: string) => void };

const unfinished = new Set<TeamAssignment['status']>(['pending', 'ready', 'active', 'blocked']);
const traceId = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b[0-9a-f]{40,64}\b/gi;

function shortObjective(objective: string | undefined, limit: number) {
  const clean = (objective ?? '').replace(traceId, '').replace(/\s+/g, ' ').trim();
  return clean.length <= limit ? clean : `${clean.slice(0, limit - 1).trimEnd()}…`;
}

function agentLabel(child: TeamChild, assignment: TeamAssignment | undefined, profiles: Profile[]) {
  const role = profiles.find((profile) => profile.id === child.profile_id)?.name ?? 'Agent';
  const objective = shortObjective(assignment?.objective, 96) || 'Saved assignment';
  const titleObjective = shortObjective(assignment?.objective, 160) || 'Saved assignment';
  return { role, objective, titleObjective };
}

export function ChiefDelegatedAgents({ children, assignments, profiles, onOpen }: Props) {
  if (!children.length) return null;
  const byChild = new Map(assignments.map((assignment) => [assignment.child_session_id, assignment]));
  const current = children.filter((child) => child.status === 'working' || unfinished.has(byChild.get(child.id)?.status ?? 'satisfied'));
  const past = children.filter((child) => !current.includes(child));
  function agentButton(child: TeamChild) {
    const assignment = byChild.get(child.id);
    const { role, objective, titleObjective } = agentLabel(child, assignment, profiles);
    const status = assignment?.status === 'blocked' ? 'Blocked' : child.status === 'working' ? 'Working' : assignment?.status === 'pending' ? 'Waiting' : assignment?.status === 'ready' ? 'Ready' : assignment?.status === 'active' ? 'Assigned' : 'Past';
    return <button type="button" key={child.id} title={`${role}: ${titleObjective} · ${status}`} aria-label={`Open ${role}: ${objective} (${status})`} onClick={() => onOpen(child.id)}>
      <span className="chief-agent-role">{role}</span><span className="chief-agent-objective">{objective}</span><span className="chief-agent-status">{status}</span>
    </button>;
  }
  return <nav className="chief-agents" aria-label="Delegated agents">
    {current.length > 0 && <div className="chief-agent-list">{current.map(agentButton)}</div>}
    {past.length > 0 && <details className="chief-past-agents"><summary>Past agent work ({past.length})</summary><div className="chief-agent-list">{past.map(agentButton)}</div></details>}
  </nav>;
}
