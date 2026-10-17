import { expect, it } from 'vitest';

import { AGENT_UPDATE_TEXT, parseSavedActionTrace } from '../src/workOrderTrace';

const trace = { schema_version: 1, coverage: 'partial', provider_tool_calls_captured: false,
  omitted_actions: 0, actions: [{ source: 'runner_observed', kind: 'command',
    command: ['python', '-m', 'unittest'], cwd: '.', started_at: '2026-09-29T10:00:00Z',
    finished_at: '2026-09-29T10:00:01Z', outcome: 'finished', exit_code: 1,
    stdout: '1 failed', stderr: '', output_truncated: false }] };

it('keeps a failed runner command and partial coverage visible', () => {
  const parsed = parseSavedActionTrace(JSON.stringify(trace));
  expect(parsed.actions[0].command).toEqual(['python', '-m', 'unittest']);
  expect(parsed.actions[0].exit_code).toBe(1);
  expect(parsed.coverage).toBe('partial');
});

it('rejects a claimed complete provider trace', () => {
  expect(() => parseSavedActionTrace(JSON.stringify({ ...trace, coverage: 'complete' }))).toThrow();
  expect(() => parseSavedActionTrace(JSON.stringify({ ...trace, provider_tool_calls_captured: true }))).toThrow();
});

it('accepts bounded safe CLI updates and rejects raw command payloads', () => {
  const progress = [{ source: 'cli_output', kind: 'agent_update',
    text: AGENT_UPDATE_TEXT, tool: null, command: null },
  { source: 'cli_output', kind: 'tool_activity', text: 'Ran a shell command.',
    tool: 'shell', command: 'git diff --check' }];
  const parsed = parseSavedActionTrace(JSON.stringify({ ...trace, progress, omitted_progress: 2 }));
  expect(parsed.progress).toHaveLength(2);
  expect(parsed.omitted_progress).toBe(2);
  expect(() => parseSavedActionTrace(JSON.stringify({ ...trace,
    progress: [{ ...progress[1], command: 'cat secret.env' }] }))).toThrow();
  expect(() => parseSavedActionTrace(JSON.stringify({ ...trace,
    progress: [{ ...progress[0], text: 'The customer SSN is 123-45-6789.' }] }))).toThrow();
});

it('reads older Claude edit events without claiming the edit succeeded', () => {
  const progress = [{ source: 'cli_output', kind: 'tool_activity',
    text: 'Edited a project file.', tool: 'Edit', command: null }];
  const parsed = parseSavedActionTrace(JSON.stringify({ ...trace, progress }));
  expect(parsed.progress[0].text).toBe('Requested a project file edit.');
});
