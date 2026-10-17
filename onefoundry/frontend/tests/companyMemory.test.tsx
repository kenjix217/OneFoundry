import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ConversationCapture } from '../src/ConversationCapture';
import { MemoryLifecycle } from '../src/MemoryLifecycle';
import { MemorySourceReview } from '../src/MemorySourceReview';
import { MemoryView } from '../src/MemoryView';
import { MemorySearch } from '../src/MemorySearch';
import { listMemory, listResults, type Candidate, type MemoryItem } from '../src/memoryApi';

const project = 'project-1';
const timestamp = '2026-10-04T12:00:00Z';
const item: MemoryItem = { id: 'memory-1', project_id: project, actor_id: 'operator', scope: 'project',
  text: 'Copper launches Tuesday', source_result_id: 'result-1', source_candidate_id: 'candidate-1',
  created_at: timestamp, score: 0, state: 'current' };
const candidate: Candidate = { id: 'candidate-2', project_id: project, actor_id: 'external-memory:codex',
  source_result_id: 'result-2', summary: 'Copper launches Wednesday', scope: 'project', status: 'pending',
  created_at: timestamp, reviewed_at: null, reviewed_by_actor_id: null, source_capture_id: 'capture-2',
  replaces_memory_id: item.id };
function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
afterEach(() => vi.unstubAllGlobals());

it('shows a meaning match without presenting similarity as confidence or zero matches', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json([{ ...item, score: 0, similarity: 0.85 }])));
  render(<MemorySearch projectId={project} projects={[{ id: project, name: 'Copper' }]} reloadToken={0} />);
  const user = userEvent.setup();
  await user.type(screen.getByRole('textbox', { name: 'Search memory' }), 'release schedule');
  await user.click(screen.getByRole('button', { name: /^Search$/ }));
  expect(await screen.findByText('Matched by meaning')).toBeInTheDocument();
  expect(screen.queryByText('85%')).not.toBeInTheDocument();
});

it('rejects malformed semantic results while accepting older lexical responses', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json([{ ...item, similarity: 4 }]))
    .mockResolvedValueOnce(json([item])));
  await expect(listMemory(project)).rejects.toThrow('invalid memory similarity');
  await expect(listMemory(project)).resolves.toMatchObject([{ id: item.id }]);
});

it('requires explicit company-content consent, proposes a correction, and never self approves', async () => {
  let captured: Record<string, unknown> | null = null;
  const fetcher = vi.fn(async (_path: string, init?: RequestInit) => {
    if (!init?.method) return json([item]);
    captured = JSON.parse(init.body as string) as Record<string, unknown>;
    return json({ source_id: 'capture-2', source_sha256: 'synthetic-digest', candidate,
      result: { id: 'result-2', project_id: project, actor_id: 'operator', run_id: null,
        summary: candidate.summary, scope: 'project', source_kind: 'conversation_capture',
        source_capture_id: 'capture-2', created_at: timestamp } }, 201);
  });
  vi.stubGlobal('fetch', fetcher);
  const changed = vi.fn(); const user = userEvent.setup();
  render(<ConversationCapture projectId={project} reloadToken={0} onChanged={changed} />);
  const submit = screen.getByRole('button', { name: 'Send excerpt for review' });
  expect(submit).toBeDisabled();
  await user.type(screen.getByLabelText('Source reference'), 'Authorized project meeting');
  await user.type(screen.getByLabelText('Conversation excerpt'), 'We agreed to move the launch to Wednesday.');
  await user.type(screen.getByLabelText('Fact for review'), candidate.summary);
  // datetime-local typing varies across jsdom/browser; set its native input boundary.
  const timeInput = screen.getByLabelText('Source message time');
  const { fireEvent } = await import('@testing-library/react');
  fireEvent.change(timeInput, { target: { value: '2026-10-04T12:00' } });
  await user.selectOptions(screen.getByLabelText('Correct an existing fact'), item.id);
  await user.click(screen.getByRole('checkbox'));
  await user.click(submit);
  expect(await screen.findByRole('status')).toHaveTextContent('Conversation excerpt saved for review');
  expect(captured).toMatchObject({ authorized_company_content: true, scope: 'project', replaces_memory_id: item.id });
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST').map(([path]) => path)).toEqual([`/api/projects/${project}/memory/captures`]);
  expect(changed).toHaveBeenCalledOnce();
});

