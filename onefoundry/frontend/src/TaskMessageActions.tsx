type Props = {
  text: string;
  source: 'your' | 'agent';
  onCopy: (text: string) => void;
  onEdit?: () => void;
};

export function TaskMessageActions({ text, source, onCopy, onEdit }: Props) {
  return <div className="task-message-actions" aria-label={`${source === 'your' ? 'Your' : 'Agent'} message actions`}>
    <button type="button" className="task-message-icon" aria-label={`Copy ${source} message`}
      title="Copy message" onClick={() => onCopy(text)}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
        strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
      </svg>
    </button>
    {onEdit && <button type="button" className="task-message-icon" aria-label={`Edit ${source} message`}
      title="Revise in composer as a new message" onClick={onEdit}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
        strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="m15 5 4 4M4 20l4.4-1 10.7-10.7a2.1 2.1 0 0 0-3-3L5.4 16 4 20Z" />
      </svg>
    </button>}
  </div>;
}
