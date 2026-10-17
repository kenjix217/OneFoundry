import type { Profile } from './api';

export function ConversationAgentSelect({ profiles, value, disabled, onChange, savedName }: {
  profiles: Profile[]; value: string; disabled: boolean; onChange: (id: string) => void; savedName?: string;
}) {
  const agents = profiles.filter((profile) => profile.name.toLowerCase() !== 'chief of staff');
  return <select className="conversation-agent-select" aria-label="Conversation agent" value={value} disabled={disabled}
    onChange={(event) => onChange(event.target.value)}>
    <option value="">Chief</option>
    {value && !agents.some((agent) => agent.id === value) && <option value={value} disabled>{savedName ?? 'Saved agent'}</option>}
    {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
  </select>;
}
