import { expect, it, vi } from 'vitest';
import { fireEvent, render as testingRender, screen } from '@testing-library/react';
import type { ReactElement } from 'react';
import userEvent from '@testing-library/user-event';

import { TaskSessionActivity } from '../src/TaskSessionActivity';
import { actionSummary, activityEntries, messageAttempt, messagePresentation, recordedTiming,
  safeDiagnostics, toolIdentity } from '../src/taskSessionActivity';
import type { TeamSessionView } from '../src/taskSessionApi';

const attempt = { id: 'turn', session_id: 'main', client_attempt_id: null, status: 'done' as const,
  input_tokens: null, output_tokens: null, cost_usd: null, reason: null,
  created_at: '2026-10-06T12:00:00Z', started_at: '2026-10-06T12:00:10Z', finished_at: '2026-10-06T12:01:00Z' };
const message = { id: 'message', session_id: 'main', client_message_id: null, sequence: 1,
  actor_id: 'profile:producer', kind: 'agent' as const, body: 'Inspecting the saved artifact.', created_at: '2026-10-06T12:00:30Z' };
const tool = { id: 'tool', session_id: 'main', attempt_id: 'turn', tool_name: 'onefoundry.role_tool',
  status: 'started' as const, result: null, reason: null, created_at: '2026-10-06T12:00:20Z', finished_at: null };
const saved: TeamSessionView = { session: { id: 'main', profile_id: 'producer', status: 'open', execution_mode: 'team' },
  messages: [message], attempts: [attempt], tool_calls: [tool], children: [], assignments: [], decision_causes: [] };

function render(ui: ReactElement) {
  const result = testingRender(ui);
  const toggle = screen.queryByRole('button', { name: /Show agent action activity/ });
  if (toggle) fireEvent.click(toggle);
  return result;
}

it('shows emitted messages while keeping recorded actions collapsed by default', () => {
  testingRender(<TaskSessionActivity view={{ ...saved, messages: [message, { ...message, id: 'scalar', sequence: 2, body: '4' },
    { ...message, id: 'progress', sequence: 3, body: '[Plan] Checking sources' },
    { ...message, id: 'system', sequence: 4, kind: 'system' as const, body: 'Verbose system context.' }] }} profiles={[]} />);
  expect(screen.getByText('Inspecting the saved artifact.')).toBeInTheDocument();
  expect(screen.getByText('4')).toBeInTheDocument();
  expect(screen.getByText('[Plan] Checking sources')).toBeInTheDocument();
  expect(screen.queryByText('Verbose system context.')).not.toBeInTheDocument();
  expect(screen.queryByText('Role tool')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Show agent action activity/ })).toHaveAttribute('aria-expanded', 'false');
});

