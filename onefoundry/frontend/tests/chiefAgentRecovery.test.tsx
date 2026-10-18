import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChiefAgentRecovery } from '../src/ChiefAgentRecovery';
import type { TeamSessionView } from '../src/taskSessionApi';

const view = { session: { id: 'child-1', profile_id: 'agent-1', status: 'closed', execution_mode: 'team' },
  messages: [], children: [], assignments: [], decision_causes: [],
  attempts: [{ id: 'attempt-1', session_id: 'child-1', status: 'unknown', unknown_acknowledged: false,
    client_attempt_id: null, reason: 'runner_interrupted', input_tokens: null, output_tokens: null, cost_usd: null,
    created_at: '2026-10-18T12:00:00Z', finished_at: '2026-10-18T12:01:00Z' }],
  tool_calls: [{ id: 'call-1', session_id: 'child-1', attempt_id: 'attempt-1', tool_name: 'onefoundry.report_result',
    status: 'started', result: null, reason: null, created_at: '2026-10-18T12:00:00Z', finished_at: null }] } as TeamSessionView;

afterEach(() => vi.unstubAllGlobals());

it('keeps abandonment disabled until the exact saved action can be reconciled', async () => {
  const fetch = vi.fn(async (path: string) => {
    expect(path).toBe('/api/projects/project-1/work-orders/order-1/team-session/sessions/child-1/reconcile');
    return new Response(JSON.stringify({ detail: 'Dependency unavailable' }), { status: 503 });
  });
  vi.stubGlobal('fetch', fetch);
  const changed = vi.fn(async () => view);
  render(<ChiefAgentRecovery projectId="project-1" orderId="order-1" view={view} onChanged={changed} />);
  const user = userEvent.setup();
  expect(screen.getByRole('button', { name: 'Abandon unverified result' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Reconcile saved actions' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Dependency unavailable');
  expect(changed).not.toHaveBeenCalled();
  expect(fetch).toHaveBeenCalledOnce();
  expect(fetch.mock.calls[0]?.[0]).toBe('/api/projects/project-1/work-orders/order-1/team-session/sessions/child-1/reconcile');
});

it('offers a saved receipt check for an unknown turn without started tool calls', async () => {
  const fetch = vi.fn(async () => new Response(JSON.stringify({ reconciled_actions: 0 }), { status: 200 }));
  vi.stubGlobal('fetch', fetch);
  const changed = vi.fn(async () => ({ ...view, tool_calls: [] }));
  render(<ChiefAgentRecovery projectId="project-1" orderId="order-1" view={{ ...view, tool_calls: [] }} onChanged={changed} />);
  const user = userEvent.setup();
  expect(screen.getByRole('button', { name: 'Abandon unverified result' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Reconcile saved actions' }));
  expect(await screen.findByText('0 saved action receipts reconciled. No agent turn was launched.')).toBeInTheDocument();
  expect(changed).toHaveBeenCalledOnce();
});

it('does not offer abandonment for a saved terminal result that was reconciled', () => {
  const changed = vi.fn(async () => view);
  render(<ChiefAgentRecovery projectId="project-1" orderId="order-1" view={{ ...view, tool_calls: [],
    attempts: [{ ...view.attempts[0], terminal_result_reconciled: true }] }} onChanged={changed} />);
  expect(screen.queryByRole('region', { name: 'Agent recovery' })).not.toBeInTheDocument();
});

it('keeps abandonment disabled when exact readback finds the saved terminal result', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ reconciled_actions: 1 }), { status: 200 })));
  const changed = vi.fn(async () => ({ ...view, tool_calls: [],
    attempts: [{ ...view.attempts[0], terminal_result_reconciled: true }] }));
  render(<ChiefAgentRecovery projectId="project-1" orderId="order-1" view={{ ...view, tool_calls: [] }} onChanged={changed} />);
  await userEvent.setup().click(screen.getByRole('button', { name: 'Reconcile saved actions' }));
  expect(await screen.findByText('1 saved action receipts reconciled. No agent turn was launched.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Abandon unverified result' })).toBeDisabled();
});
