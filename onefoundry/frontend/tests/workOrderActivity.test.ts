import { expect, it } from 'vitest';

import { describeActivity, workOrderNow, workOrderStatusLabel } from '../src/workOrderActivity';
import type { RunEvent, WorkOrder } from '../src/workApi';

const order: WorkOrder = { id: 'order-1', project_id: 'project-1', title: 'Fix import',
  instructions: 'Find the failing path', profile_id: 'engineer-1', status: 'unknown', version: 2,
  deliverable_kind: 'operator_review', acceptance_checks: [], parent_work_order_id: null, source_run_id: null, origin_signal_id: null, chain_depth: 0,
  block_reason: null, blocked_from_status: null, created_at: '2026-09-29T10:00:00Z' };

it('keeps uncertain outcomes uncertain and uses a safe label for an unfamiliar event', () => {
  expect(workOrderNow(order, []).now).toMatch(/uncertain/);
  const event: RunEvent = { id: 'event-1', project_id: order.project_id, work_order_id: order.id,
    run_id: null, event_type: 'future_event', actor_id: 'agent-1', payload: { internal_code: 'xyz' },
    created_at: '2026-09-29T10:01:00Z' };
  const activity = describeActivity(event);
  expect(activity.title).toBe('Additional activity recorded');
  expect(activity.detail).not.toContain('xyz');
});

it('reports a denied tool action without claiming a result', () => {
  const event: RunEvent = { id: 'event-2', project_id: order.project_id, work_order_id: order.id,
    run_id: 'run-1', event_type: 'role_tool_denied', actor_id: 'agent-1',
    payload: { tool_id: 'web_search', reason: 'not_admitted' }, created_at: '2026-09-29T10:02:00Z' };
  const activity = describeActivity(event);
  expect(activity.title).toBe('web search · denied');
  expect(activity.detail).toMatch(/No tool result/);
});

it('uses readable work order states in the queue and task header', () => {
  expect(workOrderStatusLabel('blocked')).toBe('Waiting for you');
  expect(workOrderStatusLabel('unknown')).toBe('Outcome uncertain');
  expect(workOrderStatusLabel('needs_review')).toBe('Needs review');
  const opened: RunEvent = { id: 'event-3', project_id: order.project_id, work_order_id: order.id,
    run_id: null, event_type: 'task_conversation_opened', actor_id: 'operator', payload: {},
    created_at: '2026-09-29T10:03:00Z' };
  expect(describeActivity(opened).detail).toBe('A chat was saved under this work order.');
});

it('shows the operator decision reason in Activity', () => {
  const accepted: RunEvent = { id: 'event-4', project_id: order.project_id, work_order_id: order.id,
    run_id: null, event_type: 'work_order_accepted', actor_id: 'operator',
    payload: { reason: 'Reviewed the saved deliverable' }, created_at: '2026-09-29T10:04:00Z' };
  expect(describeActivity(accepted).detail).toContain('Reviewed the saved deliverable');
});
