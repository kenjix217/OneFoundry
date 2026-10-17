import type { ChiefConversation } from './chiefWorkspaceApi';

type Props = {
  row: ChiefConversation; selected: boolean; current: boolean; disabled: boolean;
  onSelect: (id: string) => void; onArchive: (row: ChiefConversation) => void;
  onRestore: (row: ChiefConversation) => void; onTrash: (row: ChiefConversation) => void;
};

export function ChiefConversationRow({ row, selected, current, disabled, onSelect, onArchive, onRestore, onTrash }: Props) {
  return <div className={`chief-conversation-row ${selected ? 'selected' : ''}`}>
    <button className="chief-conversation-select" disabled={disabled} aria-current={current ? 'page' : undefined}
      title={row.title} onClick={() => onSelect(row.id)}><span>{row.title}</span><small>{row.state}</small></button>
    <div className="chief-conversation-actions">
      <button type="button" disabled={disabled} aria-label={`${row.archived ? 'Restore' : 'Archive'} ${row.title}`}
        title={row.archived ? 'Restore conversation' : 'Archive conversation'}
        onClick={() => row.archived ? onRestore(row) : onArchive(row)}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M4 9v11h16V9M3 4h18v5H3z" />
          {row.archived ? <path d="M12 17v-6m-3 3 3-3 3 3" /> : <path d="M9 13h6" />}
        </svg>
      </button>
      {row.archived && <button type="button" disabled={disabled} aria-label={`Move ${row.title} to Trash`}
        title="Delete conversation · recoverable in Trash" onClick={() => onTrash(row)}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7" />
        </svg>
      </button>}
    </div>
  </div>;
}
