import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { BrowserStaticPanel } from '../src/BrowserStaticPanel';
import type { BrowserJob } from '../src/browserStaticApi';
import type { RunEvent } from '../src/workApi';

const projectId = 'project-1';
const runId = 'run-1';
const fixture = {
  id: 'fixture-1', project_id: projectId, run_id: runId, approval_id: 'approval-1', actor_id: 'operator',
  approved_url: 'http://127.0.0.1:8765/check.html', source_sha256: 'a'.repeat(64), proposal_digest: 'b'.repeat(64),
  viewport_width: 800, viewport_height: 600, max_page_bytes: 8192, timeout_seconds: 10, lifetime_seconds: 120,
  status: 'pending', created_at: '2026-09-28T12:00:00Z', decided_at: null,
};
const job = {
  id: 'job-1', project_id: projectId, run_id: runId, fixture_id: fixture.id, actor_id: 'profile:qa-1',
  status: 'queued', reason_code: null, request_sha256: 'c'.repeat(64), binding_digest: 'd'.repeat(64),
  source_sha256: fixture.source_sha256, image_id: `sha256:${'e'.repeat(64)}`, receipt_sha256: null,
  created_at: '2026-09-28T12:01:00Z', finished_at: null,
};
const event: RunEvent = {
  id: 'event-1', project_id: projectId, work_order_id: 'work-1', run_id: runId,
  actor_id: 'operator', event_type: 'browser_static_fixture_proposed',
  payload: { fixture_id: fixture.id, approval_id: fixture.approval_id }, created_at: fixture.created_at,
};
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); });

it('proposes exact source once, reads back metadata, and clears source fields', async () => {
  let saved: typeof fixture | null = null;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs`) return json([]);
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-fixtures` && init?.method === 'POST') {
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      expect(body).toMatchObject({ approved_url: fixture.approved_url, html: '<h1>Private source</h1>', css: 'h1',
        viewport_width: 800, viewport_height: 600, max_page_bytes: 8192, timeout_seconds: 10, lifetime_seconds: 120 });
      expect(typeof body.idempotency_key).toBe('string');
      saved = fixture;
      return json(fixture, 201);
    }
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-fixtures/${fixture.id}`) return json(saved);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<BrowserStaticPanel projectId={projectId} runId={runId} events={[]} />);
  await user.click(screen.getByRole('button', { name: 'Browser fixture and evidence' }));
  await screen.findByText('No browser jobs recorded for this run.');
  await user.type(screen.getByLabelText('Approved loopback URL'), fixture.approved_url);
  await user.type(screen.getByLabelText('HTML source'), '<h1>Private source</h1>');
  await user.type(screen.getByLabelText('CSS source'), 'h1');
  for (const [label, value] of [['Viewport width', '800'], ['Viewport height', '600'], ['Maximum page bytes', '8192'],
    ['Timeout seconds', '10'], ['Lifetime seconds', '120']] as const) await user.type(screen.getByLabelText(label), value);
  await user.click(screen.getByRole('button', { name: 'Propose browser fixture' }));
  expect(await screen.findByText(/Browser check proposed/)).toBeInTheDocument();
  expect(screen.getByLabelText('Browser fixture proposals')).toHaveTextContent(fixture.approved_url);
  expect(screen.queryByText(fixture.source_sha256)).not.toBeInTheDocument();
  expect(screen.getByLabelText('HTML source')).toHaveValue('');
  expect(screen.getByLabelText('CSS source')).toHaveValue('');
  expect(screen.getByRole('button', { name: 'Propose browser fixture' })).toBeEnabled();
  expect(screen.queryByText('<h1>Private source</h1>')).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path, init]) => path.endsWith('/browser-static-fixtures') && init?.method === 'POST')).toHaveLength(1);
  expect(fetcher.mock.calls.some(([path]) => path.endsWith(`/browser-static-fixtures/${fixture.id}`))).toBe(true);
});

it('shows saved fixture and pending job, then reconciles and reads bounded evidence', async () => {
  let currentJob: BrowserJob = { ...job };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-fixtures/${fixture.id}`) return json(fixture);
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs`) return json([currentJob]);
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs/${job.id}` && !init?.method) return json(currentJob);
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs/${job.id}/reconcile` && init?.method === 'POST') {
      currentJob = { ...job, status: 'done', reason_code: 'inspection_complete', receipt_sha256: 'f'.repeat(64), finished_at: '2026-09-28T12:02:00Z' };
      return json(currentJob);
    }
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs/${job.id}/evidence`) return json({
      job_id: job.id, project_id: projectId, run_id: runId, status: 'done', title: 'Fixture page', text: 'Visible page text',
      blocked_request_count: 2, receipt_sha256: 'f'.repeat(64), finished_at: '2026-09-28T12:02:00Z',
    });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<BrowserStaticPanel projectId={projectId} runId={runId} events={[event]} />);
  await user.click(screen.getByRole('button', { name: 'Browser fixture and evidence' }));
  expect(await within(screen.getByLabelText('Browser fixture proposals')).findByText('Browser check')).toBeInTheDocument();
  expect(screen.queryByText(fixture.source_sha256)).not.toBeInTheDocument();
  const jobs = screen.getByLabelText('Browser jobs');
  expect(jobs).toHaveTextContent('queued');
  await user.click(within(jobs).getByRole('button', { name: 'Check job receipt' }));
  expect(await within(jobs).findByRole('button', { name: 'Read saved evidence' })).toBeInTheDocument();
  await user.click(within(jobs).getByRole('button', { name: 'Read saved evidence' }));
  expect(await within(jobs).findByText('Fixture page')).toBeInTheDocument();
  expect(within(jobs).getByText('Visible page text')).toBeInTheDocument();
  expect(fetcher.mock.calls.every(([path]) => String(path).startsWith(`/api/projects/${projectId}/runs/${runId}/`))).toBe(true);
});

