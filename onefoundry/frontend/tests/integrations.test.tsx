import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { IntegrationsView } from '../src/IntegrationsView';
import { confirmGitHubFixtureFetchProposal, proposeGitHubFixtureFetch } from '../src/sourceApi';

const id = 'project-1';
const revision = 'a'.repeat(40);
const source = { id: 'source-1', project_id: id, kind: 'github', display_name: 'team/repo', root_id: null,
  relative_path: null, account_id: 'account-1', owner: 'team', repo: 'repo', branch: 'main', revision,
  availability: 'verified_at_selection' };
const account = { id: 'account-1', project_id: id, label: 'Work GitHub', credential_present: true };
const discovery = { available: false, truncated: false, repositories: [] };
const repo = { full_name: 'team/repo', default_branch: 'main', private: true, html_url: 'https://github.com/team/repo' };
function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
afterEach(() => vi.unstubAllGlobals());

it('reuses one fixture fetch idempotency key after an uncertain POST response', async () => {
  const keys: string[] = [];
  const fetcher = vi.fn(async (_path: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)) as { idempotency_key: string };
    keys.push(body.idempotency_key);
    if (keys.length === 1) throw new TypeError('response lost');
    return json({ approval: { id: 'approval-v2', status: 'pending' },
      source: { project_id: id, source_id: source.id, kind: 'github_fixture_fetch_v2' },
      fetch_status: 'not_launched' }, 201);
  });
  vi.stubGlobal('fetch', fetcher);
  await expect(proposeGitHubFixtureFetch(id, 'run-1', source.id)).rejects.toThrow();
  expect(await proposeGitHubFixtureFetch(id, 'run-1', source.id)).toBe('approval-v2');
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  confirmGitHubFixtureFetchProposal(id, 'run-1', source.id);
  expect(await proposeGitHubFixtureFetch(id, 'run-1', source.id)).toBe('approval-v2');
  expect(keys[2]).not.toBe(keys[1]);
  confirmGitHubFixtureFetchProposal(id, 'run-1', source.id);
});

it('shows unavailable local roots and truthful readiness without touching a provider', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path === '/api/source-roots') return json([]);
    if (path === '/api/source-roots/workspace/repositories') return json(discovery);
    if (path === `/api/projects/${id}/sources` || path === `/api/projects/${id}/github/accounts`) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<IntegrationsView projectId={id} />);
  expect(await screen.findByText(/No local repository is mounted. This is optional for manual projects/)).toBeInTheDocument();
  expect(screen.getByText('python3 scripts/local_repositories.py add /absolute/path/to/repository')).toBeInTheDocument();
  expect(screen.getByText(/separate from a GitHub Copilot model connection/)).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'MCP & plugins' })).not.toBeInTheDocument();
  expect(screen.getByText(/A saved repository or model account alone does not make a coding backend ready/)).toBeInTheDocument();
  expect(fetcher.mock.calls).toHaveLength(4);
});

it('selects a discovered local repository and confirms the saved revision', async () => {
  const local = { ...source, id: 'local-1', kind: 'local', display_name: 'studio', root_id: 'workspace',
    relative_path: 'studio', account_id: null, owner: null, repo: null };
  const saved: typeof local[] = [];
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/source-roots') return json([{ id: 'workspace', label: 'Allowed workspace', available: true }]);
    if (path === '/api/source-roots/workspace/repositories') return json({ available: true, truncated: false,
      repositories: [{ relative_path: 'studio', display_name: 'studio', branch: 'main', revision, dirty: false }] });
    if (path === `/api/projects/${id}/sources/local` && init?.method === 'POST') {
      expect(JSON.parse(String(init.body))).toEqual({ root_id: 'workspace', relative_path: 'studio' });
      saved.push(local); return json(local, 201);
    }
    if (path === `/api/projects/${id}/sources`) return json(saved);
    if (path === `/api/projects/${id}/github/accounts`) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); render(<IntegrationsView projectId={id} />);
  await screen.findByRole('option', { name: 'studio · main' });
  await user.selectOptions(screen.getByLabelText('Discovered repository'), 'studio');
  await user.click(screen.getByRole('button', { name: 'Add local repository' }));
  expect(await screen.findByText('Local repository saved and confirmed.')).toBeInTheDocument();
  expect(screen.getByText(/Verified at selection; currentness has not been rechecked/)).toBeInTheDocument();
});

