import { afterEach, describe, expect, it } from 'vitest';

import { clearLocalKey, clearSessionKey, readLocalKey, readSessionKey } from '../src/brandStorage';

afterEach(() => { localStorage.clear(); sessionStorage.clear(); });

describe('previous product browser state', () => {
  it('carries an unresolved send key forward and clears both copies after readback', () => {
    sessionStorage.setItem('agentpod:task-chat-pending:project:order', 'saved-send');
    expect(readSessionKey('onefoundry:task-chat-pending:project:order')).toBe('saved-send');
    expect(sessionStorage.getItem('onefoundry:task-chat-pending:project:order')).toBe('saved-send');
    clearSessionKey('onefoundry:task-chat-pending:project:order');
    expect(sessionStorage.length).toBe(0);
  });

  it('keeps the new value when both versions exist', () => {
    sessionStorage.setItem('agentpod.fixture-fetch-v2.p.r.s', 'old');
    sessionStorage.setItem('onefoundry.fixture-fetch-v2.p.r.s', 'new');
    expect(readSessionKey('onefoundry.fixture-fetch-v2.p.r.s')).toBe('new');
  });

  it('carries project selection forward and clears both copies', () => {
    localStorage.setItem('agentpod:last-project-id', 'project');
    expect(readLocalKey('onefoundry:last-project-id')).toBe('project');
    clearLocalKey('onefoundry:last-project-id');
    expect(localStorage.length).toBe(0);
  });
});
