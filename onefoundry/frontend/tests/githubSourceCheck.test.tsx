import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { GitHubSourceCheck } from '../src/GitHubSourceCheck';
import type { ProjectSource } from '../src/sourceApi';

const revision = 'a'.repeat(40);
const source: ProjectSource = { id: 'github-1', project_id: 'project-1', kind: 'github', display_name: 'org/service', root_id: null,
  relative_path: null, account_id: 'account-1', owner: 'org', repo: 'service', branch: 'main', revision, availability: 'available' };
const result = { project_id: 'project-1', source_id: source.id, owner: 'org', repo: 'service', branch: 'main', saved_revision: revision, current_revision: revision, matches_saved_revision: true, checked_at: '2026-10-03T12:00:00Z' };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
afterEach(() => vi.unstubAllGlobals());

it('checks only on explicit request, keeps exact binding in the API, and shows no hashes', async () => {
  const fetcher = vi.fn(async () => json(result)); vi.stubGlobal('fetch', fetcher);
  render(<GitHubSourceCheck projectId="project-1" source={source} />);
  expect(fetcher).not.toHaveBeenCalled();
  await userEvent.setup().click(screen.getByRole('button', { name: 'Check current GitHub revision' }));
  expect(await screen.findByRole('status')).toHaveTextContent('The saved version is current.');
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(screen.queryByText(revision)).not.toBeInTheDocument();
});

it('preserves failure and rejects mismatched repository metadata', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(json({ detail: 'GitHub is unavailable' }, 503)).mockResolvedValueOnce(json({ ...result, owner: 'different' }));
  vi.stubGlobal('fetch', fetcher); render(<GitHubSourceCheck projectId="project-1" source={source} />);
  const user = userEvent.setup(); await user.click(screen.getByRole('button', { name: 'Check current GitHub revision' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('GitHub is unavailable');
  await user.click(screen.getByRole('button', { name: 'Check current GitHub revision' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('did not match');
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it('does not carry an old project result into a new selection', async () => {
  let resolve!: (value: Response) => void;
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((done) => { resolve = done; })));
  const view = render(<GitHubSourceCheck projectId="project-1" source={source} />);
  await userEvent.setup().click(screen.getByRole('button', { name: 'Check current GitHub revision' }));
  view.rerender(<GitHubSourceCheck projectId="project-2" source={{ ...source, id: 'github-2', project_id: 'project-2' }} />);
  await act(async () => resolve(json(result)));
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Check current GitHub revision' })).toBeEnabled();
});

it('rejects a response for another project and reports changed versions without exposing them', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ ...result, project_id: 'project-2' }))
    .mockResolvedValueOnce(json({ ...result, current_revision: 'b'.repeat(40), matches_saved_revision: false })));
  render(<GitHubSourceCheck projectId="project-1" source={source} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: 'Check current GitHub revision' }));
  expect(await screen.findByRole('alert')).toBeInTheDocument();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Check current GitHub revision' }));
  expect(await screen.findByRole('status')).toHaveTextContent('This branch differs from the saved version.');
  expect(screen.queryByText('b'.repeat(40))).not.toBeInTheDocument();
});
