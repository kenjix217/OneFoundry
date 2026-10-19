import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChiefArtifactMessageText } from '../src/ChiefArtifactMessageText';

const artifactId = '5b7a80c2-3d81-4e07-a502-2e82f2291192';
const sha = 'a'.repeat(64);
const file = { id: artifactId, run_id: 'run-1', sha256: sha, status: 'ready' as const };
const saved = { id: artifactId, kind: 'markdown', mime_type: 'text/markdown', sha256: sha,
  byte_size: 23, status: 'ready', created_at: '2026-10-18T12:00:00Z', finished_at: '2026-10-18T12:01:00Z' };
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
afterEach(() => vi.unstubAllGlobals());

it('opens the exact same-root saved artifact by SHA or ID through the verified preview owner', async () => {
  const fetch = vi.fn(async (path: string) => {
    if (path.endsWith('/runs/run-1/workspace-artifacts')) return json([saved]);
    if (path.endsWith(`/runs/run-1/workspace-artifacts/${artifactId}`)) return json({ ...saved, content: 'Verified final index text' });
    return json([]);
  });
  vi.stubGlobal('fetch', fetch);
  const user = userEvent.setup();
  render(<ChiefArtifactMessageText projectId="project-1" files={[file]}
    body={`Read [final index](artifact:${sha}) or [saved copy](artifact:${artifactId}).`} />);
  expect(screen.getByRole('button', { name: 'Open saved file saved copy' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open saved file final index' }));
  await waitFor(() => expect(screen.getByRole('heading', { name: 'Saved report' })).toHaveFocus());
  expect(within(screen.getByLabelText('Saved file preview')).getByText('Verified final index text', { selector: 'p' })).toBeInTheDocument();
  expect(fetch.mock.calls.some(([path]) => path.endsWith(`/projects/project-1/runs/run-1/workspace-artifacts/${artifactId}`))).toBe(true);
  await user.click(screen.getByRole('button', { name: 'Close file preview' }));
  expect(screen.getByRole('button', { name: 'Open saved file final index' })).toHaveFocus();
  await user.click(screen.getByRole('button', { name: 'Open saved file saved copy' }));
  await waitFor(() => expect(screen.getByRole('heading', { name: 'Saved report' })).toHaveFocus());
});

it('leaves unknown, ambiguous, malformed, and external references inert as literal text', () => {
  const duplicate = { id: 'other-id', run_id: 'run-2', sha256: sha, status: 'ready' as const };
  render(<ChiefArtifactMessageText projectId="project-1" files={[file, duplicate]}
    body={`[ambiguous](artifact:${sha}) [unknown](artifact:${'b'.repeat(64)}) [script](artifact:javascript:alert(1)) [external](https://example.com) [[broken](artifact:${artifactId})`} />);
  expect(screen.queryByRole('button', { name: /Open saved file/ })).not.toBeInTheDocument();
  const message = screen.getByText(/ambiguous/);
  expect(message).toHaveTextContent(`[ambiguous](artifact:${sha})`);
  expect(message).toHaveTextContent('[script](artifact:javascript:alert(1))');
  expect(message).toHaveTextContent('[external](https://example.com)');
  expect(message).toHaveTextContent(`[[broken](artifact:${artifactId})`);
});

it('keeps pending and unknown files inert and does not trust a SHA match in a capped root list', () => {
  const pending = { ...file, status: 'pending' as const };
  const unknown = { ...file, id: '21d98688-d979-4bbf-af05-7897398afcc0', status: 'unknown' as const };
  const { rerender } = render(<ChiefArtifactMessageText projectId="project-1" files={[pending, unknown]}
    body={`[pending](artifact:${artifactId}) [unknown](artifact:${unknown.id})`} />);
  expect(screen.queryByRole('button', { name: /Open saved file/ })).not.toBeInTheDocument();

  const capped = [file, ...Array.from({ length: 99 }, (_, index) => ({ ...file, id: `older-${index}`, sha256: 'b'.repeat(64) }))];
  rerender(<ChiefArtifactMessageText projectId="project-1" files={capped}
    body={`[hash](artifact:${sha}) [exact ID](artifact:${artifactId})`} />);
  expect(screen.queryByRole('button', { name: 'Open saved file hash' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Open saved file exact ID' })).toBeInTheDocument();
});

it('preserves public QA feedback and clears an open preview when its conversation changes', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => path.endsWith('/workspace-artifacts') ? json([saved]) : json({ ...saved, content: 'Verified final index text' })));
  const body = JSON.stringify({ verdict: 'needs_rework', feedback: `Fix the sources and inspect [final index](artifact:${sha}).`, reviewed_artifacts: [{ sha256: 'private' }] });
  const user = userEvent.setup();
  const { rerender } = render(<ChiefArtifactMessageText key={1} projectId="project-1" files={[file]} body={body} />);
  expect(screen.getByText(/Changes requested/)).toHaveTextContent('Fix the sources');
  expect(screen.queryByText('private')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open saved file final index' }));
  expect(await within(screen.getByLabelText('Saved file preview')).findByText('Verified final index text', { selector: 'p' })).toBeInTheDocument();
  rerender(<ChiefArtifactMessageText key={2} projectId="project-1" files={[file]} body={body} />);
  expect(screen.queryByLabelText('Saved file preview')).not.toBeInTheDocument();
});
