import { useEffect, useState } from 'react';
import { SavedVideoPlayer } from './SavedVideoPlayer';

import { getWorkspaceArtifact, listWorkspaceArtifacts,
  type WorkspaceArtifact, type WorkspaceArtifactPreview } from './workspaceArtifactApi';

type Props = { projectId: string; runId: string; initialArtifactId?: string };

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Saved artifact readback is unavailable.';
}

function time(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function WorkspaceArtifactPanel({ projectId, runId, initialArtifactId }: Props) {
  const [artifacts, setArtifacts] = useState<WorkspaceArtifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [preview, setPreview] = useState<WorkspaceArtifactPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [previewRevision, setPreviewRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setArtifacts([]);
    setLoading(true);
    setListError(null);
    setPreviewId(initialArtifactId ?? null);
    setPreview(null);
    setPreviewError(null);
    void listWorkspaceArtifacts(projectId, runId, controller.signal).then((items) => {
      if (controller.signal.aborted) return;
      setArtifacts(items);
      setLoading(false);
    }).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      setListError(message(cause));
      setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, runId, reload, initialArtifactId]);

  useEffect(() => {
    if (!previewId) return;
    const saved = artifacts.find((artifact) => artifact.id === previewId);
    if (!saved || saved.status !== 'ready') return;
    const controller = new AbortController();
    setPreview(null);
    setPreviewError(null);
    setPreviewLoading(true);
    void getWorkspaceArtifact(projectId, runId, saved, controller.signal).then((item) => {
      if (controller.signal.aborted) return;
      setPreview(item);
      setPreviewLoading(false);
    }).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      setPreviewError(message(cause));
      setPreviewLoading(false);
    });
    return () => controller.abort();
  }, [projectId, runId, artifacts, previewId, previewRevision]);

  if (!loading && !listError && artifacts.length === 0) return null;

  return <section className="timeline" aria-labelledby="workspace-artifacts-heading">
    <div className="section-heading"><div><p className="eyebrow">Private run workspace</p><h3 id="workspace-artifacts-heading">Saved artifacts</h3></div>
      {artifacts.length > 0 && <span>{artifacts.length} {artifacts.length === 1 ? 'artifact' : 'artifacts'}</span>}</div>
    {loading && <p className="quiet-note" role="status">Loading saved artifacts…</p>}
    {listError && <p className="form-error" role="alert">Could not read saved artifact records: {listError}</p>}
    {!loading && <button className="button button-outline" type="button" onClick={() => setReload((value) => value + 1)}>Refresh saved artifacts</button>}
    {artifacts.map((artifact) => <article className="timeline-step" key={artifact.id}>
      <div className="timeline-step-body">
        <h4>{artifact.kind} · <span className={`status-chip status-${artifact.status}`}>{artifact.status}</span></h4>
        <p>Saved file · {artifact.byte_size} bytes · {artifact.mime_type}</p>

        <p>Created {time(artifact.created_at)} · {artifact.finished_at ? `Finished ${time(artifact.finished_at)}` : 'No final outcome recorded'}</p>
        {artifact.status === 'ready' ? <button className="button button-outline" type="button"
          onClick={() => setPreviewId(artifact.id)}>Preview verified {artifact.kind}</button> :
          <p className="quiet-note">{artifact.status === 'pending'
            ? 'Write intent is pending. Saved bytes have not been confirmed.'
            : 'Write outcome is unknown. Saved bytes are not available for preview.'}</p>}
        {previewId === artifact.id && previewLoading && <p className="quiet-note" role="status">Verifying saved artifact…</p>}
        {previewId === artifact.id && previewError && <p className="form-error" role="alert">Artifact readback failed: {previewError}</p>}
        {previewId === artifact.id && preview && <pre aria-label="Verified workspace artifact text">{preview.content}</pre>}
        {previewId === artifact.id && preview?.kind === 'video' && <SavedVideoPlayer key={preview.id}
          projectId={projectId} runId={runId} artifactId={preview.id} sha256={preview.sha256} verified={!!preview.playback_verified} onVerified={() => setPreviewRevision((value) => value + 1)} />}
      </div>
    </article>)}
  </section>;
}
