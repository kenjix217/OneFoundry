import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { Profile } from '../src/api';
import { ApprovalInbox } from '../src/ApprovalInbox';
import { CodeRunPanel } from '../src/CodeRunPanel';
import { WorkOrdersView } from '../src/WorkOrdersView';
import { getArtifactPreview, getSyntheticCode, type CodeArtifact, type CodeDetail, type CodeJob } from '../src/codeApi';
import type { Approval, Run, WorkOrder } from '../src/workApi';

const projectId = 'project-1';
const timestamp = '2026-09-25T12:00:00Z';
const engineer: Profile = { id: 'engineer-1', project_id: projectId, name: 'Engineer', kind: 'builtin', reports_to_profile_id: null, model: null,
  instructions: '', memory_scope: 'project', allowed_tools: [], autonomy_level: 'manual',
  configured: false, permissions_active: false };
const qa: Profile = { ...engineer, id: 'qa-1', name: 'QA' };
const order: WorkOrder = { id: 'work-1', project_id: projectId, title: 'Check local fixture',
  instructions: 'Use the approved synthetic repository', profile_id: engineer.id, status: 'ready',
  deliverable_kind: 'operator_review', acceptance_checks: [], parent_work_order_id: null, source_run_id: null, origin_signal_id: null, chain_depth: 0,
  version: 1, block_reason: null, blocked_from_status: null, created_at: timestamp };
const run: Run = { id: 'run-1', project_id: projectId, work_order_id: order.id,
  work_order_title: order.title, work_order_instructions: order.instructions,
  profile_id: engineer.id, profile_model: null, status: 'running', mode: 'synthetic_code',
  tokens_used: 0, cost_usd: 0, outcome_reason: '', started_at: timestamp, finished_at: null };
const diff: CodeArtifact = { id: 'artifact-diff', kind: 'diff', path: 'artifacts/job-engineer/change.diff',
  sha256: 'abc123', byte_size: 21 };
const engineerQueued: CodeJob = { id: 'job-engineer', project_id: projectId, run_id: run.id, stage: 'engineer',
  source_job_id: null, approval_id: null,
  status: 'queued', reason_code: null, branch: null, commit: null, baseline_commit: null,
  merged_commit: null, observed_head_commit: null, diff_sha256: null, tests: [], artifacts: [], created_at: timestamp,
  started_at: null, finished_at: null };
const engineerDone: CodeJob = { ...engineerQueued, status: 'done', branch: 'fixture/work-1',
  commit: 'commit-new', baseline_commit: 'commit-base', diff_sha256: diff.sha256,
  tests: [{ name: 'before', status: 'passed', exit_code: 0 }, { name: 'after', status: 'passed', exit_code: 0 }],
  artifacts: [diff], started_at: timestamp, finished_at: timestamp };
const qaQueued: CodeJob = { ...engineerQueued, id: 'job-qa', stage: 'qa', source_job_id: engineerQueued.id };
const qaDone: CodeJob = { ...qaQueued, status: 'done', branch: 'fixture/work-1', commit: 'commit-new',
  baseline_commit: 'commit-base', diff_sha256: diff.sha256,
  tests: [{ name: 'qa', status: 'passed', exit_code: 0 }], started_at: timestamp, finished_at: timestamp };
const mergeQueued: CodeJob = { ...engineerQueued, id: 'job-merge', stage: 'merge', source_job_id: engineerQueued.id,
  approval_id: 'approval-1' };
const mergeDone: CodeJob = { ...mergeQueued, status: 'done', branch: 'fixture/work-1',
  commit: 'commit-new', baseline_commit: 'commit-base', merged_commit: 'commit-merged',
  observed_head_commit: 'commit-merged',
  diff_sha256: diff.sha256, tests: [{ name: 'merged_fixture_test', status: 'passed', exit_code: 0 }],
  started_at: timestamp, finished_at: timestamp };