it('keeps operator JSON and malformed JSON as visible, editable text while hiding agent receipts', () => {
  const receiptText = '{"tool_id":"write_text_artifact_v1","status":"done"}';
  const malformedText = '{draft text without valid JSON';
  const operator = { ...message, id: 'operator-json', kind: 'operator' as const, actor_id: 'operator', body: receiptText };
  const legacy = { ...message, id: 'operator-malformed', kind: 'legacy_operator' as const,
    actor_id: 'operator', sequence: 2, body: malformedText };
  const receipt = { ...message, id: 'agent-receipt', sequence: 3, body: receiptText };
  const onCopy = vi.fn(); const onEdit = vi.fn();
  testingRender(<TaskSessionActivity view={{ ...saved, messages: [operator, legacy, receipt], attempts: [], tool_calls: [] }}
    profiles={[]} onCopyMessage={onCopy} onEditMessage={onEdit} />);
  expect(screen.getByText(receiptText)).toBeInTheDocument();
  expect(screen.getByText(malformedText)).toBeInTheDocument();
  expect(screen.queryByText('Save text artifact')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Show agent action activity (1)' })).toBeInTheDocument();
  fireEvent.click(screen.getAllByRole('button', { name: 'Copy your message' })[0]);
  expect(onCopy).toHaveBeenCalledWith(receiptText);
  fireEvent.click(screen.getAllByRole('button', { name: 'Edit your message' })[1]);
  expect(onEdit).toHaveBeenCalledWith('operator-malformed', malformedText);
});

it('does not invent summaries for unknown or malformed structured messages', () => {
  expect(messagePresentation('{"unknown":42,"reasoning":"private"}')).toEqual({ kind: 'structured', text: 'Structured agent message. A readable summary was not recorded.' });
  expect(messagePresentation('{broken')).toMatchObject({ kind: 'structured' });
  expect(messagePresentation('[]')).toMatchObject({ kind: 'structured' });
  expect(messagePresentation('4')).toEqual({ kind: 'text', text: '4' });
  expect(messagePresentation('true')).toEqual({ kind: 'text', text: 'true' });
  expect(messagePresentation('null')).toEqual({ kind: 'text', text: 'null' });
  expect(messagePresentation('"hello"')).toEqual({ kind: 'text', text: '"hello"' });
  expect(messagePresentation('[docs](https://example.test)')).toMatchObject({ kind: 'text' });
  expect(messagePresentation('[Plan] Checking sources')).toMatchObject({ kind: 'text' });
  expect(messagePresentation('[{"tool_id":')).toMatchObject({ kind: 'structured' });
  expect(messagePresentation('')).toEqual({ kind: 'text', text: '' });
  expect(messagePresentation('{"reply":"Visible reply"}').text).toBe('Visible reply');
  expect(messagePresentation('{"summary":"Visible progress"}')).toMatchObject({ kind: 'text', text: 'Visible progress' });
  expect(messagePresentation('{"summary":"Saved","artifact":{"content":"Contents"}}').text).toBe('Saved\n\nContents');
});

it('sanitizes nested secrets and excludes hidden reasoning from diagnostics', () => {
  const text = safeDiagnostics({ password: 'sensitive', nested: { chain_of_thought: 'private', api_key: 'sensitive',
    log: 'Bearer credential-value password="sensitive" https://person:credential@example.test?q=1&token=credential' }, reasoning_content: 'private', chainOfThought: 'private', encoded: JSON.stringify({ reasoning: 'private', token: 'sensitive' }) });
  expect(text).not.toMatch(/sensitive|private|credential-value|credential@|token=credential/);
  expect(text).toContain('[redacted]');
});

it('uses recorded start time for duration, and reports missing or invalid timing', () => {
  expect(recordedTiming(attempt.started_at, attempt.finished_at)).toBe('Duration: 50s');
  expect(recordedTiming(undefined, attempt.finished_at)).toBe('Duration: not recorded');
  expect(recordedTiming('invalid', null)).toBe('Duration: not recorded');
  expect(recordedTiming(attempt.finished_at, attempt.created_at)).toBe('Duration: not recorded');
});

it('orders activity by recorded start with stable IDs and sequence, retaining repeated text', () => {
  const view = { ...saved, messages: [message, { ...message, id: 'other', sequence: 2 }, message] };
  const entries = activityEntries(view);
  expect(entries.map((entry) => entry.key)).toEqual(['attempt:turn', 'tool:tool', 'message:message', 'message:other']);
  expect(activityEntries({ ...saved, messages: [], attempts: [], tool_calls: [] })).toEqual([]);
});

it('links turns only with exact recorded receipt or source-message references', () => {
  expect(messageAttempt(message, saved)).toBeNull();
  expect(messageAttempt(message, { ...saved, attempts: [{ ...attempt, source_message_id: message.id }] })).toBe('turn');
  expect(messageAttempt(message, { ...saved, tool_calls: [{ ...tool, result: { message_id: message.id } }] })).toBe('turn');
  expect(messageAttempt({ ...message, body: JSON.stringify({ tool_id: 'custom', status: 'done', run_id: 'outside' }) }, saved)).toBeNull();
});

it('shows unknown tool names and owner failures without claiming success', () => {
  expect(toolIdentity('onefoundry.new_tool', null)).toEqual({ name: 'onefoundry.new_tool', label: 'New tool' });
  expect(actionSummary({ result: { status: 'failed', reason: 'owner_unavailable' } })).toBe('owner_unavailable');
  expect(actionSummary(null)).toBe('Result details not recorded.');
  render(<TaskSessionActivity view={{ ...saved, tool_calls: [{ ...tool, tool_name: 'onefoundry.new_tool',
    status: 'failed', reason: 'dependency_unavailable' }] }} profiles={[]} />);
  expect(screen.getByText('New tool')).toBeInTheDocument();
  expect(screen.getByText(/Recorded action error: dependency unavailable/)).toBeInTheDocument();
  expect(screen.getByText(/Turn link: not recorded/)).toBeInTheDocument();
  expect(screen.queryByText('tool')).not.toBeInTheDocument();
});

it.each([
  ['pending', false, 'waiting for result', 'This assignment has not saved a confirmed result yet.'],
  ['blocked', true, 'rework required', 'This assignment is blocked; it has no accepted result.'],
  ['canceled', false, 'assignment canceled', 'This assignment was canceled; it has no current accepted result.'],
])('shows a saved dependency %s observation without a missing-target error', (status, contentAvailable, label, summary) => {
  render(<TaskSessionActivity view={{ ...saved, messages: [], tool_calls: [{ ...tool,
    tool_name: 'onefoundry.read_dependency_result', status: 'done', finished_at: attempt.finished_at,
    result: { status, content_available: contentAvailable, summary, next_step: 'Use the next team turn.' },
  }] }} profiles={[]} />);
  expect(screen.getByText(new RegExp(label))).toBeInTheDocument();
  expect(screen.getByText(new RegExp(summary.replaceAll('.', '\\.')))).toBeInTheDocument();
  expect(screen.queryByText(/Recorded action error|Result details not recorded/)).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Open artifact run' })).not.toBeInTheDocument();
});

it('explains a rejected dependency plan from its saved failure reason', () => {
  render(<TaskSessionActivity view={{ ...saved, messages: [], tool_calls: [{ ...tool,
    tool_name: 'onefoundry.delegate_agent', status: 'failed', reason: 'dependency_not_accepted',
  }] }} profiles={[]} />);
  expect(screen.getByText(/This plan depends on rejected or canceled work/)).toBeInTheDocument();
  expect(screen.getByText(/Recorded action error: dependency not accepted/)).toBeInTheDocument();
  expect(screen.queryByText('Result details not recorded.')).not.toBeInTheDocument();
});

it('keeps long text and sanitized diagnostics collapsed, with artifact navigation', async () => {
  const user = userEvent.setup(); const open = vi.fn();
  const receipt = { status: 'done', tool_id: 'write_text_artifact_v1', run_id: 'turn',
    result: { artifact_id: 'artifact', kind: 'markdown', byte_size: 13018 }, secret: 'credential' };
  render(<TaskSessionActivity view={{ ...saved, messages: [{ ...message, body: 'Long log\n'.repeat(250) }],
    tool_calls: [{ ...tool, status: 'done', result: receipt }] }} profiles={[]} onOpenRun={open} />);
  expect(document.querySelector('pre')).toBeNull();
  await user.click(screen.getByText(/Read full text/));
  expect(screen.getByText((_text, element) => element?.tagName === 'P' && element.textContent === 'Long log\n'.repeat(250))).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open artifact run' }));
  expect(open).toHaveBeenCalledWith('turn');
  await user.click(screen.getAllByText('Technical details')[1]);
  expect(document.querySelector('pre')?.textContent).toContain('[redacted]');
  expect(document.querySelector('pre')?.textContent).not.toContain('credential');
});

it('keeps Chief inspection JSON in diagnostics while showing a readable receipt', async () => {
  render(<TaskSessionActivity view={{ ...saved, tool_calls: [{ ...tool,
    tool_name: 'onefoundry.read_project_status', status: 'done',
    result: { content: '{"internal_record":"inspection-test-record"}', body_sha256: 'a'.repeat(64),
      created_before: '2026-10-07T12:00:00Z', next_content_offset: null, next_offset: null },
  }] }} profiles={[]} />);
  expect(screen.getByRole('heading', { name: 'Read project status' })).toBeInTheDocument();
  expect(screen.getByText('Saved workflow records read. The full receipt is in Technical details.')).toBeInTheDocument();
  expect(screen.queryByText(/inspection-test-record/)).not.toBeInTheDocument();
  await userEvent.setup().click(screen.getAllByText('Technical details')[1]);
  expect(document.querySelector('pre')?.textContent).toContain('inspection-test-record');
});

it('updates streamed records by identity and shows retry history without duplication', async () => {
  const { rerender } = render(<TaskSessionActivity view={saved} profiles={[]} />);
  await userEvent.setup().click(screen.getAllByText('Technical details')[1]);
  rerender(<TaskSessionActivity view={{ ...saved, attempts: [attempt, { ...attempt, id: 'retry', created_at: '2026-10-06T12:01:05Z', status: 'queued',
    finished_at: null, started_at: null, retry_of_attempt_id: 'turn', not_before: '2026-10-06T12:02:00Z' }],
    tool_calls: [{ ...tool, status: 'done', result: { message_id: 'message' }, finished_at: '2026-10-06T12:00:25Z' }] }} profiles={[]} />);
  expect(screen.getAllByText('Role tool')).toHaveLength(1);
  expect(screen.getByText(/Retry of turn 1/)).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: /Turn 2 · Agent queued/ })).toBeInTheDocument();
  expect(document.querySelector('pre')?.textContent).toContain('"status": "done"');
  expect(screen.getByText(/Message 1 · Turn 1/)).toBeInTheDocument();
});
