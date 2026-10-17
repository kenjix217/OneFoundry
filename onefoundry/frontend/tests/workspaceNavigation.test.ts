import { expect, it } from 'vitest';
import { readWorkspaceRoute, workspaceHash } from '../src/workNavigation';
import { sectionForView, sections } from '../src/workspaceNavigation';
import { readWorkspacePreferences, saveWorkspacePreferences } from '../src/workspacePreferences';

it('groups every operator destination into Chief, Integrations and Settings and preserves deep links', () => {
  expect(sections.map((item) => item.label)).toEqual(['Chief', 'Settings', 'Integrations']);
  for (const section of sections) for (const item of section.views) {
    const route = { projectId: 'a project', view: item.view, orderId: null, runId: null };
    expect(readWorkspaceRoute(workspaceHash(route))).toEqual(route);
    expect(sectionForView(item.view)).toBe(section);
  }
  expect(readWorkspaceRoute('#project/a/task?order=b')?.view).toBe('chat');
  expect(readWorkspaceRoute('#project/a/activity?order=b&run=c')?.runId).toBe('c');
  expect(readWorkspaceRoute('#project/a/tools')?.view).toBe('integrations');
  expect(readWorkspaceRoute('#project/a/not-a-page')).toBeNull();
  expect(readWorkspaceRoute('#project/%zz/home')).toBeNull();
});

it('retains bounded display preferences and recovers from malformed or unavailable storage', () => {
  const store = new Map<string, string>();
  const storage = { getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, value: string) => { store.set(key, value); } };
  const defaults = readWorkspacePreferences(storage);
  expect(defaults.density).toBe('comfortable');
  saveWorkspacePreferences({ ...defaults, density: 'compact', hiddenHomePanels: ['team'], homeOrder: ['team', 'attention', 'work'] }, storage);
  expect(readWorkspacePreferences(storage).homeOrder).toEqual(['team', 'attention', 'work']);
  for (const key of store.keys()) store.set(key, '{broken');
  expect(readWorkspacePreferences(storage)).toEqual(defaults);
  const denied = { getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('denied'); } };
  expect(readWorkspacePreferences(denied)).toEqual(defaults);
  expect(saveWorkspacePreferences(defaults, denied)).toBe(false);
});