const mergeApproval: Approval = { id: 'approval-1', project_id: projectId, work_order_id: order.id,
  run_id: run.id, action_type: 'merge', target: 'fixture/work-1', content: 'Exact local merge proposal',
  proposal_digest: 'digest-1', status: 'pending', created_at: timestamp, decided_at: null, synthetic_merge: true };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('launches only the eligible Engineer fixture and confirms the saved queued run', async () => {
  let savedOrder = { ...order };
  let savedRun: Run | null = null;
  const detail: CodeDetail = { run, jobs: [engineerQueued], synthetic_merge_approval_id: null };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/work-orders`) return json([savedOrder]);
    if (path === `/api/projects/${projectId}/runs`) return json(savedRun ? [savedRun] : []);
    if (path.endsWith('/synthetic-code/eligibility')) return json({ eligible: true, reason: null,
      fixture_id: 'engineer_qa_v1', engineer_profile_id: engineer.id, qa_profile_id: qa.id });
    if (path.endsWith('/synthetic-code/runs') && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ fixture_id: 'engineer_qa_v1' });
      savedOrder = { ...savedOrder, status: 'running' };
      savedRun = run;
      return json(detail, 201);
    }
    if (path === `/api/projects/${projectId}/runs/${run.id}/synthetic-code`) return json(detail);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const openRun = vi.fn();
  render(<WorkOrdersView projectId={projectId} profiles={[engineer, qa]} onOpenRun={openRun} />);
  await user.click(await screen.findByRole('button', { name: 'Run synthetic Engineer → QA fixture' }));
  expect(openRun).toHaveBeenCalledWith(run.id);
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/synthetic-code/runs'))).toHaveLength(1);
  expect(fetcher.mock.calls.every(([path]) => path.startsWith(`/api/projects/${projectId}/`))).toBe(true);
});

it('shows the server ineligibility reason without launching a runner job', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (path === `/api/projects/${projectId}/runs`) return json([]);
    if (path.endsWith('/synthetic-code/eligibility')) return json({ eligible: false, reason: 'QA profile unavailable',
      fixture_id: 'engineer_qa_v1', engineer_profile_id: engineer.id, qa_profile_id: null });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<WorkOrdersView projectId={projectId} profiles={[engineer, qa]} onOpenRun={vi.fn()} />);
  expect(await screen.findByText('Synthetic code fixture unavailable: QA profile unavailable')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Run synthetic Engineer → QA fixture' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/synthetic-code/runs'))).toBe(false);
});

it('shows saved Engineer and QA tests, verified diff, then requests one pending merge approval', async () => {
  let phase = 0;
  let approval: Approval | null = null;
  const saved = (): CodeDetail => ({ run: phase >= 2 ? { ...run, status: 'done', profile_id: qa.id, finished_at: timestamp } : run,
    jobs: phase === 0 ? [engineerQueued] : phase === 1 ? [engineerDone, qaQueued] : [engineerDone, qaDone],
    synthetic_merge_approval_id: approval?.id ?? null });
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs/${run.id}/synthetic-code` && !init?.method) return json(saved());
    if (path.endsWith('/synthetic-code/reconcile') && init?.method === 'POST') {
      phase += 1;
      return json(saved());
    }
    if (path.endsWith(`/synthetic-code/artifacts/${diff.id}`)) return json({ kind: diff.kind,
      sha256: diff.sha256, byte_size: diff.byte_size, text: 'diff --git a/a b/a\n+synthetic change' });
    if (path.endsWith('/synthetic-code/merge-approval') && init?.method === 'POST') {
      approval = mergeApproval;
      return json(approval, 201);
    }
    if (path === `/api/projects/${projectId}/approvals`) return json(approval ? [approval] : []);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<CodeRunPanel projectId={projectId} runId={run.id} onTimelineRefresh={vi.fn()} />);
  expect((await screen.findAllByText('Queued · awaiting runner receipt')).length).toBeGreaterThan(0);
  await user.click(screen.getByRole('button', { name: 'Check runner receipt' }));
  expect(await screen.findByText('Before / after tests')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Check runner receipt' }));
  expect(await screen.findByText('QA tests')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'View verified diff' }));
  expect(await screen.findByLabelText('Verified synthetic diff')).toHaveTextContent('synthetic change');
  await user.click(screen.getByRole('button', { name: 'Request synthetic merge approval' }));
  expect(await screen.findByText('Merge proposal · pending')).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/synthetic-code/merge-approval'))).toHaveLength(1);
});

