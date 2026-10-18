import { afterEach, expect, it, vi } from 'vitest';
import { getChiefProgress } from '../src/chiefProgressApi';

afterEach(() => vi.unstubAllGlobals());

it('reads the exact project and root progress without treating pause as the blocker', async () => {
  const saved = { counts: { active_attempts: 0, queued_attempts: 0, unfinished_assignments: 0,
    blocked_assignments: 0, ready_artifacts: 2 }, paused: true,
    blocker: { kind: 'unknown_attempt', detail: 'An attempt has an unverified outcome.', session_id: 'child-1', attempt_id: 'attempt-1' },
    next_action: { kind: 'open_agent', label: 'Review uncertain attempt', session_id: 'child-1' } };
  const fetch = vi.fn(async (path: string) => {
    expect(path).toBe('/api/projects/project-1/chief-workspace/order-1/progress');
    return new Response(JSON.stringify(saved));
  });
  vi.stubGlobal('fetch', fetch);
  expect(await getChiefProgress('project-1', 'order-1')).toEqual(saved);
  expect(fetch.mock.calls[0]?.[0]).toBe('/api/projects/project-1/chief-workspace/order-1/progress');
});

it('rejects incomplete counts and unsafe targetless child actions', async () => {
  const base = { counts: { active_attempts: 0, queued_attempts: 0, unfinished_assignments: 1,
    blocked_assignments: 0, ready_artifacts: 0 }, paused: false, blocker: null,
    next_action: { kind: 'open_agent', label: 'Review affected agent' } };
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(base))));
  await expect(getChiefProgress('project-1', 'order-1')).rejects.toThrow('action is incomplete');
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ...base,
    counts: { ...base.counts, active_attempts: -1 }, next_action: null }))));
  await expect(getChiefProgress('project-1', 'order-1')).rejects.toThrow('counts are incomplete');
});