it('leaves a queued job pending on 409 without another dispatch', async () => {
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs`) return json([job]);
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs/${job.id}`) return json(job);
    if (path.endsWith(`/browser-static-jobs/${job.id}/reconcile`) && init?.method === 'POST') return json({ detail: 'receipt pending' }, 409);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<BrowserStaticPanel projectId={projectId} runId={runId} events={[]} />);
  await user.click(screen.getByRole('button', { name: 'Browser fixture and evidence' }));
  await user.click(await screen.findByRole('button', { name: 'Check job receipt' }));
  expect(await screen.findByText(/receipt pending/)).toBeInTheDocument();
  expect(screen.getByLabelText('Browser jobs')).toHaveTextContent('queued');
  expect(fetcher.mock.calls.filter(([path, init]) => path.endsWith(`/browser-static-jobs/${job.id}/reconcile`) && init?.method === 'POST')).toHaveLength(1);
});

it('blocks a new fixture after an unknown submit outcome across reopening the panel', async () => {
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs`) return json([]);
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-fixtures` && init?.method === 'POST') throw new TypeError('network lost');
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const first = render(<BrowserStaticPanel projectId={projectId} runId={runId} events={[]} />);
  await user.click(screen.getByRole('button', { name: 'Browser fixture and evidence' }));
  await screen.findByText('No browser jobs recorded for this run.');
  await user.type(screen.getByLabelText('Approved loopback URL'), fixture.approved_url);
  await user.type(screen.getByLabelText('HTML source'), '<h1>Check</h1>');
  for (const [label, value] of [['Viewport width', '800'], ['Viewport height', '600'], ['Maximum page bytes', '8192'],
    ['Timeout seconds', '10'], ['Lifetime seconds', '120']] as const) await user.type(screen.getByLabelText(label), value);
  await user.click(screen.getByRole('button', { name: 'Propose browser fixture' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('outcome may be unknown');
  first.unmount();
  render(<BrowserStaticPanel projectId={projectId} runId={runId} events={[]} />);
  await user.click(screen.getByRole('button', { name: 'Browser fixture and evidence' }));
  await screen.findByText('No browser jobs recorded for this run.');
  expect(screen.queryByRole('button', { name: 'Propose browser fixture' })).not.toBeInTheDocument();
  expect(screen.getByText(/previous fixture submission has an unknown outcome/)).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path, init]) => path.endsWith('/browser-static-fixtures') && init?.method === 'POST')).toHaveLength(1);
});

