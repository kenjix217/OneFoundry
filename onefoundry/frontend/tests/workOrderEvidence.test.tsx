import { afterEach, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { getExternalCode, getExternalCodeArtifact, getExternalCodeProgress,
  type ExternalCodeAttempt } from '../src/externalCodeApi';
import { CodingActions, WorkOrderEvidenceView } from '../src/WorkOrderEvidenceView';
import { AGENT_UPDATE_TEXT, STAGE_BASELINE_FAILED } from '../src/workOrderTrace';
import { getLiveTextOutput, getWorkOrderActivity, listRuns, type LiveTextOutput, type WorkOrder } from '../src/workApi';

vi.mock('../src/externalCodeApi', async (importOriginal) => ({
  ...await importOriginal<typeof import('../src/externalCodeApi')>(),
  getExternalCode: vi.fn(),
  getExternalCodeArtifact: vi.fn(),
  getExternalCodeProgress: vi.fn(),
}));
vi.mock('../src/workApi', async (importOriginal) => ({
  ...await importOriginal<typeof import('../src/workApi')>(),
  getLiveTextOutput: vi.fn(), getWorkOrderActivity: vi.fn(), listRuns: vi.fn(),
}));

afterEach(() => { vi.clearAllMocks(); vi.useRealTimers(); });

it('expands a failed observed shell command while labeling provider coverage partial', async () => {
  const trace = JSON.stringify({ schema_version: 1, coverage: 'partial', provider_tool_calls_captured: false,
    omitted_actions: 0, actions: [{ source: 'runner_observed', kind: 'command',
      command: ['python', '-m', 'unittest'], cwd: '.', started_at: '2026-09-29T10:00:00Z',
      finished_at: '2026-09-29T10:00:01Z', outcome: 'finished', exit_code: 1,
      stdout: 'FAIL [redacted]', stderr: '', output_truncated: false }] });
  const artifact = { id: 'trace-1', kind: 'trace', path: 'artifacts/attempt-1/actions.json',
    sha256: 'a'.repeat(64), byte_size: trace.length };
  const attempt = { id: 'attempt-1', run_id: 'run-1', fixture_id: 'engineer_qa_v1',
    tests: [{ name: 'before', status: 'failed', exit_code: 1 }], artifacts: [artifact] } as ExternalCodeAttempt;
  vi.mocked(getExternalCodeArtifact).mockResolvedValue({ kind: 'trace', sha256: artifact.sha256,
    byte_size: artifact.byte_size, text: trace });

  const user = userEvent.setup();
  render(<CodingActions projectId="project-1" attempt={attempt} />);
  await user.click(screen.getByText('Actions in this run · Partial record'));
  expect(await screen.findByText(/Provider tool calls and intermediate agent updates were not captured/)).toBeInTheDocument();
  expect(screen.getByText('Shell command')).toBeInTheDocument();
  expect(screen.getByText(/· Finished · exit 1/)).toBeInTheDocument();
  await user.click(screen.getByText('python -m unittest · .'));
  expect(screen.getByText('FAIL [redacted]')).toBeInTheDocument();
  expect(screen.getByText('Test receipts · 1')).toBeInTheDocument();
  await waitFor(() => expect(getExternalCodeArtifact).toHaveBeenCalledTimes(1));
});

it('shows safe coding updates in the conversation and keeps tool detail collapsed', async () => {
  const trace = JSON.stringify({ schema_version: 1, coverage: 'partial', provider_tool_calls_captured: false,
    omitted_actions: 0, omitted_progress: 1, actions: [], progress: [
      { source: 'cli_output', kind: 'agent_update', text: AGENT_UPDATE_TEXT,
        tool: null, command: null },
      { source: 'cli_output', kind: 'tool_activity', text: 'Ran a shell command.',
        tool: 'shell', command: 'git diff --check' },
    ] });
  const artifact = { id: 'trace-2', kind: 'trace', path: 'artifacts/attempt-2/actions.json',
    sha256: 'b'.repeat(64), byte_size: trace.length };
  const attempt = { id: 'attempt-2', run_id: 'run-2', fixture_id: 'engineer_qa_v1',
    tests: [], artifacts: [artifact] } as unknown as ExternalCodeAttempt;
  vi.mocked(getExternalCodeArtifact).mockResolvedValue({ kind: 'trace', sha256: artifact.sha256,
    byte_size: artifact.byte_size, text: trace });
  vi.mocked(getExternalCodeProgress).mockResolvedValue({ project_id: 'project-1', run_id: 'run-2',
    attempt_id: 'attempt-2', coverage: 'partial', latest_sequence: 0, terminal: true, items: [] });
  const user = userEvent.setup();
  render(<CodingActions projectId="project-1" attempt={attempt} showProgress />);
  expect(await screen.findByText(AGENT_UPDATE_TEXT)).toBeInTheDocument();
  expect(screen.getByText('Ran a shell command.')).toBeInTheDocument();
  expect(screen.getByText(/Private reasoning and tool payloads are excluded/)).toBeInTheDocument();
  expect(screen.getByText('Shell command').closest('details')).not.toHaveAttribute('open');
  await user.click(screen.getByText('Shell command'));
  expect(screen.getByText('git diff --check')).toBeInTheDocument();
});

it('polls ordered coding updates before a final trace exists', async () => {
  vi.useFakeTimers();
  const attempt = { id: 'attempt-live', run_id: 'run-live', status: 'queued',
    fixture_id: 'engineer_qa_v1', tests: [], artifacts: [] } as unknown as ExternalCodeAttempt;
  const base = { project_id: 'project-1', run_id: 'run-live', attempt_id: 'attempt-live',
    coverage: 'partial' as const, terminal: false };
  vi.mocked(getExternalCodeProgress)
    .mockResolvedValueOnce({ ...base, latest_sequence: 1, items: [{ sequence: 1,
      source: 'runner_observed', kind: 'runner_update', text: STAGE_BASELINE_FAILED, tool: null, command: null }] })
    .mockResolvedValueOnce({ ...base, latest_sequence: 2, items: [{ sequence: 2,
      source: 'cli_output', kind: 'tool_activity', text: 'Requested a project file edit.', tool: 'Edit', command: null }] });
  render(<CodingActions projectId="project-1" attempt={attempt} showProgress />);
  await act(async () => { await Promise.resolve(); });
  expect(screen.getByText(STAGE_BASELINE_FAILED)).toBeInTheDocument();
  expect(screen.getByText('Runner update')).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(screen.getByText('Requested a project file edit.')).toBeInTheDocument();
  expect(vi.mocked(getExternalCodeProgress).mock.calls.map((call) => call[3])).toEqual([0, 1]);
});

it('refreshes saved activity while a work order is running', async () => {
  vi.useFakeTimers();
  vi.mocked(getWorkOrderActivity).mockResolvedValue([]);
  vi.mocked(listRuns).mockResolvedValue([]);
  const order = { id: 'order-1', project_id: 'project-1', title: 'Repair import',
    instructions: 'Repair the fixture', profile_id: 'agent-1', status: 'running' } as WorkOrder;
  render(<WorkOrderEvidenceView projectId="project-1" order={order} profiles={[]}
    mode="activity" onOpenRun={() => {}} />);
  await act(async () => { await Promise.resolve(); });
  expect(getWorkOrderActivity).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(getWorkOrderActivity).toHaveBeenCalledTimes(2);
});

it('shows a saved automation decision with the assigned agent result', async () => {
  vi.mocked(getWorkOrderActivity).mockResolvedValue([]);
  vi.mocked(listRuns).mockResolvedValue([{ id: 'run-rule', project_id: 'project-1',
    work_order_id: 'order-rule', mode: 'live_text', status: 'done', profile_id: 'agent-1' } as
    Awaited<ReturnType<typeof listRuns>>[number]]);
  vi.mocked(getLiveTextOutput).mockResolvedValue({ id: 'output-rule', run_id: 'run-rule',
    text: 'Automation rule created: Monday brief.', automation_decision: { decision: 'create_rule', status: 'saved',
      rule_id: 'rule-1', rule_version: 1, project_version: null, error_code: null } } as LiveTextOutput);
  const order = { id: 'order-rule', project_id: 'project-1', title: 'Maintain a brief',
    instructions: 'Create a recurring brief', profile_id: 'agent-1', status: 'done' } as WorkOrder;
  render(<WorkOrderEvidenceView projectId="project-1" order={order} profiles={[]}
    mode="results" onOpenRun={() => {}} />);
  expect(await screen.findByText('Automation rule created: Monday brief.')).toBeInTheDocument();
  expect(screen.getByText('Automation decision: saved.')).toBeInTheDocument();
});

it('does not claim no activity when a coding attempt has updates', async () => {
  vi.mocked(getWorkOrderActivity).mockResolvedValue([]);
  const run = { id: 'run-1', project_id: 'project-1', work_order_id: 'order-1',
    mode: 'external_code', status: 'running' } as unknown as Awaited<ReturnType<typeof listRuns>>[number];
  const attempt = { id: 'attempt-1', run_id: 'run-1', status: 'queued',
    fixture_id: 'engineer_qa_v1', tests: [], artifacts: [] } as unknown as ExternalCodeAttempt;
  vi.mocked(listRuns).mockResolvedValue([run]);
  vi.mocked(getExternalCode).mockResolvedValue({ run, attempts: [attempt] });
  vi.mocked(getExternalCodeProgress).mockResolvedValue({ project_id: 'project-1',
    run_id: 'run-1', attempt_id: 'attempt-1', coverage: 'partial', latest_sequence: 1,
    terminal: false, items: [{ sequence: 1, source: 'runner_observed', kind: 'runner_update',
      text: STAGE_BASELINE_FAILED, tool: null, command: null }] });
  const order = { id: 'order-1', project_id: 'project-1', title: 'Repair calculator',
    instructions: 'Repair calculator', profile_id: 'agent-1', status: 'running' } as WorkOrder;
  render(<WorkOrderEvidenceView projectId="project-1" order={order} profiles={[]}
    mode="conversation" onOpenRun={() => {}} />);
  expect(await screen.findByText(STAGE_BASELINE_FAILED)).toBeInTheDocument();
  expect(screen.queryByText('No work activity has been recorded yet.')).not.toBeInTheDocument();
});

it('omits technical identifiers while retaining the readable assignment', () => {
  const order = { id: 'order-1', project_id: 'project-1', title: 'Repair import',
    instructions: 'Repair the fixture', profile_id: 'agent-1', status: 'blocked', version: 2,
    deliverable_kind: 'operator_review', acceptance_checks: [], parent_work_order_id: null,
    source_run_id: null, origin_signal_id: null, chain_depth: 0, block_reason: null, blocked_from_status: null,
    created_at: '2026-09-30T10:00:00Z' } as WorkOrder;
  render(<WorkOrderEvidenceView projectId="project-1" order={order} profiles={[]}
    mode="details" onOpenRun={() => {}} />);
  expect(screen.getByText(/Waiting for you/)).toBeInTheDocument();
  expect(screen.queryByText(/Work order ID/)).not.toBeInTheDocument();
  expect(screen.queryByText(order.id)).not.toBeInTheDocument();
});
