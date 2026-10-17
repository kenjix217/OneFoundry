import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { Profile } from '../src/api';
import { ExternalCodePanel } from '../src/ExternalCodePanel';
import type { ExternalCodeDetail } from '../src/externalCodeApi';
import { WorkOrdersView } from '../src/WorkOrdersView';
import type { Run, WorkOrder } from '../src/workApi';

const projectId = 'project-1';
const stamp = '2026-09-26T12:00:00Z';
const engineer: Profile = { id: 'engineer-1', project_id: projectId, name: 'Engineer', kind: 'builtin', reports_to_profile_id: null, model: null,
  instructions: '', memory_scope: 'project', allowed_tools: [], autonomy_level: 'manual',
  configured: false, permissions_active: false };
const order: WorkOrder = { id: 'work-1', project_id: projectId, title: 'Implement feature',
  instructions: 'Work only in disposable fixture', profile_id: engineer.id, status: 'ready', version: 1,
  deliverable_kind: 'operator_review', acceptance_checks: [], parent_work_order_id: null, source_run_id: null, origin_signal_id: null, chain_depth: 0,
  block_reason: null, blocked_from_status: null, created_at: stamp };
const run: Run = { id: 'run-1', project_id: projectId, work_order_id: order.id,
  work_order_title: order.title, work_order_instructions: order.instructions,
  profile_id: engineer.id, profile_model: null, status: 'running', mode: 'external_code',
  tokens_used: null, cost_usd: null, outcome_reason: '', started_at: stamp, finished_at: null };
const detail: ExternalCodeDetail = { run, attempts: [{ id: 'attempt-1', project_id: projectId, run_id: run.id,
  adapter_id: 'codex_cli', fixture_id: 'engineer_qa_v1', target_id: 'engineer_qa_v1',
  source_id: null, source_root_id: null, source_relative_path: null, source_branch: null,
  source_revision: null, request_sha256: 'b'.repeat(64), status: 'queued', reason_code: null,
  provider_session_id: null, baseline_commit: null, branch: null, commit: null, diff_sha256: null,
  tokens_used: null, cost_usd: null, receipt_sha256: null, created_at: stamp, started_at: null,
  finished_at: null, tests: [], artifacts: [] }] };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('shows both coding backends and blocks launch when server says unavailable', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith('/work-orders')) return json([order]);
    if (path.endsWith('/runs')) return json([]);
    if (path.endsWith('/synthetic-code/eligibility')) return json({ eligible: false, reason: 'fixture unavailable',
      fixture_id: 'engineer_qa_v1', engineer_profile_id: engineer.id, qa_profile_id: null });
    if (path.endsWith('/external-code/eligibility')) return json({ eligible: false, reason: 'Runner isolation not configured',
      adapters: [{ adapter_id: 'codex_cli', available: false, reason: 'Codex CLI authentication unavailable' },
        { adapter_id: 'claude_code', available: false, reason: 'Claude Code binary unavailable' }] });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={[engineer]} onOpenRun={vi.fn()} />);
  const selector = await screen.findByRole('combobox', { name: 'Coding backend' });
  await user.selectOptions(selector, 'codex_cli');
  expect(screen.getByText('External coding unavailable: Runner isolation not configured')).toBeInTheDocument();
  expect(screen.getByText('Codex CLI: Codex CLI authentication unavailable')).toBeInTheDocument();
  expect(screen.getByText('Claude Code: Claude Code binary unavailable')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Run with coding agent' })).toBeDisabled();
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/external-code/runs'))).toBe(false);
});

