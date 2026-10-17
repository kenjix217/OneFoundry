import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { MemorySearch } from '../src/MemorySearch';
import { MemoryView } from '../src/MemoryView';
import { listMemory, reviewCandidate, type Candidate, type MemoryItem, type SourceResult } from '../src/memoryApi';
import type { Run } from '../src/workApi';

const projectId = 'project-1';
const otherProjectId = 'project-2';
const timestamp = '2026-09-25T12:00:00Z';
const projects = [{ id: projectId, name: 'Studio' }, { id: otherProjectId, name: 'Field' }];
const doneRun = (id: string, title: string): Run => ({
  id, project_id: projectId, work_order_id: `work-${id}`, work_order_title: title,
  work_order_instructions: 'Manual plan', profile_id: 'profile-1', profile_model: null,
  status: 'done', mode: 'manual_planning', tokens_used: 0, cost_usd: 0,
  outcome_reason: 'Reviewed', started_at: timestamp, finished_at: timestamp,
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('saves local results, approves one candidate, rejects another, and retrieves only approved text', async () => {
  const runs = [doneRun('run-1', 'Plan alpha'), doneRun('run-2', 'Plan beta'),
    { ...doneRun('run-3', 'Synthetic result'), mode: 'synthetic_text' as const }];
  const results: SourceResult[] = [];
  const candidates: Candidate[] = [];
  const memory: MemoryItem[] = [];
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs`) return json(runs);
    if (path === `/api/projects/${projectId}/results` && !init?.method) return json(results);
    if (path === `/api/projects/${projectId}/candidates` && !init?.method) return json(candidates);
    if (path.startsWith(`/api/projects/${projectId}/memory`) && !init?.method) {
      const query = new URL(`http://localhost${path}`).searchParams.get('query')?.toLowerCase() ?? '';
      return json(memory.filter((item) => !query || item.text.toLowerCase().includes(query))
        .map((item) => ({ ...item, score: query ? 1 : 0 })));
    }
    const resultRun = path.match(/\/runs\/(run-\d+)\/results$/);
    if (resultRun && init?.method === 'POST') {
      const fields = JSON.parse(init.body as string) as Pick<SourceResult, 'summary' | 'scope'>;
      const created: SourceResult = { id: `result-${results.length + 1}`, project_id: projectId,
        actor_id: 'operator', run_id: resultRun[1], source_kind: 'operator_entered',
        created_at: timestamp, ...fields };
      results.push(created);
      return json(created, 201);
    }
    const candidateResult = path.match(/\/results\/(result-\d+)\/candidates$/);
    if (candidateResult && init?.method === 'POST') {
      const source = results.find((item) => item.id === candidateResult[1]);
      if (!source) throw new Error('Missing result');
      const created: Candidate = { id: `candidate-${candidates.length + 1}`, project_id: projectId,
        actor_id: 'operator', source_result_id: source.id, summary: source.summary,
        scope: source.scope, status: 'pending', created_at: timestamp,
        reviewed_at: null, reviewed_by_actor_id: null };
      candidates.push(created);
      return json(created, 201);
    }
    const reviewId = path.match(/\/candidates\/(candidate-\d+)\/review$/);
    if (reviewId && init?.method === 'POST') {
      const item = candidates.find((candidate) => candidate.id === reviewId[1]);
      if (!item) throw new Error('Missing candidate');
      item.status = (JSON.parse(init.body as string) as { decision: 'approved' | 'rejected' }).decision;
      item.reviewed_at = timestamp;
      item.reviewed_by_actor_id = 'operator';
      if (item.status === 'approved') memory.push({ id: `memory-${memory.length + 1}`,
        project_id: projectId, actor_id: 'operator', scope: item.scope, text: item.summary,
        source_result_id: item.source_result_id, source_candidate_id: item.id,
        created_at: timestamp, score: 0 });
      return json(item);
    }
    throw new Error(`Unexpected request ${init?.method ?? 'GET'} ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<MemoryView projectId={projectId} projects={projects} />);
  await user.click(screen.getByRole('button', { name: 'Import verified results' }));
  expect(await within(screen.getByRole('region', { name: 'Stage a completed run' })).findByRole('option', { name: /Synthetic result/ })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Add a result' }));
  const intake = await screen.findByRole('region', { name: 'Completed-run results' });
  await screen.findByText('No approved memory is available in this scope.');

  expect(within(intake).queryByRole('option', { name: 'Synthetic result' })).not.toBeInTheDocument();

  await user.selectOptions(within(intake).getByLabelText('Completed run'), 'run-1');
  await user.type(within(intake).getByLabelText('Result summary'), 'Cedar atlas is approved knowledge');
  await user.selectOptions(within(intake).getByLabelText('Memory scope'), 'company');
  await user.click(within(intake).getByRole('button', { name: 'Save and send for review' }));
  expect(await within(intake).findByText('Cedar atlas is approved knowledge')).toBeInTheDocument();
  expect(await within(intake).findByText('Candidate pending')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Browse & review' }));
  const review = screen.getByRole('region', { name: 'Memory candidates' });
  expect(await within(review).findByText('Cedar atlas is approved knowledge')).toBeInTheDocument();
  await user.click(within(review).getByRole('button', { name: 'Approve to memory' }));
  await within(review).findByText('Candidate approved. Search memory to inspect the published source-linked item.');

  const search = screen.getByRole('region', { name: 'Browse and search' });
  await user.type(within(search).getByLabelText('Search memory'), 'Cedar');
  await user.click(within(search).getByRole('button', { name: 'Search' }));
  expect(await within(search).findByText('Cedar atlas is approved knowledge')).toBeInTheDocument();
  expect(within(search).queryByText('result-1')).not.toBeInTheDocument();
  expect(within(search).queryByText('candidate-1')).not.toBeInTheDocument();
  expect(within(search).getByText('Search matches')).toHaveTextContent('Search matches 1');

  await user.click(screen.getByRole('button', { name: 'Add a result' }));
  await user.selectOptions(within(intake).getByLabelText('Completed run'), 'run-2');
  await user.type(within(intake).getByLabelText('Result summary'), 'Maple atlas must stay out of memory');
  await user.selectOptions(within(intake).getByLabelText('Memory scope'), 'project');
  await user.click(within(intake).getByRole('button', { name: 'Save and send for review' }));
  expect(await within(intake).findByText('Maple atlas must stay out of memory')).toBeInTheDocument();
  expect(await within(intake).findByText('Result saved and sent for review. Approve it in Browse & review before it becomes shared knowledge.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Browse & review' }));
  expect(await within(review).findByText('Maple atlas must stay out of memory')).toBeInTheDocument();
  await user.click(within(review).getByRole('button', { name: 'Reject candidate' }));
  await within(review).findByText('Candidate rejected. It was not published to memory.');
  await user.clear(within(search).getByLabelText('Search memory'));
  await user.type(within(search).getByLabelText('Search memory'), 'Maple');
  await user.click(within(search).getByRole('button', { name: 'Search' }));
  expect(await within(search).findByText('No approved memory matched this search and scope.')).toBeInTheDocument();
  expect(memory).toHaveLength(1);
  expect(fetcher.mock.calls.every(([path]) => path.startsWith(`/api/projects/${projectId}/`))).toBe(true);
});

it('shows company memory from another project and local project memory separately', async () => {
  const items: MemoryItem[] = [
    { id: 'memory-company', project_id: projectId, actor_id: 'operator', scope: 'company',
      text: 'Shared company guidance', source_result_id: 'result-company',
      source_candidate_id: 'candidate-company', created_at: timestamp, score: 0 },
    { id: 'memory-local', project_id: otherProjectId, actor_id: 'operator', scope: 'project',
      text: 'Field project guidance', source_result_id: 'result-field',
      source_candidate_id: 'candidate-field', created_at: timestamp, score: 0 },
  ];
  const fetcher = vi.fn(async (path: string) => {
    const scope = new URL(`http://localhost${path}`).searchParams.get('scope');
    return json(scope ? items.filter((item) => item.scope === scope) : items);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<MemorySearch projectId={otherProjectId} projects={projects} reloadToken={0} />);
  expect(await screen.findByText('Shared company guidance')).toBeInTheDocument();
  expect(screen.getByText('Field project guidance')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'This project' }));
  expect(await screen.findByText('Field project guidance')).toBeInTheDocument();
  expect(screen.queryByText('Shared company guidance')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Company' }));
  expect(await screen.findByText('Shared company guidance')).toBeInTheDocument();
  expect(screen.queryByText('Field project guidance')).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => path === `/api/projects/${otherProjectId}/memory?scope=project`)).toBe(true);
  expect(fetcher.mock.calls.some(([path]) => path === `/api/projects/${otherProjectId}/memory?scope=company`)).toBe(true);
});

