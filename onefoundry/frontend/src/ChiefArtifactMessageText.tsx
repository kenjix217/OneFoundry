import { useEffect, useRef, useState, type ReactNode } from 'react';
import { publicMessage } from './ChiefConversationText';
import { WorkspaceArtifactPanel } from './WorkspaceArtifactPanel';

type SavedFile = { id: string; run_id: string; sha256?: string; status?: 'ready' | 'pending' | 'unknown' };
type Props = { body: string; projectId: string; files: SavedFile[] };

const SHA = /^[0-9a-f]{64}$/i;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PREFIX = '(artifact:';
// The team artifact readback stops at 100 rows, so a full list cannot prove SHA uniqueness.
const ROOT_FILE_READBACK_CAP = 100;

function referenceAt(text: string, open: number): { label: string; token: string; end: number } | null {
  if (open > 0 && text[open - 1] === '[') return null;
  let close = open + 1;
  while (close < text.length && close - open <= 120 && ![']', '[', '\n'].includes(text[close]!)) close++;
  if (text[close] !== ']' || close === open + 1 || !text.startsWith(PREFIX, close + 1)) return null;
  const tokenStart = close + 1 + PREFIX.length;
  const sha = text.slice(tokenStart, tokenStart + 64);
  const uuid = text.slice(tokenStart, tokenStart + 36);
  const token = SHA.test(sha) && text[tokenStart + 64] === ')' ? sha :
    UUID.test(uuid) && text[tokenStart + 36] === ')' ? uuid : null;
  return token ? { label: text.slice(open + 1, close), token: token.toLowerCase(), end: tokenStart + token.length + 1 } : null;
}

function matchingFile(token: string, files: SavedFile[]): SavedFile | null {
  if (token.length === 64 && files.length >= ROOT_FILE_READBACK_CAP) return null;
  const exact = files.filter((file) => typeof file.id === 'string' && typeof file.run_id === 'string' &&
    (file.id.toLowerCase() === token || typeof file.sha256 === 'string' && file.sha256.toLowerCase() === token));
  return exact.length === 1 && exact[0]!.status === 'ready' ? exact[0]! : null;
}

export function ChiefArtifactMessageText({ body, projectId, files }: Props) {
  const [selectedToken, setSelectedToken] = useState<string | null>(null);
  const preview = useRef<HTMLElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const selected = selectedToken ? matchingFile(selectedToken, files) : null;
  const selectedId = selected?.id;
  const selectedRunId = selected?.run_id;
  useEffect(() => {
    if (selectedId && selectedRunId) {
      preview.current?.scrollIntoView?.({ block: 'nearest' });
      preview.current?.querySelector<HTMLButtonElement>('button')?.focus();
    }
  }, [selectedId, selectedRunId]);

  const text = publicMessage(body);
  const parts: ReactNode[] = [];
  let offset = 0;
  let cursor = 0;
  while (cursor < text.length) {
    const open = text.indexOf('[', cursor);
    if (open < 0) break;
    const reference = referenceAt(text, open);
    if (!reference) { cursor = open + 1; continue; }
    const file = matchingFile(reference.token, files);
    if (!file) { cursor = reference.end; continue; }
    parts.push(text.slice(offset, open));
    parts.push(<button className="chief-artifact-link" type="button" key={open}
      aria-label={`Open saved file ${reference.label}`} onClick={(event) => { trigger.current = event.currentTarget; setSelectedToken(reference.token); }}>
      {reference.label}
    </button>);
    offset = reference.end;
    cursor = reference.end;
  }
  parts.push(text.slice(offset));

  return <><div className="chief-message-body">{parts}</div>
    {selected && <section className="chief-file-preview chief-linked-file-preview" aria-label="Saved file preview" ref={preview}>
      <button type="button" onClick={() => { setSelectedToken(null); trigger.current?.focus(); }}>Close file preview</button>
      <WorkspaceArtifactPanel projectId={projectId} runId={selected.run_id} initialArtifactId={selected.id} />
    </section>}</>;
}
