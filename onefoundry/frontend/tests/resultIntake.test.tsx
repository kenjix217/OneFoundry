import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { ResultIntake } from '../src/ResultIntake';
import { createCandidate, createResult, listCandidates, listResults, type Candidate, type SourceResult } from '../src/memoryApi';
import { listRuns, type Run } from '../src/workApi';
vi.mock('../src/memoryApi', () => ({ createCandidate: vi.fn(), createResult: vi.fn(), listCandidates: vi.fn(), listResults: vi.fn() }));
vi.mock('../src/workApi', () => ({ listRuns: vi.fn() }));
afterEach(() => vi.resetAllMocks());

it('preserves the saved result if automatic review fails and retries review without duplicating the result', async () => {
  const stamp = '2026-10-03T12:00:00Z';
  const run: Run = { id: 'run-1', project_id: 'project-1', work_order_id: 'work-1', work_order_title: 'Research brief',
    work_order_instructions: 'Find sources', profile_id: 'profile-1', profile_model: null, mode: 'manual_planning', status: 'done',
    tokens_used: 0, cost_usd: 0, outcome_reason: 'Checked', started_at: stamp, finished_at: stamp };
  const result: SourceResult = { id: 'result-1', project_id: 'project-1', actor_id: 'operator', run_id: 'run-1', summary: 'Verified research', scope: 'project', source_kind: 'operator_entered', created_at: stamp };
  const candidate: Candidate = { id: 'candidate-1', project_id: 'project-1', actor_id: 'operator', source_result_id: 'result-1', summary: result.summary, scope: 'project', status: 'pending', created_at: stamp, reviewed_at: null, reviewed_by_actor_id: null };
  vi.mocked(listRuns).mockResolvedValue([run]);
  vi.mocked(listResults).mockResolvedValueOnce([]).mockResolvedValue([result]);
  vi.mocked(listCandidates).mockResolvedValue([]);
  vi.mocked(createResult).mockResolvedValue(result);
  vi.mocked(createCandidate).mockRejectedValueOnce(new Error('Review service unavailable')).mockResolvedValue(candidate);
  render(<ResultIntake projectId="project-1" reloadToken={0} onChanged={vi.fn()} />);
  const user = userEvent.setup();
  await screen.findByRole('option', { name: /Research brief/ });
  await user.click(screen.getByRole('button', { name: 'Save and send for review' }));
  expect(createResult).not.toHaveBeenCalled();
  await user.selectOptions(screen.getByLabelText('Completed run'), 'run-1');
  await user.type(screen.getByLabelText('Result summary'), result.summary);
  await user.click(screen.getByRole('button', { name: 'Save and send for review' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Your result is saved, but review could not be confirmed. Review service unavailable');
  expect(await screen.findByText(result.summary)).toBeInTheDocument();
  vi.mocked(listCandidates).mockResolvedValue([candidate]);
  await user.click(screen.getByRole('button', { name: 'Send for memory review' }));
  expect(await screen.findByText('Candidate pending')).toBeInTheDocument();
  expect(createResult).toHaveBeenCalledTimes(1);
  expect(createCandidate).toHaveBeenCalledTimes(2);
});
