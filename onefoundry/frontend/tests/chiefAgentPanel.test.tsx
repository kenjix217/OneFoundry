import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { ChiefAgentPanel } from '../src/ChiefAgentPanel';
import type { Profile } from '../src/api';
import type { TeamAttempt, TeamSessionView } from '../src/taskSessionApi';

const main = { session: { id: 'chief-session', profile_id: 'chief', status: 'open', execution_mode: 'team' },
  children: [{ id: 'child-1', profile_id: 'gtm', status: 'closed', parent_session_id: 'chief-session' }],
  assignments: [{ id: 'assignment-1', child_session_id: 'child-1', objective: 'Review the pricing draft.', expected_result: '',
    acceptance_check: '', status: 'blocked', result_message_id: null }],
  messages: [], attempts: [], decision_causes: [], tool_calls: [] } as TeamSessionView;
const attempt = { id: 'attempt-1', session_id: 'child-1', status: 'failed', reason: 'memory_embedding_timeout',
  created_at: '2026-10-18T12:00:00Z', finished_at: '2026-10-18T12:00:01Z' } as TeamAttempt;
const profiles = [{ id: 'gtm', name: 'GTM' }] as Profile[];
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
afterEach(() => vi.unstubAllGlobals());

function renderPanel(saved: TeamSessionView) {
  vi.stubGlobal('fetch', vi.fn(async () => json(saved)));
  render(<ChiefAgentPanel projectId="project-1" orderId="order-1" sessionId="child-1" main={main}
    profiles={profiles} files={[]} initialTab="conversation" onClose={vi.fn()} />);
}

it('explains an empty failed memory lookup without exposing the internal reason', async () => {
  renderPanel({ ...main, session: { ...main.session, id: 'child-1' }, attempts: [attempt] });
  const panel = screen.getByRole('dialog', { name: 'GTM' });
  expect(await within(panel).findByText('Memory lookup timed out before the agent started. Chief can review the recorded attempt.')).toBeInTheDocument();
  expect(within(panel).getByRole('tab', { name: 'Context' })).toBeInTheDocument();
  expect(within(panel).getByRole('tab', { name: 'Files' })).toBeInTheDocument();
  expect(panel).not.toHaveTextContent('memory_embedding_timeout');
});

it('explains another known failed attempt without inventing a reply', async () => {
  renderPanel({ ...main, session: { ...main.session, id: 'child-1' }, attempts: [{ ...attempt, reason: 'provider_unavailable' }] });
  expect(await screen.findByText('The agent stopped before saving a reply. Chief can review the recorded attempt.')).toBeInTheDocument();
});

it('does not claim failure for an unknown outcome or a conversation with a saved reply', async () => {
  renderPanel({ ...main, session: { ...main.session, id: 'child-1' }, attempts: [attempt, { ...attempt, id: 'attempt-2', status: 'unknown',
    created_at: '2026-10-18T12:01:00Z' }] });
  expect(await screen.findByText('No conversation reply has been saved yet. Review recorded actions and earlier attempts for the current state.')).toBeInTheDocument();
  expect(screen.queryByText(/agent stopped before saving/)).not.toBeInTheDocument();
});

it('keeps a saved reply visible instead of showing the empty failure state', async () => {
  renderPanel({ ...main, session: { ...main.session, id: 'child-1' }, attempts: [attempt], messages: [{ id: 'message-1',
    session_id: 'child-1', client_message_id: null, sequence: 1, actor_id: 'profile:gtm', kind: 'agent',
    body: 'Saved GTM findings.', created_at: '2026-10-18T12:00:00Z' }] });
  expect(await screen.findByText('Saved GTM findings.')).toBeInTheDocument();
  expect(screen.queryByText(/Memory lookup timed out/)).not.toBeInTheDocument();
});

it('shows a loading state before any child readback instead of calling it failed', () => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => undefined)));
  render(<ChiefAgentPanel projectId="project-1" orderId="order-1" sessionId="child-1" main={main}
    profiles={profiles} files={[]} initialTab="conversation" onClose={vi.fn()} />);
  const panel = screen.getByRole('dialog', { name: 'GTM' });
  expect(within(panel).getByText('Reading saved conversation…')).toBeInTheDocument();
  expect(within(panel).queryByText(/stopped before saving|Memory lookup timed out/)).not.toBeInTheDocument();
});
