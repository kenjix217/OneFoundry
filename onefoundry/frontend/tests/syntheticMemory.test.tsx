import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { SyntheticMemoryIntake } from '../src/SyntheticMemoryIntake';
import { proposeSyntheticCandidate } from '../src/memoryApi';
import type { Run } from '../src/workApi';

const projectId = 'project-1';
const time = '2026-09-26T10:00:00Z';
const run: Run = {
  id: 'run-1', project_id: projectId, work_order_id: 'order-1', profile_id: 'profile-sales',
  work_order_title: 'Local launch', work_order_instructions: 'Use approved context',
  profile_model: null, status: 'done', mode: 'synthetic_text', tokens_used: 0,
  cost_usd: 0, outcome_reason: '', started_at: time, finished_at: time,
};
const result = {
  id: 'result-1', project_id: projectId, actor_id: 'operator', run_id: run.id,
  summary: 'Synthetic Sales draft', scope: 'project', source_kind: 'synthetic_sales',
  source_sales_draft_id: 'draft-1', source_runner_job_id: null, source_sha256: 'a'.repeat(64),
  created_at: time,
};
const candidate = {
  id: 'candidate-1', project_id: projectId, actor_id: 'operator', source_result_id: result.id,
  summary: result.summary, scope: 'project', status: 'pending', created_at: time,
  reviewed_at: null, reviewed_by_actor_id: null,
};
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}
afterEach(() => vi.unstubAllGlobals());

it('requires an explicit click, confirms saved source and candidate, and leaves review pending', async () => {
  let staged = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path === `/api/projects/${projectId}/results`) return json(staged ? [result] : []);
    if (path === `/api/projects/${projectId}/candidates`) return json(staged ? [candidate] : []);
    if (path.endsWith('/synthetic-memory-candidate') && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ scope: 'project' });
      staged = true;
      return json({ result, candidate }, 201);
    }
    throw new Error(`Unexpected ${init?.method ?? 'GET'} ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const changed = vi.fn();
  const user = userEvent.setup();
  render(<SyntheticMemoryIntake projectId={projectId} reloadToken={0} onChanged={changed} />);
  const section = await screen.findByRole('region', { name: 'Stage a completed run' });
  expect(await within(section).findByRole('option', { name: /Local launch/ })).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/synthetic-memory-candidate'))).toBe(false);
  await user.selectOptions(within(section).getByLabelText('Completed synthetic run'), run.id);
  await user.click(within(section).getByRole('button', { name: 'Stage DreamCycle candidate' }));
  expect(await within(section).findByText(/Memory has not been published/)).toBeInTheDocument();
  expect(changed).toHaveBeenCalledOnce();
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/synthetic-memory-candidate'))).toHaveLength(1);
});

it('rejects a synthetic proposal with mismatched project or result lineage', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({
    result: { ...result, project_id: 'other-project' }, candidate,
  })));
  await expect(proposeSyntheticCandidate(projectId, run.id, 'project')).rejects.toThrow('invalid project scope');
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({
    result, candidate: { ...candidate, source_result_id: 'other-result' },
  })));
  await expect(proposeSyntheticCandidate(projectId, run.id, 'project')).rejects.toThrow('invalid synthetic proposal lineage');
});

it('shows an ineligible source conflict without retrying the write', async () => {
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path === `/api/projects/${projectId}/results`) return json([]);
    if (path.endsWith('/synthetic-memory-candidate') && init?.method === 'POST') {
      return json({ detail: 'confirmed Mailpit delivery is required' }, 409);
    }
    throw new Error(`Unexpected ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<SyntheticMemoryIntake projectId={projectId} reloadToken={0} onChanged={vi.fn()} />);
  const section = await screen.findByRole('region', { name: 'Stage a completed run' });
  await user.selectOptions(within(section).getByLabelText('Completed synthetic run'), run.id);
  await user.click(within(section).getByRole('button', { name: 'Stage DreamCycle candidate' }));
  expect(await within(section).findByRole('alert')).toHaveTextContent('confirmed Mailpit delivery is required');
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/synthetic-memory-candidate'))).toHaveLength(1);
});
