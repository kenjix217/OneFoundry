import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { SalesPanel } from '../src/SalesPanel';

const projectId = 'project-1';
const runId = 'run-1';
const root = `/api/projects/${projectId}/runs/${runId}`;
const draft = {
  id: 'draft-1', project_id: projectId, source_run_id: runId, run_id: runId,
  approval_id: 'approval-1', actor_id: 'profile:sales-1',
  recipient: 'sales-preview@onefoundry.invalid', subject: 'OneFoundry local preview',
  body: 'Synthetic GTM continuation', body_sha256: 'digest', delivery_status: 'pending',
  sent_at: null, created_at: '2026-09-26T12:00:00Z',
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('creates and reads back only a local Sales draft before approval', async () => {
  let stored = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `${root}/sales-eligibility`) {
      return json({ eligible: !stored, reason: stored ? 'Sales draft already exists' : null, sales_profile_id: 'sales-1' });
    }
    if (path === `${root}/sales-draft` && init?.method === 'POST') {
      stored = true;
      return json(draft, 201);
    }
    if (path === `${root}/sales-draft`) return stored ? json(draft) : json({ detail: 'Sales draft not found' }, 404);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const changed = vi.fn();
  const user = userEvent.setup();
  render(<SalesPanel projectId={projectId} runId={runId} onChanged={changed} />);
  await user.click(await screen.findByRole('button', { name: 'Create local Sales draft' }));
  expect(await screen.findByText(/sales-preview@onefoundry.invalid/)).toBeInTheDocument();
  expect(screen.getByText('Synthetic GTM continuation')).toBeInTheDocument();
  expect(screen.getByText(/Delivery:/).parentElement).toHaveTextContent('pending');
  expect(changed).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  expect(fetcher.mock.calls.every(([path]) => path.startsWith(root))).toBe(true);
});
