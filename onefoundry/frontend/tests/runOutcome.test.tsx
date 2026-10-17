import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';

import { RunTimelineView } from '../src/RunTimelineView';
import type { Run } from '../src/workApi';

const projectId = 'project-1';
const started = '2026-09-25T12:00:00Z';
const run: Run = {
  id: 'run-1', project_id: projectId, work_order_id: 'work-1',
  work_order_title: 'Check fixture change', work_order_instructions: 'Review the saved result',
  profile_id: 'qa-1', profile_model: null, status: 'unknown', mode: 'synthetic_code',
  tokens_used: 0, cost_usd: 0, outcome_reason: 'Runner receipt did not confirm the final outcome',
  started_at: started, finished_at: started,
};

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('scans a saved unknown code run without treating its status as a test result', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path === `/api/projects/${projectId}/runs/${run.id}`) return json({ ...run, events: [],
      profile_snapshots: [{ profile_id: 'qa-1', profile_name: 'QA', profile_model: null,
        profile_instructions: '', memory_scope: 'project', allowed_tools: [], autonomy_level: 'manual',
        transition_index: 1, actor_id: 'operator', recorded_at: started }], skill_snapshots: [{
          skill_id: 'skill-1', skill_name: 'QA review', version: 2, sha256: 'a'.repeat(64),
          transition_index: 1, actor_id: 'operator', created_at: started,
        }] });
    if (path === `/api/projects/${projectId}/runs/${run.id}/synthetic-code`) return json({
      run, synthetic_merge_approval_id: null, jobs: [{
        id: 'job-1', project_id: projectId, run_id: run.id, source_job_id: null, approval_id: null,
        stage: 'engineer', status: 'unknown', reason_code: 'receipt_unknown', branch: null,
        commit: null, baseline_commit: null, merged_commit: null, observed_head_commit: null,
        diff_sha256: 'abc', created_at: started, started_at: started, finished_at: null,
        tests: [{ name: 'fixture test', status: 'failed', exit_code: 1 }],
        artifacts: [{ id: 'diff-1', kind: 'diff', path: 'artifacts/diff.patch', sha256: 'abc', byte_size: 5 }],
      }],
    });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);

  render(<RunTimelineView projectId={projectId} profiles={[]} initialRunId={run.id} />);
  const list = await screen.findByLabelText('Runs');
  expect(within(list).getByText('Synthetic Engineer → QA')).toBeInTheDocument();
  expect(within(list).getByText(/Previous agent ·/)).toBeInTheDocument();
  expect(within(list).getByText(run.outcome_reason)).toBeInTheDocument();

  const summary = await screen.findByLabelText('Selected run outcome');
  expect(within(summary).getByText('Unknown')).toBeInTheDocument();
  expect(within(summary).getByText('QA')).toBeInTheDocument();
  expect(await within(summary).findByText('1 saved runner job · QA checks not recorded · Engineer checks: 0 passed, 1 failed · 1 saved diff · 1 saved artifact')).toBeInTheDocument();
  expect(within(summary).getByText(run.outcome_reason)).toBeInTheDocument();
  expect(within(summary).getByRole('link', { name: 'Inspect saved tests, diff, and artifacts' })).toHaveAttribute('href', '#code-panel-heading');
  const selectedSkills = screen.getByRole('region', { name: 'Saved profile procedure versions' });
  expect(within(selectedSkills).getByText('QA review · v2')).toBeInTheDocument();
  expect(within(selectedSkills).queryByText(/SHA-256:/)).not.toBeInTheDocument();
  expect(within(selectedSkills).getByText(/does not show whether an agent executed/)).toBeInTheDocument();
  expect(fetcher.mock.calls.every(([path]) => path.startsWith(`/api/projects/${projectId}/`))).toBe(true);
});

it('labels a completed manual planning run as a planning record with no saved result', async () => {
  const manual: Run = { ...run, status: 'done', mode: 'manual_planning', outcome_reason: 'Plan reviewed' };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs`) return json([manual]);
    if (path === `/api/projects/${projectId}/runs/${manual.id}`) return json({ ...manual, events: [],
      profile_snapshots: [], skill_snapshots: [] });
    if (path === `/api/projects/${projectId}/results`) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));

  render(<RunTimelineView projectId={projectId} profiles={[]} initialRunId={manual.id} />);
  const summary = await screen.findByLabelText('Selected run outcome');
  expect(within(summary).getByText('Planning record only · no agent or model result')).toBeInTheDocument();
  expect(within(summary).getByText('Plan reviewed')).toBeInTheDocument();
  expect(within(summary).getByText('No model usage · 0 tokens · $0.00')).toBeInTheDocument();
});

it('shows the saved operator result on a completed manual run', async () => {
  const manual: Run = { ...run, status: 'done', mode: 'manual_planning', outcome_reason: 'Plan reviewed' };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs`) return json([manual]);
    if (path === `/api/projects/${projectId}/runs/${manual.id}`) return json({ ...manual, events: [],
      profile_snapshots: [], skill_snapshots: [] });
    if (path === `/api/projects/${projectId}/results`) return json([{ id: 'result-1', project_id: projectId,
      actor_id: 'operator', run_id: manual.id, summary: 'The local plan is ready for QA.', scope: 'project',
      source_kind: 'operator_entered', created_at: started }]);
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<RunTimelineView projectId={projectId} profiles={[]} initialRunId={manual.id} />);
  const summary = await screen.findByLabelText('Selected run outcome');
  expect(await within(summary).findByText('1 saved operator result · no agent or model result')).toBeInTheDocument();
  expect(within(summary).getByText('The local plan is ready for QA.')).toBeInTheDocument();
});

it('keeps unloaded external attempts neutral when multiple runs are listed', async () => {
  const first: Run = { ...run, id: 'offline-probe', mode: 'external_code', status: 'done' };
  const second: Run = { ...run, id: 'coding-attempt', mode: 'external_code', status: 'unknown' };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs`) return json([first, second]);
    throw new Error(`Detail unavailable in this list test: ${path}`);
  }));

  render(<RunTimelineView projectId={projectId} profiles={[]} initialRunId={null} />);
  const list = await screen.findByLabelText('Runs');
  expect(within(list).getAllByText('External execution attempt')).toHaveLength(2);
  expect(within(list).queryByText('External coding agent')).not.toBeInTheDocument();
});
