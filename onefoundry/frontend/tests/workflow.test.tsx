vi.mock('../src/taskWorkflowApi', () => ({
  activateTeam: vi.fn(),
  getTeamReadiness: vi.fn(async (_project: string, order: string) => ({
    work_order_id: order, status: 'draft', execution_mode: 'legacy', worker_enabled: false,
    provider: null, ready_to_activate: false, blockers: ['The team worker is disabled.'],
    pending_approvals: 0, queued_attempts: 0, running_attempts: 0, confirmed_results: 0,
    next_step: 'Review configured execution in Team.',
  })),
}));

import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { Profile } from '../src/api';
import { ApprovalInbox } from '../src/ApprovalInbox';
import { RunTimelineView } from '../src/RunTimelineView';
import { WorkOrdersView } from '../src/WorkOrdersView';
import { getRun, markWorkReady, type Approval, type Run, type RunEvent, type WorkOrder } from '../src/workApi';

const projectId = 'project-1';
const timestamp = '2026-09-25T12:00:00Z';
const profiles: Profile[] = [
  { id: 'engineer-1', project_id: projectId, name: 'Engineer', kind: 'builtin', reports_to_profile_id: null, model: null,
    instructions: '', memory_scope: 'company', allowed_tools: [], autonomy_level: 'manual',
    configured: false, permissions_active: false },
  { id: 'qa-1', project_id: projectId, name: 'QA', kind: 'builtin', reports_to_profile_id: null, model: null,
    instructions: '', memory_scope: 'company', allowed_tools: [], autonomy_level: 'manual',
    configured: false, permissions_active: false },
];
const baseOrder: WorkOrder = { id: 'work-1', project_id: projectId, title: 'Plan a test pass',
  instructions: 'Review the fixture', profile_id: 'engineer-1', status: 'draft',
  deliverable_kind: 'operator_review', acceptance_checks: [], parent_work_order_id: null, source_run_id: null, origin_signal_id: null, chain_depth: 0,
  version: 1, block_reason: null, blocked_from_status: null, created_at: timestamp };
const baseRun: Run = { id: 'run-1', project_id: projectId, work_order_id: baseOrder.id,
  work_order_title: baseOrder.title, work_order_instructions: baseOrder.instructions,
  profile_id: 'engineer-1', profile_model: null, status: 'running', mode: 'manual_planning',
  tokens_used: 0, cost_usd: 0, outcome_reason: '', started_at: timestamp, finished_at: null };
