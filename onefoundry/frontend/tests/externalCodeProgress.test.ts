import { afterEach, expect, it, vi } from 'vitest';

import { request } from '../src/api';
import { getExternalCodeProgress } from '../src/externalCodeApi';
import { AGENT_UPDATE_TEXT, parseSavedActionTrace, STAGE_BASELINE_FAILED,
  STAGE_CLI_FINISHED, STAGE_TEST_PASSED } from '../src/workOrderTrace';

vi.mock('../src/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../src/api')>(),
  request: vi.fn(),
}));

afterEach(() => vi.clearAllMocks());

const record = { project_id: 'project-1', run_id: 'run-1', attempt_id: 'attempt-1',
  coverage: 'partial', latest_sequence: 1, terminal: false,
  items: [{ sequence: 1, source: 'cli_output', kind: 'agent_update',
    text: AGENT_UPDATE_TEXT, tool: null, command: null }] };

it('reads only ordered, scoped public progress', async () => {
  vi.mocked(request).mockResolvedValue(record);
  const progress = await getExternalCodeProgress('project-1', 'run-1', 'attempt-1');
  expect(progress.items[0].text).toBe(AGENT_UPDATE_TEXT);
  expect(vi.mocked(request).mock.calls[0][0]).toContain('/attempts/attempt-1/progress?after=0');
});

it('rejects a wrong attempt, sequence gap, or unsafe command', async () => {
  vi.mocked(request).mockResolvedValueOnce({ ...record, attempt_id: 'attempt-2' });
  await expect(getExternalCodeProgress('project-1', 'run-1', 'attempt-1')).rejects.toThrow();
  vi.mocked(request).mockResolvedValueOnce({ ...record, latest_sequence: 2 });
  await expect(getExternalCodeProgress('project-1', 'run-1', 'attempt-1')).rejects.toThrow();
  vi.mocked(request).mockResolvedValueOnce({ ...record,
    items: [{ sequence: 1, source: 'cli_output', kind: 'tool_activity',
      text: 'Ran a shell command.', tool: 'shell', command: 'cat secret.env' }] });
  await expect(getExternalCodeProgress('project-1', 'run-1', 'attempt-1')).rejects.toThrow();
});

it.each([STAGE_BASELINE_FAILED, STAGE_CLI_FINISHED, STAGE_TEST_PASSED])(
  'accepts runner stage %s in live API and saved trace with runner provenance', async (stage) => {
    const item = { source: 'runner_observed', kind: 'runner_update', text: stage,
      tool: null, command: null };
    vi.mocked(request).mockResolvedValue({ ...record, items: [{ sequence: 1, ...item }] });
    const live = await getExternalCodeProgress('project-1', 'run-1', 'attempt-1');
    expect(live.items[0]).toMatchObject(item);
    const saved = parseSavedActionTrace(JSON.stringify({ schema_version: 1, coverage: 'partial',
      provider_tool_calls_captured: false, omitted_actions: 0, actions: [], progress: [item] }));
    expect(saved.progress[0]).toMatchObject(item);
    vi.mocked(request).mockResolvedValue({ ...record, items: [{ sequence: 1, ...item,
      source: 'cli_output' }] });
    await expect(getExternalCodeProgress('project-1', 'run-1', 'attempt-1')).rejects.toThrow();
  });
