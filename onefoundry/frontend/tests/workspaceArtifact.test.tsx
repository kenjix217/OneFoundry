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
