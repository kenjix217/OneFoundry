import { useState } from 'react';
import type { ChiefFile } from './ChiefAgentPanel';
import { WorkspaceArtifactPanel } from './WorkspaceArtifactPanel';

const TRACE_ID = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b[0-9a-f]{24,64}\b/gi;
const RECENT_FILES = 6;

function shortTitle(title: string): string {
  const readable = title.replace(TRACE_ID, '').replace(/\s+/g, ' ').trim();
  if (!readable) return 'Saved file';
  return readable.length > 80 ? `${readable.slice(0, 80).trimEnd()}…` : readable;
}

export function ChiefDeliverables({ projectId, files }: { projectId: string; files: ChiefFile[] }) {
  const [selected, setSelected] = useState<ChiefFile | null>(null);
  const [showAll, setShowAll] = useState(false);
  if (!files.length) return null;
  const visible = showAll ? files : files.slice(0, RECENT_FILES);
  return <section className="chief-deliverables" aria-label="Saved deliverables" id="chief-deliverables">
    <h2>Saved files <small>{files.length}</small></h2>
    <div className="chief-deliverables-list">{visible.map((file, index) => {
      const fullTitle = file.work_order_title?.trim();
      const title = fullTitle ? shortTitle(fullTitle) : `${file.profile_name} · ${file.kind.replaceAll('_', ' ')} file ${index + 1}`;
      return <div className="chief-deliverable-card" key={file.id}>
        <button type="button" aria-label={`Open saved file ${index + 1}: ${title}`}
          aria-current={selected?.id === file.id ? 'true' : undefined} onClick={() => setSelected(file)}>
          <span className="chief-deliverable-title">{title}</span>
          <small>File {index + 1} · {file.profile_name} · {file.kind.replaceAll('_', ' ')} · {file.status === 'ready' ? 'Saved' : file.status === 'unknown' ? 'Outcome unknown' : file.status === 'pending' ? 'Pending' : 'Status unavailable'}</small>
        </button>
        {fullTitle && title !== fullTitle && <details><summary>Full task title</summary><p>{fullTitle}</p></details>}
      </div>;
    })}</div>
    {files.length > RECENT_FILES && <button className="chief-deliverables-toggle" type="button" aria-expanded={showAll}
      onClick={() => setShowAll((value) => !value)}>{showAll ? `Show recent ${RECENT_FILES} files` : `Show all ${files.length} files`}</button>}
    {selected && <div className="chief-file-preview"><button type="button" onClick={() => setSelected(null)}>Close file preview</button>
      <WorkspaceArtifactPanel projectId={projectId} runId={selected.run_id} initialArtifactId={selected.id} /></div>}
  </section>;
}
