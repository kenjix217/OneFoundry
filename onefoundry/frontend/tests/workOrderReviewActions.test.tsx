import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { WorkOrderReviewActions } from '../src/WorkOrderReviewActions';
import type { WorkOrder } from '../src/workApi';

const projectId = 'project-1';
const order: WorkOrder = {
  id: 'order-1', project_id: projectId, title: 'Prepare result', instructions: 'Deliver result',
  profile_id: 'profile-1', deliverable_kind: 'operator_review', acceptance_checks: ['Show saved result'],
  parent_work_order_id: null, source_run_id: null, origin_signal_id: null, chain_depth: 0, status: 'needs_review', version: 2,
  block_reason: null, blocked_from_status: null, created_at: '2026-09-30T10:00:00Z',
};

afterEach(() => vi.unstubAllGlobals());

it('requires a reason and confirms the saved Accept transition before reporting success', async () => {
  const saved = { ...order, status: 'done', version: 3 };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/accept') && init?.method === 'POST') return new Response(JSON.stringify(saved), { status: 200 });
    if (path.endsWith('/work-orders')) return new Response(JSON.stringify([saved]), { status: 200 });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const onSaved = vi.fn();
  const user = userEvent.setup();
  render(<WorkOrderReviewActions projectId={projectId} order={order} onSaved={onSaved} />);
  const accept = screen.getByRole('button', { name: 'Accept' });
  expect(accept).toBeDisabled();
  await user.type(screen.getByLabelText('Reason for your decision'), 'Reviewed saved plan');
  await user.click(accept);
  expect(await screen.findByText('Accepted and recorded in Activity.')).toBeInTheDocument();
  expect(onSaved).toHaveBeenCalledOnce();
  expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual({
    reason: 'Reviewed saved plan', expected_version: 2,
  });
});