const preRunEvent: RunEvent = { id: 'event-1', project_id: projectId, work_order_id: baseOrder.id,
  run_id: null, actor_id: 'operator', event_type: 'work_order_created',
  payload: { status: 'draft' }, created_at: timestamp };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('creates work in a dismissible dialog without replacing the queue or its filters', async () => {
  const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
    if (options?.method === 'POST') throw new Error('Cancel must not create work');
    if (path.endsWith('/work-orders')) return json([baseOrder]);
    if (path.endsWith('/runs') || path.includes('/approvals?')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={profiles} onOpenRun={vi.fn()} onOpenTask={vi.fn()} />);
  await screen.findByRole('heading', { name: baseOrder.title });
  await user.type(screen.getByRole('searchbox'), 'test pass');
  await user.click(screen.getByRole('button', { name: '+ New work order' }));
  const dialog = screen.getByRole('dialog', { name: 'Create work order' });
  expect(within(dialog).getByRole('button', { name: 'Create draft' })).toBeEnabled();
  await user.type(within(dialog).getByPlaceholderText('A clear outcome for this work'), 'Unsaved task');
  await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('searchbox')).toHaveValue('test pass');
  expect(screen.getByRole('heading', { name: baseOrder.title })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '+ New work order' }));
  expect(screen.getByPlaceholderText('A clear outcome for this work')).toHaveValue('');
  expect(screen.getByPlaceholderText('A clear outcome for this work')).toHaveFocus();
  await user.type(screen.getByPlaceholderText('A clear outcome for this work'), '  ');
  await user.type(screen.getByPlaceholderText('Describe the task and any boundaries'), '  ');
  await user.selectOptions(screen.getByLabelText('Assign to profile'), 'engineer-1');
  await user.click(screen.getByRole('button', { name: 'Create draft' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Add a title, instructions, and an assigned agent.');
  expect(fetcher.mock.calls.every(([, options]) => options?.method !== 'POST')).toBe(true);
});

it('surfaces a rejected work transition without retrying the write', async () => {
  const fetcher = vi.fn().mockResolvedValue(json({ detail: 'work order is not draft' }, 409));
  vi.stubGlobal('fetch', fetcher);
  await expect(markWorkReady(projectId, baseOrder.id)).rejects.toMatchObject({
    status: 409, message: 'work order is not draft',
  });
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it('dispatches an approved GitHub proposal to a synthetic fixture once and reads back status', async () => {
  const approval: Approval = {
    id: 'stage-approval', project_id: projectId, work_order_id: baseOrder.id,
    run_id: baseRun.id, action_type: 'other', target: `team/repo:main@${'a'.repeat(40)}`,
    content: JSON.stringify({ kind: 'github_stage_v1', project_id: projectId,
      source_id: 'source-1', account_id: 'account-1', owner: 'team', repo: 'repo',
      branch: 'main', saved_revision: 'a'.repeat(40) }), proposal_digest: 'digest',
    status: 'pending', created_at: timestamp, decided_at: null,
    synthetic_merge: false, synthetic_sales: false, github_stage: true,
  };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs`) return json([baseRun]);
    if (path.startsWith(`/api/projects/${projectId}/approvals?status=`)) {
      return json(approval.status === new URL(`http://localhost${path}`).searchParams.get('status') ? [approval] : []);
    }
    if (path === `/api/projects/${projectId}/approvals/${approval.id}/decision` && init?.method === 'POST') {
      approval.status = 'approved'; approval.decided_at = timestamp;
      return json(approval);
    }
    if (path === `/api/projects/${projectId}/tools/github/stage-proposals/${approval.id}`) {
      return json(stageStatus);
    }
    if (path === `/api/projects/${projectId}/tools/github/stage-proposals/${approval.id}/dispatch` && init?.method === 'POST') {
      stageStatus = { ...stageStatus, stage_status: 'queued', attempt_id: 'attempt-1' };
      return json(stageStatus);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  let stageStatus = { approval_id: approval.id, decision: 'approved', source_current: true,
    stage_status: 'not_launched', mode: 'offline_synthetic_fixture', attempt_id: null as string | null,
    receipt_sha256: null, source_tree: null, file_count: null, total_bytes: null, reason_code: null,
    source: JSON.parse(approval.content) as Record<string, unknown> };
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ApprovalInbox projectId={projectId} />);
  expect(await screen.findByText(/approval authorizes a separate synthetic local fixture stage attempt/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Approve future exact stage' }));
  expect(await screen.findByText(/Check synthetic offline stage status before dispatch/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'approved' }));
  expect(await screen.findByRole('button', { name: 'Check synthetic offline stage status' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Start synthetic offline stage' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Check synthetic offline stage status' }));
  expect(await screen.findByRole('button', { name: 'Start synthetic offline stage' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Start synthetic offline stage' }));
  expect(await screen.findByText('Synthetic local fixture stage: queued. No live GitHub fetch.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Start synthetic offline stage' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path, init]) =>
    String(path).endsWith(`/approvals/${approval.id}/decision`) && init?.method === 'POST')).toHaveLength(1);
  expect(fetcher.mock.calls.filter(([path, init]) =>
    String(path).endsWith(`/stage-proposals/${approval.id}/dispatch`) && init?.method === 'POST')).toHaveLength(1);
});

it('shows a separate synthetic HTTPS approval and its verification-only receipt', async () => {
  const approval: Approval = {
    id: 'fetch-approval', project_id: projectId, work_order_id: baseOrder.id,
    run_id: baseRun.id, action_type: 'other', target: `team/repo:main@${'a'.repeat(40)}`,
    content: JSON.stringify({ kind: 'github_fixture_fetch_v2', action: 'verify_local_https_fixture' }),
    proposal_digest: 'digest', status: 'pending', created_at: timestamp, decided_at: null,
    synthetic_merge: false, synthetic_sales: false, github_stage: false, github_fetch: true,
  };
  let status = { approval_id: approval.id, decision: 'approved', source_current: true,
    fetch_status: 'not_launched', mode: 'synthetic_local_https_verification_only',
    action: 'verify_local_https_fixture', expires_at: '2026-09-28T00:00:00Z', expired: false,
    idempotency_key: 'key-1', attempt_id: null as string | null, receipt_sha256: null,
    source_revision: null, source_tree: null, file_count: null, total_bytes: null, reason_code: null,
    source: { kind: 'github_fixture_fetch_v2', project_id: projectId, source_id: 'source-1',
      account_id: 'account-1', owner: 'team', repo: 'repo', branch: 'main', saved_revision: 'a'.repeat(40) },
  };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs`) return json([baseRun]);
    if (path.startsWith(`/api/projects/${projectId}/approvals?status=`)) {
      return json(approval.status === new URL(`http://localhost${path}`).searchParams.get('status') ? [approval] : []);
    }
    if (path === `/api/projects/${projectId}/approvals/${approval.id}/decision` && init?.method === 'POST') {
      approval.status = 'approved'; approval.decided_at = timestamp;
      return json(approval);
    }
    if (path === `/api/projects/${projectId}/tools/github/fixture-fetch-v2/${approval.id}`) return json(status);
    if (path === `/api/projects/${projectId}/tools/github/fixture-fetch-v2/${approval.id}/dispatch` && init?.method === 'POST') {
      status = { ...status, fetch_status: 'queued', attempt_id: 'attempt-v2' };
      return json(status);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); render(<ApprovalInbox projectId={projectId} />);
  expect(await screen.findByText(/Version 2 synthetic HTTPS fixture verification only/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Approve synthetic HTTPS verification' }));
  await user.click(screen.getByRole('button', { name: 'approved' }));
  await user.click(await screen.findByRole('button', { name: 'Check synthetic HTTPS verification' }));
  await user.click(await screen.findByRole('button', { name: 'Start synthetic HTTPS verification' }));
  expect(await screen.findByText('Synthetic HTTPS fixture verification: queued. No real GitHub fetch or coding checkout.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Start synthetic HTTPS verification' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path]) => String(path).includes('github.com'))).toHaveLength(0);
});

it('keeps v3 snapshot approval separate and shows its durable local CAS receipt', async () => {
  const approval: Approval = {
    id: 'snapshot-approval', project_id: projectId, work_order_id: baseOrder.id,
    run_id: baseRun.id, action_type: 'other', target: `team/repo:main@${'a'.repeat(40)}`,
    content: JSON.stringify({ kind: 'github_fixture_snapshot_v3', action: 'materialize_local_https_fixture_snapshot' }),
    proposal_digest: 'digest', status: 'pending', created_at: timestamp, decided_at: null,
    synthetic_merge: false, synthetic_sales: false, github_stage: false, github_fetch: false,
    github_snapshot: true,
  };
  let status = { approval_id: approval.id, decision: 'approved', source_current: true,
    snapshot_status: 'not_launched', mode: 'synthetic_local_https_snapshot_fixture_asserted_commit',
    action: 'materialize_local_https_fixture_snapshot', expires_at: '2026-09-28T00:00:00Z', expired: false,
    idempotency_key: 'key-3', attempt_id: null as string | null, receipt_sha256: null,
    source_revision: null, source_tree: null, file_count: null, total_bytes: null, reason_code: null,
    snapshot_path: null, snapshot_sha256: null, snapshot_byte_size: null,
    source: { kind: 'github_fixture_snapshot_v3', project_id: projectId, source_id: 'source-1',
      account_id: 'account-1', owner: 'team', repo: 'repo', branch: 'main', saved_revision: 'a'.repeat(40) },
  };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs`) return json([baseRun]);
    if (path.startsWith(`/api/projects/${projectId}/approvals?status=`)) {
      return json(approval.status === new URL(`http://localhost${path}`).searchParams.get('status') ? [approval] : []);
    }
    if (path === `/api/projects/${projectId}/approvals/${approval.id}/decision` && init?.method === 'POST') {
      approval.status = 'approved'; approval.decided_at = timestamp;
      return json(approval);
    }
    if (path === `/api/projects/${projectId}/tools/github/fixture-snapshot-v3/${approval.id}`) return json(status);
    if (path === `/api/projects/${projectId}/tools/github/fixture-snapshot-v3/${approval.id}/dispatch` && init?.method === 'POST') {
      status = { ...status, snapshot_status: 'queued', attempt_id: 'attempt-v3' };
      return json(status);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); render(<ApprovalInbox projectId={projectId} />);
  expect(await screen.findByText(/Version 3 synthetic HTTPS source snapshot/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Approve synthetic local snapshot' }));
  await user.click(screen.getByRole('button', { name: 'approved' }));
  await user.click(await screen.findByRole('button', { name: 'Check synthetic snapshot' }));
  await user.click(await screen.findByRole('button', { name: 'Create synthetic local snapshot' }));
  expect(await screen.findByText('Synthetic HTTPS snapshot: queued. No coding launch.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Create synthetic local snapshot' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path]) => String(path).includes('github.com'))).toHaveLength(0);
  expect(fetcher.mock.calls.filter(([path]) => String(path).includes('/fixture-fetch-v2/'))).toHaveLength(0);
});

it('keeps a user-authored lookalike approval labeled as a generic decision', async () => {
  const approval: Approval = {
    id: 'generic-approval', project_id: projectId, work_order_id: baseOrder.id,
    run_id: baseRun.id, action_type: 'other', target: 'team/repo',
    content: JSON.stringify({ kind: 'github_stage_v1', project_id: projectId }),
    proposal_digest: 'digest', status: 'pending', created_at: timestamp, decided_at: null,
    synthetic_merge: false, synthetic_sales: false, github_stage: false,
  };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs`) return json([baseRun]);
    if (path === `/api/projects/${projectId}/approvals?status=pending`) return json([approval]);
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<ApprovalInbox projectId={projectId} />);
  expect(await screen.findByRole('heading', { name: 'team/repo' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Approve decision' })).toBeInTheDocument();
  expect(screen.queryByText(/synthetic local fixture stage attempt/)).not.toBeInTheDocument();
});

it('creates, readbacks, readies, and starts a manual work order without provider calls', async () => {
  let order: WorkOrder | null = null;
  let run: Run | null = null;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/work-orders` && init?.method === 'POST') {
      order = { ...baseOrder, ...(JSON.parse(init.body as string) as Pick<WorkOrder, 'title' | 'instructions' | 'profile_id'>) };
      return json(order, 201);
    }
    if (path === `/api/projects/${projectId}/work-orders`) return json(order ? [order] : []);
    if (path === `/api/projects/${projectId}/work-orders/work-1/ready` && init?.method === 'POST') {
      order = { ...baseOrder, ...order, status: 'ready' };
      return json(order);
    }
    if (path === `/api/projects/${projectId}/work-orders/work-1/runs` && init?.method === 'POST') {
      order = { ...baseOrder, ...order, status: 'running' };
      run = { ...baseRun, work_order_title: order.title, work_order_instructions: order.instructions };
      return json(run, 201);
    }
    if (path === `/api/projects/${projectId}/runs`) return json(run ? [run] : []);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const onOpenRun = vi.fn();
  render(<WorkOrdersView projectId={projectId} profiles={profiles} onOpenRun={onOpenRun} />);
  expect(await screen.findByText('No work orders yet. Create a draft to begin planning.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '+ New work order' }));
  await user.type(screen.getByPlaceholderText('A clear outcome for this work'), 'Plan a test pass');
  await user.type(screen.getByPlaceholderText('Describe the task and any boundaries'), 'Review the fixture');
  await user.selectOptions(screen.getByLabelText('Assign to profile'), 'engineer-1');
  await user.click(screen.getByRole('button', { name: 'Create draft' }));
  expect(await screen.findByRole('button', { name: 'Mark ready' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Mark ready' }));
  expect(await screen.findByRole('button', { name: 'Start manual run' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Start manual run' }));
  expect(await screen.findByText('Manual planning run recorded. No agent or provider was called.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'View run timeline · running' }));
  expect(onOpenRun).toHaveBeenCalledWith('run-1');
  expect(fetcher.mock.calls.every(([path]) => path.startsWith(`/api/projects/${projectId}/`))).toBe(true);
});

it('saves a selected file with the new draft work order', async () => {
  let order: WorkOrder | null = null;
  let file: Record<string, unknown> | null = null;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/work-orders` && init?.method === 'POST') {
      order = { ...baseOrder, ...JSON.parse(String(init.body)) }; return json(order, 201);
    }
    if (path === `/api/projects/${projectId}/work-orders`) return json(order ? [order] : []);
    if (path === `/api/projects/${projectId}/runs`) return json([]);
    if (path.startsWith(`/api/projects/${projectId}/input-files/`) && !init?.method) return json({}, 404);
    if (path.startsWith(`/api/projects/${projectId}/input-files?`) && init?.method === 'POST') {
      const headers = init.headers as Record<string, string>;
      file = { id: headers['X-Client-File-Id'], project_id: projectId,
        display_name: 'scope.md', sha256: 'b'.repeat(64), byte_size: 5, status: 'pending' };
      return json(file, 201);
    }
    if (path === `/api/projects/${projectId}/input-files?work_order_id=work-1`) return json(file ? [file] : []);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={profiles} onOpenRun={vi.fn()} />);
  await screen.findByText('No work orders yet. Create a draft to begin planning.');
  await user.click(screen.getByRole('button', { name: '+ New work order' }));
  await user.type(screen.getByPlaceholderText('A clear outcome for this work'), 'Plan a test pass');
  await user.type(screen.getByPlaceholderText('Describe the task and any boundaries'), 'Review the fixture');
  await user.selectOptions(screen.getByLabelText('Assign to profile'), 'engineer-1');
  await user.upload(screen.getByLabelText('Work order files'), new File(['scope'], 'scope.md'));
  await user.click(screen.getByRole('button', { name: 'Create draft' }));
  expect(await screen.findByText('Draft and files saved. Files are processing.')).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path, init]) => path.startsWith(`/api/projects/${projectId}/input-files?scope=work_order`) && init?.method === 'POST')).toBe(true);
});

it('keeps a draft visible and unlocks new work after an attachment failure', async () => {
  let order: WorkOrder | null = null;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/work-orders` && init?.method === 'POST') {
      order = { ...baseOrder, ...JSON.parse(String(init.body)) }; return json(order, 201);
    }
    if (path === `/api/projects/${projectId}/work-orders`) return json(order ? [order] : []);
    if (path === `/api/projects/${projectId}/runs`) return json([]);
    if (path.startsWith(`/api/projects/${projectId}/input-files/`) && !init?.method) return json({}, 404);
    if (path.startsWith(`/api/projects/${projectId}/input-files?`) && init?.method === 'POST')
      return json({ detail: 'Input storage unavailable.' }, 503);
    if (path === `/api/projects/${projectId}/input-files?work_order_id=work-1`) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={profiles} onOpenRun={vi.fn()} />);
  await screen.findByText('No work orders yet. Create a draft to begin planning.');
  await user.click(screen.getByRole('button', { name: '+ New work order' }));
  await user.type(screen.getByPlaceholderText('A clear outcome for this work'), 'First draft');
  await user.type(screen.getByPlaceholderText('Describe the task and any boundaries'), 'Review scope');
  await user.selectOptions(screen.getByLabelText('Assign to profile'), 'engineer-1');
  await user.upload(screen.getByLabelText('Work order files'), new File(['scope'], 'scope.md'));
  await user.click(screen.getByRole('button', { name: 'Create draft' }));
  expect((await screen.findAllByText('Input storage unavailable.')).length).toBeGreaterThan(0);
  expect(screen.getAllByText('First draft').length).toBeGreaterThan(0);
  await user.click(screen.getByRole('button', { name: 'Cancel' }));
  await user.click(screen.getByRole('button', { name: '+ New work order' }));
  expect(screen.getByPlaceholderText('A clear outcome for this work')).toBeEnabled();
  expect(screen.getByPlaceholderText('A clear outcome for this work')).toHaveValue('');
});

it('reassigns a pre-run owner and shows blocked reason through resume', async () => {
  let order = { ...baseOrder };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (path === `/api/projects/${projectId}/runs`) return json([]);
    if (path === `/api/projects/${projectId}/work-orders/work-1/assign` && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ profile_id: 'qa-1', expected_version: 1 });
      order = { ...order, profile_id: 'qa-1', version: 2 };
      return json(order);
    }
    if (path === `/api/projects/${projectId}/work-orders/work-1/block` && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ reason: 'Waiting for review', expected_version: 2 });
      order = { ...order, status: 'blocked', version: 3, block_reason: 'Waiting for review', blocked_from_status: 'draft' };
      return json(order);
    }
    if (path === `/api/projects/${projectId}/work-orders/work-1/unblock` && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ expected_version: 3 });
      order = { ...order, status: 'draft', version: 4, block_reason: null, blocked_from_status: null };
      return json(order);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={profiles} onOpenRun={vi.fn()} />);
  expect(await screen.findByText('Assigned to Engineer')).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('Reassign to profile'), 'qa-1');
  await user.click(screen.getByRole('button', { name: 'Save assignment' }));
  expect(await screen.findByText('Assigned profile updated.')).toBeInTheDocument();
  expect(screen.getByText('Assigned to QA')).toBeInTheDocument();
  await user.type(screen.getByPlaceholderText('What is preventing progress?'), 'Waiting for review');
  await user.click(screen.getByRole('button', { name: 'Block' }));
  expect(await screen.findByText('Work order blocked.')).toBeInTheDocument();
  expect(screen.getByText('Blocked: Waiting for review')).toBeInTheDocument();
  expect(screen.getByText('Resume returns this work order to draft.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Mark ready' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Resume' }));
  expect(await screen.findByText('Work order resumed.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Mark ready' })).toBeInTheDocument();
  expect(screen.queryByText('Blocked: Waiting for review')).not.toBeInTheDocument();
});

it('refreshes current ownership after a stale version conflict without retrying the write', async () => {
  let order = { ...baseOrder };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (path === `/api/projects/${projectId}/runs`) return json([]);
    if (path === `/api/projects/${projectId}/work-orders/work-1/assign` && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ profile_id: 'qa-1', expected_version: 1 });
      order = { ...order, profile_id: 'qa-1', version: 2 };
      return json({ detail: 'work order changed; refresh and retry' }, 409);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={profiles} onOpenRun={vi.fn()} />);
  await screen.findByText('Assigned to Engineer');
  await user.selectOptions(screen.getByLabelText('Reassign to profile'), 'qa-1');
  await user.click(screen.getByRole('button', { name: 'Save assignment' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('work order changed; refresh and retry');
  expect(screen.getByRole('alert')).toHaveTextContent('Saved work orders were refreshed');
  expect(screen.getByText('Assigned to QA')).toBeInTheDocument();
  await waitFor(() => expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/assign'))).toHaveLength(1));
});

it('reports an unconfirmed ownership write and keeps the saved owner visible', async () => {
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/work-orders`) return json([baseOrder]);
    if (path === `/api/projects/${projectId}/runs`) return json([]);
    if (path === `/api/projects/${projectId}/work-orders/work-1/assign` && init?.method === 'POST') {
      return json({ ...baseOrder, profile_id: 'qa-1', version: 2 });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={profiles} onOpenRun={vi.fn()} />);
  await screen.findByText('Assigned to Engineer');
  await user.selectOptions(screen.getByLabelText('Reassign to profile'), 'qa-1');
  await user.click(screen.getByRole('button', { name: 'Save assignment' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('saved work order readback did not confirm the change');
  expect(screen.getByText('Assigned to Engineer')).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/assign'))).toHaveLength(1);
});

it('shows pre-run events and zero usage, then records a handoff and outcome', async () => {
  let run = { ...baseRun };
  const events = [preRunEvent];
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/work-orders`) return json([{ ...baseOrder, status: run.status === 'running' ? 'running' : run.status }]);
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path === `/api/projects/${projectId}/runs?query=work_order_created`) return json([run]);
    if (path === `/api/projects/${projectId}/runs/run-1`) return json({ ...run, events });
    if (path === `/api/projects/${projectId}/runs/run-1/handoffs` && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ profile_id: 'qa-1' });
      run = { ...run, profile_id: 'qa-1' };
      events.push({ ...preRunEvent, id: 'event-2', run_id: run.id, event_type: 'run_handed_off', payload: { to_profile_id: 'qa-1' } });
      return json(run);
    }
    if (path === `/api/projects/${projectId}/runs/run-1/finish` && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ outcome: 'done', reason: 'Reviewed' });
      run = { ...run, status: 'done', outcome_reason: 'Reviewed', finished_at: timestamp };
      events.push({ ...preRunEvent, id: 'event-3', run_id: run.id, event_type: 'run_finished', payload: { outcome: 'done' } });
      return json(run);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<RunTimelineView projectId={projectId} profiles={profiles} initialRunId="run-1" />);
  expect(await screen.findByText('No model usage · 0 tokens · $0.00')).toBeInTheDocument();
  expect(screen.getByText('Work order created')).toBeInTheDocument();
  expect(screen.queryByText('operator')).not.toBeInTheDocument();
  expect((await getRun(projectId, 'run-1')).events[0].run_id).toBeNull();
  await user.type(screen.getByLabelText('Search runs'), 'work_order_created');
  await user.click(screen.getByRole('button', { name: 'Search' }));
  expect(fetcher.mock.calls.some(([path]) => path === `/api/projects/${projectId}/runs?query=work_order_created`)).toBe(true);
  await user.selectOptions(screen.getByLabelText('Next profile'), 'qa-1');
  await user.click(screen.getByRole('button', { name: 'Record handoff' }));
  expect(await screen.findByText('Profile handoff recorded. No agent was called.')).toBeInTheDocument();
  expect(screen.getByText('Work handed to another agent')).toBeInTheDocument();
  await user.type(screen.getByLabelText('Reason'), 'Reviewed');
  await user.click(screen.getByRole('button', { name: 'Record outcome' }));
  expect(await screen.findByText('Manual planning outcome recorded.')).toBeInTheDocument();
  expect(screen.getByText('Run outcome recorded')).toBeInTheDocument();
  expect(screen.getByText('Outcome reason:').closest('p')).toHaveTextContent('Outcome reason: Reviewed');
  expect(screen.queryByText('"reason": "Reviewed"')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Record handoff' })).not.toBeInTheDocument();
});

