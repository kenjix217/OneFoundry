import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { RunTimelineView } from '../src/RunTimelineView';
import { WorkspaceArtifactPanel } from '../src/WorkspaceArtifactPanel';
import { getWorkspaceArtifact, listWorkspaceArtifacts, type WorkspaceArtifact } from '../src/workspaceArtifactApi';
import type { Run } from '../src/workApi';

const projectId = 'project-1';
const runId = 'run-1';
const artifactId = 'c2222222-2222-4222-8222-222222222222';
const created = '2026-09-28T12:00:00Z';
const artifactsPath = `/api/projects/${projectId}/runs/${runId}/workspace-artifacts`;
const content = '# Saved note\n<script>alert(1)</script>\n';
const artifact: WorkspaceArtifact = {
  id: artifactId, kind: 'markdown', mime_type: 'text/markdown', sha256: 'a'.repeat(64),
  byte_size: new TextEncoder().encode(content).length, status: 'ready', created_at: created,
  finished_at: created,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('validates list and preview identity before exposing artifact text', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === artifactsPath) return json([artifact]);
    if (path === `${artifactsPath}/${artifactId}`) return json({
      id: artifactId, kind: artifact.kind, mime_type: artifact.mime_type,
      sha256: artifact.sha256, byte_size: artifact.byte_size, content,
    });
    throw new Error(`Unexpected path ${path}`);
  }));
  const listed = await listWorkspaceArtifacts(projectId, runId);
  expect(listed).toEqual([artifact]);
  expect((await getWorkspaceArtifact(projectId, runId, listed[0])).content).toBe(content);

  vi.stubGlobal('fetch', vi.fn(async () => json({
    id: artifactId, kind: artifact.kind, mime_type: artifact.mime_type,
    sha256: 'b'.repeat(64), byte_size: artifact.byte_size, content,
  })));
  await expect(getWorkspaceArtifact(projectId, runId, artifact)).rejects.toThrow('invalid workspace artifact preview identity');
});

it('shows verified text literally and distinguishes pending and unknown writes', async () => {
  const pending: WorkspaceArtifact = { ...artifact, id: 'd3333333-3333-4333-8333-333333333333', status: 'pending', finished_at: null };
  const unknown: WorkspaceArtifact = { ...artifact, id: 'e4444444-4444-4444-8444-444444444444', status: 'unknown' };
  const fetcher = vi.fn(async (path: string) => {
    if (path === artifactsPath) return json([artifact, pending, unknown]);
    if (path === `${artifactsPath}/${artifactId}`) return json({
      id: artifactId, kind: artifact.kind, mime_type: artifact.mime_type,
      sha256: artifact.sha256, byte_size: artifact.byte_size, content,
    });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const { container } = render(<WorkspaceArtifactPanel projectId={projectId} runId={runId} />);
  expect(await screen.findByText('3 artifacts')).toBeInTheDocument();
  expect(screen.getByText('Write intent is pending. Saved bytes have not been confirmed.')).toBeInTheDocument();
  expect(screen.getByText('Write outcome is unknown. Saved bytes are not available for preview.')).toBeInTheDocument();
  expect(screen.getAllByRole('button', { name: /Preview verified/ })).toHaveLength(1);
  await user.click(screen.getByRole('button', { name: 'Preview verified markdown' }));
  expect((await screen.findByLabelText('Verified workspace artifact text')).textContent).toBe(content);
  expect(container.querySelector('script')).toBeNull();
  expect(fetcher).toHaveBeenCalledWith(`${artifactsPath}/${artifactId}`, expect.anything());
});

it('opens a selected Chief report from verified readback, formats it, and keeps its exact source copyable', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path === artifactsPath) return json([artifact]);
    if (path === `${artifactsPath}/${artifactId}`) return json({ ...artifact, content });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const writeText = vi.fn(async () => undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  render(<WorkspaceArtifactPanel projectId={projectId} runId={runId} initialArtifactId={artifactId}
    presentation="chief" displayName="DistrictAI report" />);
  const title = await screen.findByRole('heading', { name: 'DistrictAI report' });
  await waitFor(() => expect(title).toHaveFocus());
  expect(screen.getByRole('heading', { name: 'Saved note' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Preview verified/ })).not.toBeInTheDocument();
  expect(screen.getByLabelText('Exact saved report source').textContent).toBe(content);
  expect(screen.getByText('Other saved files and details').closest('details')).not.toHaveAttribute('open');
  await user.click(screen.getByRole('button', { name: 'Copy report' }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(content));
  expect(fetcher).toHaveBeenCalledWith(`${artifactsPath}/${artifactId}`, expect.anything());
});

it('shows and focuses verified readback failure in Chief mode without showing report actions', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => path === artifactsPath
    ? json([artifact]) : json({ detail: 'stored bytes unavailable' }, 409)));
  render(<WorkspaceArtifactPanel projectId={projectId} runId={runId} initialArtifactId={artifactId}
    presentation="chief" />);
  const error = await screen.findByRole('alert');
  expect(error).toHaveTextContent('stored bytes unavailable');
  expect(error).toHaveFocus();
  expect(screen.queryByRole('button', { name: 'Copy report' })).not.toBeInTheDocument();
});