it('explains the mounted-root boundary and accepts the root repository without sending an absolute host path', async () => {
  const local = { ...source, id: 'local-root', kind: 'local', display_name: 'districtAI', root_id: 'workspace',
    relative_path: '.', account_id: null, owner: null, repo: null };
  const saved: typeof local[] = [];
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/source-roots') return json([{ id: 'workspace', label: 'Configured local repository root', available: true }]);
    if (path === '/api/source-roots/workspace/repositories') return json({ available: true, truncated: false,
      repositories: [{ relative_path: 'studio', display_name: 'studio', branch: 'main', revision, dirty: false }] });
    if (path === `/api/projects/${id}/sources/local` && init?.method === 'POST') {
      expect(JSON.parse(String(init.body))).toEqual({ root_id: 'workspace', relative_path: '.' });
      saved.push(local); return json(local, 201);
    }
    if (path === `/api/projects/${id}/sources`) return json(saved);
    if (path === `/api/projects/${id}/github/accounts`) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); render(<IntegrationsView projectId={id} />);
  await user.click(await screen.findByRole('button', { name: 'Enter path manually' }));
  const path = screen.getByLabelText('Repository path within root');
  await user.type(path, '/home/ken/districtAI');
  expect(screen.getByRole('alert')).toHaveTextContent('Absolute paths like /home/... cannot be used here.');
  expect(screen.getByRole('button', { name: 'Add local repository' })).toBeDisabled();
  expect(fetcher.mock.calls.some(([requestPath]) => requestPath === `/api/projects/${id}/sources/local`)).toBe(false);
  await user.clear(path);
  await user.type(path, '.');
  await user.click(screen.getByRole('button', { name: 'Add local repository' }));
  expect(await screen.findByText('Local repository saved and confirmed.')).toBeInTheDocument();
});

it('explicitly loads bounded GitHub repos, saves source, removes it, then disconnects account', async () => {
  const accounts = [account]; const sources: typeof source[] = [];
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/source-roots') return json([]);
    if (path === '/api/source-roots/workspace/repositories') return json(discovery);
    if (path === `/api/projects/${id}/sources`) return json(sources);
    if (path === `/api/projects/${id}/github/accounts`) return json(accounts);
    if (path === `/api/projects/${id}/github/accounts/account-1/repos`) return json([repo]);
    if (path === `/api/projects/${id}/sources/github` && init?.method === 'POST') {
      expect(JSON.parse(String(init.body))).toEqual({ account_id: 'account-1', owner: 'team', repo: 'repo', branch: 'main' });
      sources.push(source); return json(source, 201);
    }
    if (path === `/api/projects/${id}/sources/source-1` && init?.method === 'DELETE') {
      sources.length = 0; return new Response(null, { status: 204 });
    }
    if (path === `/api/projects/${id}/github/accounts/account-1` && init?.method === 'DELETE') {
      accounts.length = 0; return new Response(null, { status: 204 });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); render(<IntegrationsView projectId={id} />);
  await screen.findByLabelText('Saved connection');
  expect(fetcher.mock.calls.some(([path]) => String(path).endsWith('/repos'))).toBe(false);
  await user.click(screen.getByRole('button', { name: 'Load repositories' }));
  expect(await screen.findByText(/Showing up to 50 repositories/)).toBeInTheDocument();
  await user.type(screen.getByLabelText('Repository (owner/repo)'), 'team/repo');
  expect((screen.getByLabelText('Branch') as HTMLInputElement).value).toBe('main');
  await user.click(screen.getByRole('button', { name: 'Select repository' }));
  expect(await screen.findByText('GitHub repository saved at its confirmed revision.')).toBeInTheDocument();
  const selected = screen.getByRole('heading', { name: 'Selected repositories' }).closest('section')!;
  await user.click(within(selected).getByRole('button', { name: 'Remove repository' }));
  await user.click(within(selected).getByRole('button', { name: 'Remove repository' }));
  expect(await screen.findByText('Repository removed and confirmed.')).toBeInTheDocument();
  const connections = screen.getByRole('list', { name: 'Saved GitHub connections' });
  await user.click(within(connections).getByRole('button', { name: 'Disconnect' }));
  await user.click(within(connections).getByRole('button', { name: 'Disconnect' }));
  await waitFor(() => expect(accounts).toHaveLength(0));
  expect(await screen.findByText('GitHub connection removed and confirmed.')).toBeInTheDocument();
});