it('shows a blocked manual run reason and hides handoff and outcome controls', async () => {
  const blockedOrder: WorkOrder = { ...baseOrder, status: 'blocked', version: 3,
    block_reason: 'Waiting on approval', blocked_from_status: 'running' };
  const fetcher = vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs`) return json([baseRun]);
    if (path === `/api/projects/${projectId}/runs/run-1`) return json({ ...baseRun, events: [], profile_snapshots: [] });
    if (path === `/api/projects/${projectId}/work-orders`) return json([blockedOrder]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<RunTimelineView projectId={projectId} profiles={profiles} initialRunId="run-1" />);
  expect(await screen.findByText('Work order blocked')).toBeInTheDocument();
  expect(screen.getByText('Waiting on approval')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Record handoff' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Record outcome' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => path === `/api/projects/${projectId}/work-orders`)).toBe(true);
});

it('fails closed when current work order state cannot be read and can refresh', async () => {
  let fail = true;
  const fetcher = vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs`) return json([baseRun]);
    if (path === `/api/projects/${projectId}/runs/run-1`) return json({ ...baseRun, events: [], profile_snapshots: [] });
    if (path === `/api/projects/${projectId}/work-orders`) {
      if (fail) return json({ detail: 'Work order read unavailable' }, 503);
      return json([{ ...baseOrder, status: 'running' }]);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<RunTimelineView projectId={projectId} profiles={profiles} initialRunId="run-1" />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Work order read unavailable');
  expect(screen.queryByRole('button', { name: 'Record handoff' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Record outcome' })).not.toBeInTheDocument();
  fail = false;
  await user.click(screen.getByRole('button', { name: 'Refresh run and work order' }));
  expect(await screen.findByRole('button', { name: 'Record handoff' })).toBeInTheDocument();
});

it('records approval and rejection decisions without an outbound request', async () => {
  const approvals: Approval[] = [];
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs`) return json([baseRun]);
    if (path.startsWith(`/api/projects/${projectId}/approvals?status=`)) {
      const status = path.split('=')[1];
      return json(approvals.filter((item) => item.status === status));
    }
    if (path === `/api/projects/${projectId}/runs/run-1/approval-requests` && init?.method === 'POST') {
      const fields = JSON.parse(init.body as string) as Pick<Approval, 'action_type' | 'target' | 'content'>;
      const item: Approval = { id: `approval-${approvals.length + 1}`, project_id: projectId,
        work_order_id: baseOrder.id, run_id: baseRun.id, ...fields, proposal_digest: 'digest',
        status: 'pending', created_at: timestamp, decided_at: null, synthetic_merge: false };
      approvals.push(item);
      return json(item, 201);
    }
    const decision = path.match(/\/approvals\/(approval-\d+)\/decision$/);
    if (decision && init?.method === 'POST') {
      const item = approvals.find((approval) => approval.id === decision[1]);
      if (!item) throw new Error('Missing approval');
      item.status = (JSON.parse(init.body as string) as { decision: 'approved' | 'rejected' }).decision;
      item.decided_at = timestamp;
      return json(item);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ApprovalInbox projectId={projectId} />);
  await screen.findByText('No pending proposals for this project.');
  await user.selectOptions(screen.getByLabelText('Manual run'), 'run-1');
  await user.type(screen.getByPlaceholderText('Exact recipient, branch, URL, or target'), 'review@example.test');
  await user.type(screen.getByPlaceholderText('What would be sent or changed'), 'Review draft');
  await user.click(screen.getByRole('button', { name: 'Request approval' }));
  const proposed = await screen.findByRole('heading', { name: 'review@example.test' });
  expect(within(proposed.closest('article')!).getByText('Review draft')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Approve decision' }));
  expect(await screen.findByText('Proposal approved. This records a decision only; no outbound action was performed.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'approved' }));
  expect(await screen.findByRole('heading', { name: 'review@example.test' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'pending' }));
  await user.type(screen.getByPlaceholderText('Exact recipient, branch, URL, or target'), 'another@example.test');
  await user.type(screen.getByPlaceholderText('What would be sent or changed'), 'Second draft');
  await user.click(screen.getByRole('button', { name: 'Request approval' }));
  expect(await screen.findByRole('heading', { name: 'another@example.test' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Reject' }));
  expect(await screen.findByText('Proposal rejected. This records a decision only; no outbound action was performed.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'rejected' }));
  expect(await screen.findByRole('heading', { name: 'another@example.test' })).toBeInTheDocument();
  expect(fetcher.mock.calls.every(([path]) => path.startsWith(`/api/projects/${projectId}/`))).toBe(true);
});


it('explains the separate document dispatch after approval', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json([])));
  render(<ApprovalInbox projectId={projectId} />);
  expect(await screen.findByText(/Document changes and GitHub PR comments require a separate dispatch after approval/)).toBeInTheDocument();
  expect(screen.queryByText(/cloud changes, external merges, and coding launch are not connected/)).not.toBeInTheDocument();
});
