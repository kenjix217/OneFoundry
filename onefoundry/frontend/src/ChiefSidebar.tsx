import { useState } from 'react';
import type { ChiefConversation } from './chiefWorkspaceApi';
import { ChiefConversationRow } from './ChiefConversationRow';

export function ChiefSidebar({ projectName, rows, selectedId, disabled, settingsOpen, integrationsOpen, onSelect, onProjects, onSettings, onIntegrations, onArchive, onRestore, onTrash }: {
  projectName: string; rows: ChiefConversation[]; selectedId: string | null; disabled: boolean; settingsOpen: boolean; integrationsOpen: boolean;
  onSelect: (id: string | null) => void; onProjects: () => void; onSettings: () => void; onIntegrations: () => void;
  onRestore: (order: ChiefConversation) => void;
  onArchive: (order: ChiefConversation) => void; onTrash: (order: ChiefConversation) => void;
}) {
  const [search, setSearch] = useState('');
  const [list, setList] = useState<'active' | 'archived' | 'trash'>('active');
  const visible = rows.filter((row) => (list === 'trash' ? row.trashed : !row.trashed && row.archived === (list === 'archived'))
    && row.title.toLowerCase().includes(search.toLowerCase()));
  return <aside className="chief-sidebar" aria-label="Conversation sidebar">
    <div className="chief-brand">OneFoundry</div><button className="chief-project" onClick={onProjects}>{projectName} <span>⌄</span></button>
    <button className="chief-new" disabled={disabled} onClick={() => { setList('active'); onSelect(null); }}>＋ New conversation</button>
    <input className="chief-search" aria-label="Search conversations" placeholder="Search conversations" value={search} onChange={(event) => setSearch(event.target.value)} />
    <div className="chief-list-heading">{list === 'archived' ? 'Archived conversations' : list === 'trash' ? 'Trash' : 'Conversations'}</div>
    <nav className="chief-conversations" aria-label="Chief conversations">{visible.map((row) => list === 'trash'
      ? <div className="chief-trash-row" key={row.id}><span title={row.title}>{row.title}</span><button disabled={disabled} aria-label={`Restore ${row.title} from Trash`} onClick={() => onRestore(row)}>Restore to Archived</button></div>
      : <ChiefConversationRow key={row.id} row={row} selected={selectedId === row.id} current={selectedId === row.id && !settingsOpen}
        disabled={disabled} onSelect={onSelect} onArchive={onArchive} onRestore={onRestore} onTrash={onTrash} />)}</nav>
    {list !== 'active' && visible.length === 0 && <p className="chief-list-empty">{list === 'trash' ? 'Trash is empty.' : 'No archived conversations.'}</p>}
    <footer><button disabled={disabled} onClick={() => setList(list === 'active' ? 'archived' : 'active')}>{list === 'active' ? 'Archived' : 'Active conversations'}</button>
      {rows.some((row) => row.trashed) && <button disabled={disabled} onClick={() => setList('trash')}>Trash</button>}
      <button onClick={onProjects}>Projects</button><button aria-current={settingsOpen ? 'page' : undefined} onClick={onSettings}>Settings</button>
      <button aria-current={integrationsOpen ? 'page' : undefined} onClick={onIntegrations}>Integrations</button></footer>
  </aside>;
}
