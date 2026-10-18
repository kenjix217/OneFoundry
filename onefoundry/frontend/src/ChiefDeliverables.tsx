import { useState } from 'react';
import type { ChiefFile } from './ChiefAgentPanel';
import { WorkspaceArtifactPanel } from './WorkspaceArtifactPanel';

export function ChiefDeliverables({ projectId, files }: { projectId: string; files: ChiefFile[] }) {
  const [selected, setSelected] = useState<ChiefFile | null>(null);
  if (!files.length) return null;
  return <section className="chief-deliverables" aria-label="Saved deliverables" id="chief-deliverables">
    <h2>Saved files <small>{files.length}</small></h2>
    <div className="chief-deliverables-list">{files.map((file, index) => <button type="button" key={file.id}
      aria-current={selected?.id === file.id ? 'true' : undefined} onClick={() => setSelected(file)}>
      <span>{file.work_order_title?.trim() ? `${file.work_order_title.trim()} · file ${index + 1}` : `${file.profile_name} · ${file.kind.replaceAll('_', ' ')} file ${index + 1}`}</span>
      <small>{file.work_order_title?.trim() && `${file.profile_name} · ${file.kind.replaceAll('_', ' ')} · `}{file.status === 'ready' ? 'Saved' : file.status === 'unknown' ? 'Outcome unknown' : file.status === 'pending' ? 'Pending' : 'Status unavailable'}</small>
    </button>)}</div>
    {selected && <div className="chief-file-preview"><button type="button" onClick={() => setSelected(null)}>Close file preview</button>
      <WorkspaceArtifactPanel projectId={projectId} runId={selected.run_id} initialArtifactId={selected.id} /></div>}
  </section>;
}
