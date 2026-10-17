import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { Profile } from '../src/api';
import { ExternalCodePanel } from '../src/ExternalCodePanel';
import { WorkOrdersView } from '../src/WorkOrdersView';
import type { ExternalCodeDetail } from '../src/externalCodeApi';
import type { Run, WorkOrder } from '../src/workApi';

const projectId = 'project-1';
const stamp = '2026-09-26T12:00:00Z';
const revision = 'a'.repeat(40);
const engineer: Profile = { id: 'engineer-1', project_id: projectId, name: 'Engineer', kind: 'builtin', reports_to_profile_id: null, model: null,
  instructions: '', memory_scope: 'project', allowed_tools: [], autonomy_level: 'manual',
  configured: false, permissions_active: false };
const order: WorkOrder = { id: 'work-1', project_id: projectId, title: 'Check parser', instructions: 'Check local source',
  profile_id: engineer.id, status: 'ready', version: 1, block_reason: null, blocked_from_status: null, created_at: stamp,
  deliverable_kind: 'operator_review', acceptance_checks: [], parent_work_order_id: null, source_run_id: null, origin_signal_id: null, chain_depth: 0 };
const source = { id: 'source-1', project_id: projectId, kind: 'local', display_name: 'parser', root_id: 'workspace',
  relative_path: 'parser', account_id: null, owner: null, repo: null, branch: 'main', revision,
  availability: 'verified_at_selection' };
const run: Run = { id: 'run-1', project_id: projectId, work_order_id: order.id, work_order_title: order.title,
  work_order_instructions: order.instructions, profile_id: engineer.id, profile_model: null,
  status: 'running', mode: 'external_code', tokens_used: null, cost_usd: null,
  outcome_reason: '', started_at: stamp, finished_at: null };
const detail: ExternalCodeDetail = { run, attempts: [{ id: 'attempt-1', project_id: projectId, run_id: run.id,
  adapter_id: 'offline_probe', fixture_id: 'local_source_probe_v1', target_id: 'local_source_probe_v1',
  source_id: source.id, source_root_id: source.root_id, source_relative_path: source.relative_path,
  source_branch: source.branch, source_revision: revision, request_sha256: 'b'.repeat(64),
  status: 'queued', reason_code: null, provider_session_id: null, baseline_commit: revision,
  branch: 'main', commit: null, diff_sha256: null, tokens_used: null, cost_usd: null,
  receipt_sha256: null, created_at: stamp, started_at: null, finished_at: null, tests: [], artifacts: [] }] };
function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
afterEach(() => vi.unstubAllGlobals());

function eligibility(path: string) {
  if (path.endsWith('/synthetic-code/eligibility')) return json({ eligible: false, reason: 'fixture unavailable',
    fixture_id: 'engineer_qa_v1', engineer_profile_id: engineer.id, qa_profile_id: null });
  if (path.endsWith('/external-code/eligibility')) return json({ eligible: false, reason: 'No coding provider', adapters: [] });
  return null;
}

it('shows offline eligibility independently and blocks an unavailable probe', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith('/work-orders')) return json([order]);
    if (path.endsWith('/runs')) return json([]);
    if (path.endsWith('/sources')) return json([source]);
    if (path.endsWith('/offline-source-probe/eligibility')) return json({ eligible: false, reason: 'offline_probe_disabled' });
    return eligibility(path) ?? Promise.reject(new Error(`Unexpected ${path}`));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={[engineer]} onOpenRun={vi.fn()} />);
  await user.click(await screen.findByText('Diagnostics · offline local source check'));
  await user.selectOptions(await screen.findByRole('combobox', { name: 'Saved local source' }), source.id);
  expect(screen.getByText('Offline source check unavailable: offline_probe_disabled')).toBeInTheDocument();
  expect(screen.getByText(/Selected source:.*saved version/)).toBeInTheDocument();
  expect(screen.queryByText(new RegExp(revision))).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Run offline source probe' })).toBeDisabled();
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/offline-source-probe/runs'))).toBe(false);
});

it('queues the selected local source once and opens only a confirmed saved run', async () => {
  let savedOrder = order;
  let savedRun: Run | null = null;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/work-orders')) return json([savedOrder]);
    if (path === `/api/projects/${projectId}/runs`) return json(savedRun ? [savedRun] : []);
    if (path.endsWith('/sources')) return json([source]);
    if (path.endsWith('/offline-source-probe/eligibility')) return json({ eligible: true, reason: null });
    if (path.endsWith('/offline-source-probe/runs') && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ source_id: source.id });
      savedOrder = { ...order, status: 'running' };
      savedRun = run;
      return json(detail, 201);
    }
    if (path.endsWith(`/runs/${run.id}/external-code`)) return json(detail);
    return eligibility(path) ?? Promise.reject(new Error(`Unexpected ${path}`));
  });
  vi.stubGlobal('fetch', fetcher);
  const open = vi.fn(); const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={[engineer]} onOpenRun={open} />);
  await user.click(await screen.findByText('Diagnostics · offline local source check'));
  await user.selectOptions(await screen.findByRole('combobox', { name: 'Saved local source' }), source.id);
  await user.click(screen.getByRole('button', { name: 'Run offline source probe' }));
  await waitFor(() => expect(open).toHaveBeenCalledWith(run.id));
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/offline-source-probe/runs'))).toHaveLength(1);
});

it('does not offer a second launch when POST outcome and saved readback are unknown', async () => {
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/work-orders')) return json([order]);
    if (path.endsWith('/runs')) return json([]);
    if (path.endsWith('/sources')) return json([source]);
    if (path.endsWith('/offline-source-probe/eligibility')) return json({ eligible: true, reason: null });
    if (path.endsWith('/offline-source-probe/runs') && init?.method === 'POST') throw new Error('Connection interrupted');
    return eligibility(path) ?? Promise.reject(new Error(`Unexpected ${path}`));
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={[engineer]} onOpenRun={vi.fn()} />);
  await user.click(await screen.findByText('Diagnostics · offline local source check'));
  await user.selectOptions(await screen.findByRole('combobox', { name: 'Saved local source' }), source.id);
  await user.click(screen.getByRole('button', { name: 'Run offline source probe' }));
  expect(await screen.findByText(/No new offline probe run was found/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Run offline source probe' })).toBeDisabled();
});

it('labels an unknown offline receipt without implying a coding provider', async () => {
  const unknown = { ...detail, run: { ...run, status: 'unknown', outcome_reason: 'receipt_invalid' },
    attempts: [{ ...detail.attempts[0], status: 'unknown', reason_code: 'receipt_invalid' }] };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => path.endsWith(`/runs/${run.id}/external-code`)
    ? json(unknown) : Promise.reject(new Error(`Unexpected ${path}`))));
  render(<ExternalCodePanel projectId={projectId} runId={run.id} onTimelineRefresh={vi.fn()} onDetailChange={vi.fn()} />);
  expect(await screen.findByRole('heading', { name: 'Offline local source probe' })).toBeInTheDocument();
  expect(screen.getByText('Local source check · no coding provider session')).toBeInTheDocument();
  expect(screen.getByText(/offline source probe outcome is unknown/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Check runner receipt' })).not.toBeInTheDocument();
});