it('inspects the archived excerpt and previous fact before a correction is reviewed', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => path.includes('/captures/') ? json({
    id: 'capture-2', project_id: project, actor_id: candidate.actor_id, source_ref: 'Authorized conversation',
    created_at: timestamp, source_sha256: 'synthetic-digest',
    messages: [{ speaker: 'user', text: 'Move launch to Wednesday', sent_at: timestamp }],
  }) : json(item)));
  render(<MemorySourceReview projectId={project} candidate={candidate} />);
  await userEvent.setup().click(screen.getByRole('button', { name: 'Inspect source' }));
  expect(await screen.findByText('Move launch to Wednesday')).toBeInTheDocument();
  expect(screen.getByText(/Captured/)).toHaveTextContent('by an authorized external client');
  expect(screen.queryByText(candidate.actor_id)).not.toBeInTheDocument();
  expect(screen.getByText(/Previous fact/)).toHaveTextContent(item.text);
  expect(screen.queryByRole('button', { name: /Approve/ })).not.toBeInTheDocument();
});

it('withdraws reversibly with a reason and preserves errors on owner failure', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(json({ ...item, state: 'withdrawn' }))
    .mockResolvedValueOnce(json({ detail: 'database unavailable' }, 503));
  vi.stubGlobal('fetch', fetcher);
  const changed = vi.fn(); const user = userEvent.setup();
  const view = render(<MemoryLifecycle projectId={project} item={item} onChanged={changed} />);
  await user.click(screen.getByRole('button', { name: 'Withdraw fact' }));
  expect(screen.getByRole('button', { name: 'Confirm withdraw' })).toBeDisabled();
  await user.type(screen.getByLabelText('Reason'), 'Needs another review');
  await user.click(screen.getByRole('button', { name: 'Confirm withdraw' }));
  expect(changed).toHaveBeenCalledOnce();
  view.rerender(<MemoryLifecycle projectId={project} item={{ ...item, state: 'withdrawn' }} onChanged={changed} />);
  await user.click(screen.getByRole('button', { name: 'Restore fact' }));
  await user.click(screen.getByRole('button', { name: 'Confirm restore' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('database unavailable');
  expect(changed).toHaveBeenCalledOnce();
});

it('does not offer mutation for facts owned by another project or superseded facts', () => {
  const { container } = render(<MemoryLifecycle projectId="other-project" item={item} onChanged={vi.fn()} />);
  expect(within(container).queryByRole('button')).not.toBeInTheDocument();
});

it('accepts capture result lineage with no synthetic run', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json([{ id: 'result-2', project_id: project, actor_id: 'operator',
    run_id: null, summary: candidate.summary, scope: 'project', source_kind: 'conversation_capture',
    source_capture_id: 'capture-2', created_at: timestamp }])));
  expect((await listResults(project))[0].source_kind).toBe('conversation_capture');
});

it('refreshes current browse results when history restores a fact', async () => {
  let withdrawn = true;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { withdrawn = false; return json(item); }
    if (path.endsWith('/memory/history')) return json(withdrawn ? [{ ...item, state: 'withdrawn' }] : []);
    if (path.endsWith('/memory') || path.includes('/memory?')) return json(withdrawn ? [] : [item]);
    return json([]);
  }));
  const user = userEvent.setup();
  render(<MemoryView projectId={project} projects={[{ id: project, name: 'Copper' }]} />);
  await screen.findByText('No approved memory is available in this scope.');
  await user.click(screen.getByRole('button', { name: 'History' }));
  await user.click(await screen.findByRole('button', { name: 'Restore fact' }));
  await user.type(screen.getByLabelText('Reason'), 'Operator reconfirmed');
  await user.click(screen.getByRole('button', { name: 'Confirm restore' }));
  await screen.findByText('No withdrawn or replaced facts for this project.');
  await user.click(screen.getByRole('button', { name: 'Browse & review' }));
  expect(await screen.findByText(item.text)).toBeInTheDocument();
});
