import { expect, it } from 'vitest';
import { readWorkspaceRoute, workspaceHash } from '../src/workNavigation';

it('keeps the selected task and run in a shareable workspace URL', () => {
  const task = { projectId: 'project one', view: 'chat' as const, orderId: 'order 2', runId: null };
  expect(readWorkspaceRoute(workspaceHash(task))).toEqual(task);
  const activity = { ...task, view: 'activity' as const, runId: 'run 3' };
  expect(readWorkspaceRoute(workspaceHash(activity))).toEqual(activity);
  const management = { ...task, view: 'work' as const };
  expect(readWorkspaceRoute(workspaceHash(management))).toEqual(management);
  expect(readWorkspaceRoute('#project/%broken/task')).toBeNull();
  expect(readWorkspaceRoute('#project/p/other')).toBeNull();
});
