import { useEffect, useRef, useState } from 'react';
import { SavedVideoPlayer } from './SavedVideoPlayer';
import { ReadableMarkdown } from './ReadableMarkdown';

import { getWorkspaceArtifact, listWorkspaceArtifacts,
  type WorkspaceArtifact, type WorkspaceArtifactPreview } from './workspaceArtifactApi';

type Props = { projectId: string; runId: string; initialArtifactId?: string;
  presentation?: 'technical' | 'chief'; displayName?: string; onOpenReport?: () => void };

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Saved artifact readback is unavailable.';
}

function time(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function WorkspaceArtifactPanel({ projectId, runId, initialArtifactId, presentation = 'technical',
  displayName, onOpenReport }: Props) {
  const [artifacts, setArtifacts] = useState<WorkspaceArtifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [preview, setPreview] = useState<WorkspaceArtifactPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [previewRevision, setPreviewRevision] = useState(0);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [copyNotice, setCopyNotice] = useState<string | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const readError = useRef<HTMLParagraphElement>(null);
  const missingError = useRef<HTMLParagraphElement>(null);
  const focusOnRead = useRef(!!initialArtifactId);
  const selectionKey = useRef('');
  const selected = artifacts.find((artifact) => artifact.id === previewId);

  useEffect(() => {
    const key = `${projectId}:${runId}:${initialArtifactId ?? ''}`;
    if (selectionKey.current !== key) { focusOnRead.current = !!initialArtifactId; selectionKey.current = key; }
    const controller = new AbortController();
    setArtifacts([]);
    setLoading(true);
    setListError(null);
    setPreviewId(initialArtifactId ?? null);
    setPreview(null);
    setPreviewError(null);
    setCopyError(null);
    setCopyNotice(null);
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

  useEffect(() => {
    if (presentation !== 'chief' || !focusOnRead.current) return;
    if (preview && selected?.id === preview.id) {
      focusOnRead.current = false; heading.current?.focus(); heading.current?.scrollIntoView?.({ block: 'start' });
    } else if (previewError || listError) {
      focusOnRead.current = false; readError.current?.focus(); readError.current?.scrollIntoView?.({ block: 'nearest' });
    } else if (!loading && selected?.status !== 'ready' && selected) {
      focusOnRead.current = false; heading.current?.focus(); heading.current?.scrollIntoView?.({ block: 'start' });
    } else if (!loading && initialArtifactId && !selected) {
      focusOnRead.current = false; missingError.current?.focus(); missingError.current?.scrollIntoView?.({ block: 'nearest' });
    }
  }, [presentation, preview, previewError, listError, loading, selected, initialArtifactId]);

  function select(id: string) {
    onOpenReport?.();
    focusOnRead.current = true;
    setCopyError(null);
    setCopyNotice(null);
    setPreviewId(id);
  }

  async function copyVerified() {
    if (!preview) return;
    try { await navigator.clipboard.writeText(preview.content); setCopyError(null); setCopyNotice('Report copied.'); }
    catch { setCopyNotice(null); setCopyError('Could not copy this report. Check clipboard access in this browser.'); }
  }

  function saveVerified() {
    if (!preview) return;
    try {
      const url = URL.createObjectURL(new Blob([preview.content], { type: preview.mime_type }));
      const link = document.createElement('a');
      link.href = url;
      link.download = preview.kind === 'markdown' ? 'saved-report.md' : preview.kind === 'csv' ? 'saved-data.csv' : 'saved-report.txt';
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      setCopyError(null);
    } catch {
      setCopyError('Could not save a copy of this report in this browser.');
    }
  }

  if (!loading && !listError && artifacts.length === 0 && (!initialArtifactId || presentation !== 'chief')) return null;

  if (presentation === 'chief') {
    return <section className="chief-saved-reports" aria-label="Saved files">
      {loading && <p role="status">Loading saved files…</p>}
      {listError && <p ref={readError} tabIndex={-1} className="form-error" role="alert">Could not read saved files: {listError}</p>}
      {!loading && !listError && initialArtifactId && !selected &&
        <p ref={missingError} tabIndex={-1} className="form-error" role="alert">This saved file was not found in the selected run.</p>}
      {selected && <article className="chief-report">
        <h3 ref={heading} tabIndex={-1}>{displayName || (selected.kind === 'markdown' ? 'Saved report' : 'Saved file')}</h3>
        {selected.status !== 'ready' && <p role="status">{selected.status === 'pending'
          ? 'This file is still being saved.' : 'The saved file outcome is unknown. Its content cannot be opened.'}</p>}
        {previewLoading && <p role="status">Verifying saved file…</p>}
        {previewError && <p ref={readError} tabIndex={-1} className="form-error" role="alert">Could not open saved file: {previewError}</p>}
        {preview && preview.id === selected.id && <>
          {preview.kind !== 'video' && <div className="chief-report-actions">
            <button type="button" onClick={() => { void copyVerified(); }}>Copy report</button>
            <button type="button" onClick={saveVerified}>Save a copy</button>
          </div>}
          {copyNotice && <p role="status">{copyNotice}</p>}
          {copyError && <p className="form-error" role="alert">{copyError}</p>}
          {preview.kind === 'markdown' ? <ReadableMarkdown source={preview.content} /> :
            preview.kind !== 'video' && <pre aria-label="Verified workspace artifact text">{preview.content}</pre>}
          {preview.kind === 'video' && <SavedVideoPlayer key={preview.id}
            projectId={projectId} runId={runId} artifactId={preview.id} sha256={preview.sha256}
            verified={!!preview.playback_verified} onVerified={() => setPreviewRevision((value) => value + 1)} />}
          {preview.kind !== 'video' && <details className="chief-report-source"><summary>Exact saved text</summary>
            <pre aria-label="Exact saved report source">{preview.content}</pre></details>}
        </>}
      </article>}
      {!loading && !listError && <details className="chief-report-files" open={!selected}>
        <summary>{selected ? 'Other saved files and details' : `Saved files (${artifacts.length})`}</summary>
        {artifacts.map((artifact, index) => <div className="chief-report-file" key={artifact.id}>
          <button type="button" disabled={artifact.status !== 'ready'} onClick={() => select(artifact.id)}>
            Open saved file {index + 1} · {artifact.kind === 'markdown' ? 'Report' : artifact.kind}
          </button>
          {artifact.status !== 'ready' && <span>{artifact.status === 'pending' ? 'Saving' : 'Outcome unknown'}</span>}
          <details><summary>File details</summary><p>{artifact.kind} · {artifact.byte_size} bytes · {artifact.mime_type}</p>
            <p>Created {time(artifact.created_at)} · {artifact.finished_at ? `Finished ${time(artifact.finished_at)}` : 'No final outcome recorded'}</p></details>
        </div>)}
        <button type="button" onClick={() => setReload((value) => value + 1)}>Refresh saved files</button>
      </details>}
    </section>;
  }

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