it('approves only a bound local merge and reads back its queued job', async () => {
  let approval = { ...mergeApproval };
  let merge: CodeJob | null = null;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs`) return json([{ ...run, status: 'done' }]);
    if (path.startsWith(`/api/projects/${projectId}/approvals?status=`)) {
      return json(approval.status === path.split('=')[1] ? [approval] : []);
    }
    if (path.endsWith('/approvals/approval-1/decision') && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ decision: 'approved' });
      approval = { ...approval, status: 'approved', decided_at: timestamp };
      merge = mergeQueued;
      return json(approval);
    }
    if (path === `/api/projects/${projectId}/runs/${run.id}/synthetic-code`) return json({
      run: { ...run, status: 'done' }, jobs: [engineerDone, qaDone, ...(merge ? [merge] : [])],
      synthetic_merge_approval_id: approval.id,
    });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ApprovalInbox projectId={projectId} />);
  await user.click(await screen.findByRole('button', { name: 'Approve and queue local merge' }));
  expect(await screen.findByText('Synthetic merge approved; local fixture merge queued. Check Activity for its saved receipt.')).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/decision'))).toHaveLength(1);
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/synthetic-code/merge'))).toBe(false);
});

it('rejects the bound merge without a merge job and shows a completed merge receipt separately', async () => {
  let approval = { ...mergeApproval };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs`) return json([{ ...run, status: 'done' }]);
    if (path.startsWith(`/api/projects/${projectId}/approvals?status=`)) {
      return json(approval.status === path.split('=')[1] ? [approval] : []);
    }
    if (path.endsWith('/approvals/approval-1/decision') && init?.method === 'POST') {
      approval = { ...approval, status: 'rejected', decided_at: timestamp };
      return json(approval);
    }
    if (path === `/api/projects/${projectId}/runs/${run.id}/synthetic-code`) return json({
      run: { ...run, status: 'done' }, jobs: [engineerDone, qaDone], synthetic_merge_approval_id: approval.id,
    });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const view = render(<ApprovalInbox projectId={projectId} />);
  await user.click(await screen.findByRole('button', { name: 'Reject' }));
  expect(await screen.findByText('Synthetic merge rejected. No local merge job was queued.')).toBeInTheDocument();
  view.unmount();

  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs/${run.id}/synthetic-code`) return json({
      run: { ...run, status: 'done' }, jobs: [engineerDone, qaDone, mergeDone],
      synthetic_merge_approval_id: mergeApproval.id,
    });
    if (path === `/api/projects/${projectId}/approvals`) return json([{ ...mergeApproval, status: 'approved' }]);
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<CodeRunPanel projectId={projectId} runId={run.id} onTimelineRefresh={vi.fn()} />);
  expect(await screen.findAllByText('Recorded')).toHaveLength(3);
  expect(screen.queryByText('commit-merged')).not.toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Post-merge test' })).toBeInTheDocument();
  expect(screen.getByText('Local fixture merge completed. Receipt is shown above.')).toBeInTheDocument();
});

it('rejects mismatched artifact previews and cross-project runner details', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ kind: 'diff', sha256: 'wrong',
    byte_size: diff.byte_size, text: 'wrong body' })));
  await expect(getArtifactPreview(projectId, run.id, diff)).rejects.toThrow('invalid artifact preview identity');
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ run, jobs: [{ ...engineerQueued, project_id: 'other' }],
    synthetic_merge_approval_id: null })));
  await expect(getSyntheticCode(projectId, run.id)).rejects.toThrow('invalid runner job scope');
});

it('shows an unknown runner outcome without offering another launch or receipt import', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs/${run.id}/synthetic-code`) return json({
      run: { ...run, status: 'unknown', outcome_reason: 'receipt_invalid' },
      jobs: [{ ...engineerQueued, status: 'unknown', reason_code: 'receipt_invalid' }],
      synthetic_merge_approval_id: null,
    });
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<CodeRunPanel projectId={projectId} runId={run.id} onTimelineRefresh={vi.fn()} />);
  expect(await screen.findByText('Reason: receipt_invalid')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Check runner receipt' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Request synthetic merge approval' })).not.toBeInTheDocument();
});

it('holds a failed receipt check until explicit saved-state readback and never retries the POST', async () => {
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs/${run.id}/synthetic-code` && !init?.method) return json({
      run, jobs: [engineerQueued], synthetic_merge_approval_id: null,
    });
    if (path.endsWith('/synthetic-code/reconcile') && init?.method === 'POST') {
      return json({ detail: 'runner receipt unavailable' }, 503);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<CodeRunPanel projectId={projectId} runId={run.id} onTimelineRefresh={vi.fn()} />);
  await user.click(await screen.findByRole('button', { name: 'Check runner receipt' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('runner receipt unavailable');
  expect(screen.getByRole('button', { name: 'Check runner receipt' })).toBeDisabled();
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/synthetic-code/reconcile'))).toHaveLength(1);
  await user.click(screen.getByRole('button', { name: 'Reload saved state' }));
  expect(await screen.findByRole('button', { name: 'Check runner receipt' })).toBeEnabled();
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/synthetic-code/reconcile'))).toHaveLength(1);
});