it('requests scope before the server limit so older project memory remains reachable', async () => {
  const newerCompany: MemoryItem[] = Array.from({ length: 20 }, (_, index) => ({
    id: `company-${index}`, project_id: projectId, actor_id: 'operator', scope: 'company',
    text: `Newer company item ${index}`, source_result_id: `result-${index}`,
    source_candidate_id: `candidate-${index}`, created_at: timestamp, score: 0,
  }));
  const olderProject: MemoryItem = { id: 'older-project', project_id: projectId,
    actor_id: 'operator', scope: 'project', text: 'Older project note',
    source_result_id: 'project-result', source_candidate_id: 'project-candidate',
    created_at: '2026-09-24T12:00:00Z', score: 0 };
  const fetcher = vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/memory`) return json(newerCompany);
    if (path === `/api/projects/${projectId}/memory?scope=project`) return json([olderProject]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<MemorySearch projectId={projectId} projects={projects} reloadToken={0} />);
  expect(await screen.findByText('Newer company item 0')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'This project' }));
  expect(await screen.findByText('Older project note')).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => path === `/api/projects/${projectId}/memory?scope=project`)).toBe(true);
});

it('rejects foreign project memory and preserves review conflict errors', async () => {
  const leaked: MemoryItem = { id: 'bad', project_id: projectId, actor_id: 'operator',
    scope: 'project', text: 'Foreign project text', source_result_id: 'result',
    source_candidate_id: 'candidate', created_at: timestamp, score: 0 };
  const fetcher = vi.fn().mockResolvedValueOnce(json([leaked]))
    .mockResolvedValueOnce(json({ detail: 'candidate already reviewed' }, 409));
  vi.stubGlobal('fetch', fetcher);
  await expect(listMemory(otherProjectId)).rejects.toThrow('invalid project scope');
  await expect(reviewCandidate(projectId, 'candidate-1', 'approved')).rejects.toMatchObject({
    status: 409, message: 'candidate already reviewed',
  });
  expect(fetcher).toHaveBeenCalledTimes(2);
});
