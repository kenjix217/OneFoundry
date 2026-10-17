import { afterEach, expect, it, vi } from 'vitest';
import { getCoordination } from '../src/coordinationApi';

const status = { mode: 'off', paused: false, agent_rules_enabled: false, version: 1,
  max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', signal_types: {},
  profile_tool_ceiling: [], registered_tools: ['approved_memory_v1'] };

afterEach(() => vi.unstubAllGlobals());

it('preserves optional registered tool labels and defaults older responses to an empty map', async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...status, registered_tool_labels: { approved_memory_v1: 'Approved memory' } }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(status), { status: 200 }));
  vi.stubGlobal('fetch', fetcher);
  expect((await getCoordination('project-1')).registered_tool_labels).toEqual({ approved_memory_v1: 'Approved memory' });
  expect((await getCoordination('project-1')).registered_tool_labels).toEqual({});
});

it.each([null, [], { approved_memory_v1: 42 }])('rejects invalid registered tool label metadata: %j', async (labels) => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ...status, registered_tool_labels: labels }), { status: 200 })));
  await expect(getCoordination('project-1')).rejects.toThrow('The server returned an unexpected automation response.');
});
