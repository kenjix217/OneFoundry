import { useEffect, useRef, useState } from 'react';
import { publicMessage } from './ChiefConversationText';
import { ReadableMarkdown } from './ReadableMarkdown';
import { WorkspaceArtifactPanel } from './WorkspaceArtifactPanel';

type SavedFile = { id: string; run_id: string; sha256?: string; status?: 'ready' | 'pending' | 'unknown' };
type Props = { body: string; projectId: string; files: SavedFile[]; onOpenReport?: () => void };

const SHA = /^[0-9a-f]{64}$/i;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// The team artifact readback stops at 100 rows, so a full list cannot prove SHA uniqueness.
const ROOT_FILE_READBACK_CAP = 100;

function matchingFile(token: string, files: SavedFile[]): SavedFile | null {
  if (token.length === 64 && files.length >= ROOT_FILE_READBACK_CAP) return null;
  const exact = files.filter((file) => typeof file.id === 'string' && typeof file.run_id === 'string' &&
    (file.id.toLowerCase() === token || typeof file.sha256 === 'string' && file.sha256.toLowerCase() === token));
  return exact.length === 1 && exact[0]!.status === 'ready' ? exact[0]! : null;
}

export function ChiefArtifactMessageText({ body, projectId, files, onOpenReport }: Props) {
  const [selectedToken, setSelectedToken] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const selected = selectedToken ? matchingFile(selectedToken, files) : null;
  const selectedId = selected?.id;
  const selectedRunId = selected?.run_id;
  useEffect(() => { if (!selectedId || !selectedRunId) setSelectedToken(null); }, [selectedId, selectedRunId]);

  const text = publicMessage(body);
  return <><div className="chief-message-body"><ReadableMarkdown source={text} reference={(label, token, key) => {
    if (!(SHA.test(token) || UUID.test(token)) || !matchingFile(token, files)) return null;
    return <button className="chief-artifact-link" type="button" key={key}
      aria-label={`Open saved file ${label}`} onClick={(event) => {
        onOpenReport?.(); trigger.current = event.currentTarget; setSelectedToken(token);
      }}>{label}</button>;
  }} /></div>
    {selected && <section className="chief-file-preview chief-linked-file-preview" aria-label="Saved file preview">
      <button type="button" onClick={() => { setSelectedToken(null); trigger.current?.focus(); }}>Close file preview</button>
      <WorkspaceArtifactPanel projectId={projectId} runId={selected.run_id} initialArtifactId={selected.id}
        presentation="chief" />
    </section>}</>;
}