it('keeps an absent or pending selected file unavailable in Chief mode', async () => {
  const pending: WorkspaceArtifact = { ...artifact, status: 'pending', finished_at: null };
  const fetcher = vi.fn(async () => json([pending]));
  vi.stubGlobal('fetch', fetcher);
  const result = render(<WorkspaceArtifactPanel projectId={projectId} runId={runId}
    initialArtifactId={artifactId} presentation="chief" />);
  expect(await screen.findByText('This file is still being saved.')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Saved report' })).toHaveFocus();
  expect(fetcher).toHaveBeenCalledTimes(1);
  result.unmount();

  vi.stubGlobal('fetch', vi.fn(async () => json([artifact])));
  render(<WorkspaceArtifactPanel projectId={projectId} runId={runId}
    initialArtifactId="missing-in-run" presentation="chief" />);
  const missing = await screen.findByRole('alert');
  expect(missing).toHaveTextContent('not found in the selected run');
  expect(missing).toHaveFocus();
  expect(screen.queryByRole('button', { name: 'Copy report' })).not.toBeInTheDocument();
});

it('saves verified report text as a local copy and shows a browser failure', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => path === artifactsPath
    ? json([artifact]) : json({ ...artifact, content })));
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  const create = vi.fn((blob: Blob) => { void blob; return 'blob:verified-report'; });
  URL.createObjectURL = create;
  URL.revokeObjectURL = vi.fn();
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    expect(this.download).toBe('saved-report.md');
    expect(this.href).toBe('blob:verified-report');
  });
  try {
    render(<WorkspaceArtifactPanel projectId={projectId} runId={runId}
      initialArtifactId={artifactId} presentation="chief" />);
    await screen.findByRole('button', { name: 'Save a copy' });
    await userEvent.setup().click(screen.getByRole('button', { name: 'Save a copy' }));
    expect(click).toHaveBeenCalledTimes(1);
    const savedText = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(create.mock.calls[0]![0]);
    });
    expect(savedText).toBe(content);
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:verified-report'));
    create.mockImplementation(() => { throw new Error('unsupported'); });
    await userEvent.setup().click(screen.getByRole('button', { name: 'Save a copy' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not save a copy');
  } finally {
    URL.createObjectURL = originalCreate;
    URL.revokeObjectURL = originalRevoke;
    click.mockRestore();
  }
});

it('keeps readback errors visible and does not show an empty panel', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === artifactsPath) return json([artifact]);
    if (path === `${artifactsPath}/${artifactId}`) return json({ detail: 'artifact readback failed' }, 409);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<WorkspaceArtifactPanel projectId={projectId} runId={runId} />);
  await user.click(await screen.findByRole('button', { name: 'Preview verified markdown' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('artifact readback failed');
  expect(screen.queryByLabelText('Verified workspace artifact text')).not.toBeInTheDocument();

  vi.stubGlobal('fetch', vi.fn(async () => json([])));
  const empty = render(<WorkspaceArtifactPanel projectId="another-project" runId="another-run" />);
  await waitFor(() => expect(empty.container).toBeEmptyDOMElement());
});

it('places persisted artifacts in the selected run timeline', async () => {
  const run: Run = {
    id: runId, project_id: projectId, work_order_id: 'work-1',
    work_order_title: 'Write a note', work_order_instructions: 'Save one artifact',
    profile_id: 'role-1', profile_model: 'model-1', mode: 'live_text', status: 'running',
    tokens_used: null, cost_usd: null, outcome_reason: '', started_at: created, finished_at: null,
  };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path === `/api/projects/${projectId}/runs/${runId}`) return json({
      ...run, profile_snapshots: [], skill_snapshots: [], events: [{
        id: 'event-1', project_id: projectId, work_order_id: 'work-1', run_id: runId,
        event_type: 'role_tool_started', actor_id: 'profile:role-1',
        payload: { tool_id: 'write_text_artifact_v1', artifact_id: artifactId }, created_at: created,
      }],
    });
    if (path === artifactsPath) return json([artifact]);
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<RunTimelineView projectId={projectId} profiles={[]} initialRunId={runId} />);
  const section = await screen.findByRole('region', { name: 'Saved artifacts' });
  expect(await within(section).findByText('1 artifact')).toBeInTheDocument();
  expect(within(section).getByText(`Saved file · ${artifact.byte_size} bytes · text/markdown`)).toBeInTheDocument();
});

it('does not show content from an aborted earlier report selection and reports clipboard denial', async () => {
  let finishFirst: ((value: Response) => void) | undefined;
  const secondId = 'b5555555-5555-4555-8555-555555555555';
  const second = { ...artifact, id: secondId };
  const fetcher = vi.fn(async (path: string) => {
    if (path === artifactsPath) return json([artifact, second]);
    if (path.endsWith(secondId)) return json({ ...second, content });
    return new Promise<Response>((resolve) => { finishFirst = resolve; });
  });
  vi.stubGlobal('fetch', fetcher);
  const result = render(<WorkspaceArtifactPanel projectId={projectId} runId={runId}
    initialArtifactId={artifactId} presentation="chief" />);
  await waitFor(() => expect(finishFirst).toBeDefined());
  result.rerender(<WorkspaceArtifactPanel projectId={projectId} runId={runId}
    initialArtifactId={secondId} presentation="chief" />);
  await screen.findByRole('button', { name: 'Copy report' });
  finishFirst?.(json({ ...artifact, content: 'STALE report bytes' }));
  await waitFor(() => expect(screen.queryByText('STALE report bytes')).not.toBeInTheDocument());
  const user = userEvent.setup();
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
    writeText: vi.fn(async () => { throw new Error('denied'); }),
  } });
  await user.click(screen.getByRole('button', { name: 'Copy report' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not copy this report');
});