it('clears token after submit and does not render saved credential text', async () => {
  const accounts: typeof account[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/source-roots') return json([]);
    if (path === '/api/source-roots/workspace/repositories') return json(discovery);
    if (path === `/api/projects/${id}/sources`) return json([]);
    if (path === `/api/projects/${id}/github/accounts` && init?.method === 'POST') {
      expect(JSON.parse(String(init.body))).toEqual({ label: 'Work GitHub', token: 'test-token' });
      accounts.push(account); return json(account, 201);
    }
    if (path === `/api/projects/${id}/github/accounts`) return json(accounts);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup(); render(<IntegrationsView projectId={id} />);
  await screen.findByLabelText('GitHub token');
  await user.type(screen.getByLabelText('Connection label'), 'Work GitHub');
  await user.type(screen.getByLabelText('GitHub token'), 'test-token');
  await user.click(screen.getByRole('button', { name: 'Save GitHub connection' }));
  expect(await screen.findByText('GitHub token saved; use Load repositories to verify access.')).toBeInTheDocument();
  expect((screen.getByLabelText('GitHub token') as HTMLInputElement).value).toBe('');
  expect(document.body.textContent).not.toContain('test-token');
});

it('switches projects without carrying a selected source or account into the next project', async () => {
  const other = 'project-2';
  const fetcher = vi.fn(async (path: string) => {
    if (path === '/api/source-roots') return json([]);
    if (path === '/api/source-roots/workspace/repositories') return json(discovery);
    if (path === `/api/projects/${id}/sources`) return json([source]);
    if (path === `/api/projects/${id}/github/accounts`) return json([account]);
    if (path === `/api/projects/${other}/sources` || path === `/api/projects/${other}/github/accounts`) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const view = render(<IntegrationsView key={id} projectId={id} />);
  expect(await screen.findByText('team/repo')).toBeInTheDocument();
  view.rerender(<IntegrationsView key={other} projectId={other} />);
  expect(await screen.findByText('No repository selected yet. Select a local or GitHub source above.')).toBeInTheDocument();
  expect(screen.queryByText('team/repo')).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => path === `/api/projects/${other}/sources`)).toBe(true);
});

it('proposes a pinned GitHub stage from a running manual work order without launching fetch', async () => {
  const run = { id: 'run-1', project_id: id, work_order_id: 'work-1', profile_id: 'engineer-1',
    work_order_title: 'Review source', work_order_instructions: 'Review staging', profile_model: null,
    status: 'running', mode: 'manual_planning', tokens_used: 0, cost_usd: 0,
    outcome_reason: '', started_at: '2026-09-27T00:00:00Z', finished_at: null };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/source-roots') return json([]);
    if (path === '/api/source-roots/workspace/repositories') return json(discovery);
    if (path === `/api/projects/${id}/sources`) return json([source]);
    if (path === `/api/projects/${id}/github/accounts`) return json([account]);
    if (path === `/api/projects/${id}/runs`) return json([run]);
    if (path === `/api/projects/${id}/tools/github/stage-proposals` && init?.method === 'POST') {
      expect(JSON.parse(String(init.body))).toEqual({ run_id: 'run-1', source_id: source.id });
      return json({ approval: { id: 'approval-1', status: 'pending' },
        source: { project_id: id, source_id: source.id }, stage_status: 'not_launched' }, 201);
    }
    if (path === `/api/projects/${id}/tools/github/stage-proposals/approval-1`) {
      return json({ approval_id: 'approval-1', decision: 'pending', source_current: true,
        stage_status: 'not_launched', mode: 'offline_synthetic_fixture', attempt_id: null,
        receipt_sha256: null, source_tree: null, file_count: null, total_bytes: null, reason_code: null,
        source: { kind: 'github_stage_v1', project_id: id,
          source_id: source.id, account_id: account.id, owner: source.owner, repo: source.repo,
          branch: source.branch, saved_revision: revision } });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); render(<IntegrationsView projectId={id} />);
  await screen.findByText('team/repo');
  await user.click(screen.getByRole('button', { name: 'Load manual work orders' }));
  await user.selectOptions(screen.getByLabelText('Manual work order for staging proposal'), 'run-1');
  await user.click(screen.getByRole('button', { name: 'Propose exact stage' }));
  expect(await screen.findByText(/Stage status: not launched/)).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => String(path).includes('github.com'))).toBe(false);
});