it('launches the explicitly selected adapter once and confirms its saved attempt', async () => {
  let savedOrder = { ...order };
  let savedRun: Run | null = null;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/work-orders')) return json([savedOrder]);
    if (path === `/api/projects/${projectId}/runs`) return json(savedRun ? [savedRun] : []);
    if (path.endsWith('/synthetic-code/eligibility')) return json({ eligible: false, reason: 'fixture unavailable',
      fixture_id: 'engineer_qa_v1', engineer_profile_id: engineer.id, qa_profile_id: null });
    if (path.endsWith('/external-code/eligibility')) return json({ eligible: true, reason: null,
      adapters: [{ adapter_id: 'codex_cli', available: true, reason: null },
        { adapter_id: 'claude_code', available: false, reason: 'Not configured' }] });
    if (path.endsWith('/external-code/runs') && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ adapter_id: 'codex_cli' });
      savedOrder = { ...order, status: 'running' };
      savedRun = run;
      return json(detail, 201);
    }
    if (path === `/api/projects/${projectId}/runs/${run.id}/external-code`) return json(detail);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const openRun = vi.fn();
  render(<WorkOrdersView projectId={projectId} profiles={[engineer]} onOpenRun={openRun} />);
  await user.selectOptions(await screen.findByRole('combobox', { name: 'Coding backend' }), 'codex_cli');
  await user.click(screen.getByRole('button', { name: 'Run with coding agent' }));
  await waitFor(() => expect(openRun).toHaveBeenCalledWith(run.id));
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/external-code/runs'))).toHaveLength(1);
});

it('shows saved session and unknown outcome without offering receipt import again', async () => {
  const unknown: ExternalCodeDetail = { run: { ...run, status: 'unknown', outcome_reason: 'receipt_invalid' },
    attempts: [{ ...detail.attempts[0], status: 'unknown', provider_session_id: 'session-1',
      reason_code: 'receipt_invalid', tokens_used: null, cost_usd: null }] };
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith(`/runs/${run.id}/external-code`)) return json(unknown);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<ExternalCodePanel projectId={projectId} runId={run.id} onTimelineRefresh={vi.fn()} onDetailChange={vi.fn()} />);
  expect(await screen.findByText('Provider session recorded')).toBeInTheDocument();
  expect(screen.getByText('Reason: receipt invalid')).toBeInTheDocument();
  expect(screen.getByText(/The coding outcome is unknown/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Check runner receipt' })).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it('shows reported tests, usage, and a saved diff preview for a completed attempt', async () => {
  const completed: ExternalCodeDetail = { run: { ...run, status: 'done', tokens_used: 42, cost_usd: 0.01 },
    attempts: [{ ...detail.attempts[0], status: 'done', provider_session_id: 'session-2',
      tokens_used: 42, cost_usd: '0.01' as unknown as number,
      tests: [{ name: 'pytest', status: 'passed', exit_code: 0 }],
      diff_sha256: 'a'.repeat(64), artifacts: [{ id: 'artifact-1', kind: 'diff',
        path: 'artifacts/attempt-1/change.diff', sha256: 'a'.repeat(64), byte_size: 12 }] }] };
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith(`/runs/${run.id}/external-code`)) return json(completed);
    if (path.endsWith('/external-code/artifacts/artifact-1')) return json({ kind: 'diff',
      sha256: 'a'.repeat(64), byte_size: 12, text: '+new change\n' });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ExternalCodePanel projectId={projectId} runId={run.id} onTimelineRefresh={vi.fn()} onDetailChange={vi.fn()} />);
  expect(await screen.findByText('pytest: passed · exit 0')).toBeInTheDocument();
  expect(screen.getByText('Usage: 42 tokens · $0.0100')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Preview diff' }));
  expect(await screen.findByLabelText('Verified external diff')).toHaveTextContent('+new change');
});

it('renders an offline receipt when PostgreSQL serializes zero cost in exponent form', async () => {
  const completed: ExternalCodeDetail = { run: { ...run, status: 'done', tokens_used: 0, cost_usd: 0 },
    attempts: [{ ...detail.attempts[0], adapter_id: 'offline_probe', fixture_id: 'local_source_probe_v1',
      target_id: 'local_source_probe_v1', source_id: 'source-1', source_root_id: 'workspace',
      source_relative_path: 'calculator', source_branch: 'main', source_revision: 'a'.repeat(40),
      status: 'done', tokens_used: 0, cost_usd: '0E-10' as unknown as number,
      tests: [{ name: 'git_diff_check', status: 'passed', exit_code: 0 }] }] };
  vi.stubGlobal('fetch', vi.fn(async () => json(completed)));
  render(<ExternalCodePanel projectId={projectId} runId={run.id} onTimelineRefresh={vi.fn()} onDetailChange={vi.fn()} />);
  expect(await screen.findByText('Offline local source probe · done')).toBeInTheDocument();
  expect(screen.getByText('git_diff_check: passed · exit 0')).toBeInTheDocument();
  expect(screen.getByText('No model or coding provider usage')).toBeInTheDocument();
});