it('explains the disabled static browser feature without offering a proposal', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs`) return json({ detail: 'Not Found' }, 404);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<BrowserStaticPanel projectId={projectId} runId={runId} events={[]} />);
  await user.click(screen.getByRole('button', { name: 'Browser fixture and evidence' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Static browser is not configured');
  expect(screen.queryByRole('button', { name: 'Propose browser fixture' })).not.toBeInTheDocument();
});

it('shows an unknown terminal job without offering another receipt check or evidence', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs`) return json([{
      ...job, status: 'unknown', reason_code: 'host_claim_missing', finished_at: '2026-09-28T12:02:00Z',
    }]);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<BrowserStaticPanel projectId={projectId} runId={runId} events={[]} />);
  await user.click(screen.getByRole('button', { name: 'Browser fixture and evidence' }));
  const jobs = await screen.findByLabelText('Browser jobs');
  expect(jobs).toHaveTextContent('unknown · host_claim_missing');
  expect(within(jobs).queryByRole('button', { name: 'Check job receipt' })).not.toBeInTheDocument();
  expect(within(jobs).queryByRole('button', { name: 'Read saved evidence' })).not.toBeInTheDocument();
});

it('dispatches an approved fixture as the operator once and confirms the exact key by readback', async () => {
  const approved = { ...fixture, status: 'approved', decided_at: '2026-09-28T12:00:30Z' };
  const dispatched = { ...job, actor_id: 'profile:qa-1' };
  let key = '';
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-fixtures/${fixture.id}`) return json(approved);
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs` && !init?.method) return json([]);
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs` && init?.method === 'POST') {
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      expect(Object.keys(body).sort()).toEqual(['fixture_id', 'idempotency_key']);
      expect(body.fixture_id).toBe(fixture.id);
      key = String(body.idempotency_key);
      return json(dispatched, 201);
    }
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs/by-key/${key}`) return json(dispatched);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<BrowserStaticPanel projectId={projectId} runId={runId} events={[event]} />);
  await user.click(screen.getByRole('button', { name: 'Browser fixture and evidence' }));
  await user.click(await screen.findByRole('button', { name: 'Dispatch for assigned Researcher or QA' }));
  expect(await screen.findByText(/Browser check dispatched/)).toBeInTheDocument();
  expect(screen.getByLabelText('Browser jobs')).not.toHaveTextContent(dispatched.id);
  expect(screen.queryByRole('button', { name: 'Dispatch for assigned Researcher or QA' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path, init]) => path.endsWith('/browser-static-jobs') && init?.method === 'POST')).toHaveLength(1);
});

it('checks an uncertain dispatch by the same key without sending a second job', async () => {
  const approved = { ...fixture, status: 'approved', decided_at: '2026-09-28T12:00:30Z' };
  let found = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-fixtures/${fixture.id}`) return json(approved);
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs` && !init?.method) return json([]);
    if (path === `/api/projects/${projectId}/runs/${runId}/browser-static-jobs` && init?.method === 'POST') throw new TypeError('connection dropped');
    if (path.includes('/browser-static-jobs/by-key/')) return found ? json(job) : json({ detail: 'Not Found' }, 404);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const first = render(<BrowserStaticPanel projectId={projectId} runId={runId} events={[event]} />);
  await user.click(screen.getByRole('button', { name: 'Browser fixture and evidence' }));
  await user.click(await screen.findByRole('button', { name: 'Dispatch for assigned Researcher or QA' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('outcome may be unknown');
  first.unmount();
  render(<BrowserStaticPanel projectId={projectId} runId={runId} events={[event]} />);
  await user.click(screen.getByRole('button', { name: 'Browser fixture and evidence' }));
  await user.click(await screen.findByRole('button', { name: 'Check saved dispatch' }));
  expect(await screen.findByText(/No job was found for the saved key/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Dispatch for assigned Researcher or QA' })).not.toBeInTheDocument();
  found = true;
  await user.click(screen.getByRole('button', { name: 'Check saved dispatch' }));
  expect(await screen.findByText(/Browser check dispatched/)).toBeInTheDocument();
  expect(screen.getByLabelText('Browser jobs')).not.toHaveTextContent(job.id);
  expect(fetcher.mock.calls.filter(([path, init]) => path.endsWith('/browser-static-jobs') && init?.method === 'POST')).toHaveLength(1);
});
