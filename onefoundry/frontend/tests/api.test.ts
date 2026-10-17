import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, createProfile, fillStarterProfiles, listProfiles, listProjects, updateProfile, updateProjectBrief } from '../src/api';

const profile = {
  id: 'profile-1', project_id: 'project-1', name: 'Engineer', kind: 'builtin', reports_to_profile_id: null, model: null, model_account_id: null,
  reasoning_effort: null,
  instructions: '', memory_scope: 'project', allowed_tools: [], autonomy_level: 'manual',
  configured: false, permissions_active: false,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

describe('OneFoundry API client', () => {
  it('reads bare project/profile lists from same-origin paths', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(json([{ id: 'project-1', name: 'Studio', goal: '', desired_outcome: '' }]))
      .mockResolvedValueOnce(json([profile]));
    vi.stubGlobal('fetch', fetcher);

    expect(await listProjects()).toEqual([{ id: 'project-1', name: 'Studio', goal: '', desired_outcome: '', status: 'active', version: 1, trash_from_status: null }]);
    expect(await listProfiles('project-1')).toEqual([profile]);
    expect(fetcher.mock.calls[0][0]).toBe('/api/projects');
    expect(fetcher.mock.calls[1][0]).toBe('/api/projects/project-1/profiles');
    expect(fetcher.mock.calls[0][1].credentials).toBe('same-origin');
  });

  it('sends profile metadata without claiming permission or execution', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(json({ ...profile, id: 'custom-1', kind: 'custom', name: 'Planner' }))
      .mockResolvedValueOnce(json({ ...profile, model: 'model-a', configured: true }));
    vi.stubGlobal('fetch', fetcher);
    const fields = { model: 'model-a', instructions: 'Plan carefully', memory_scope: 'company' as const,
      allowed_tools: ['search.read'], autonomy_level: 'supervised' as const };

    await createProfile('project-1', { name: 'Planner', ...fields });
    await updateProfile('project-1', 'profile-1', fields);
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ name: 'Planner', ...fields });
    expect(fetcher.mock.calls[1][1].method).toBe('PATCH');
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual(fields);
    expect(fetcher.mock.calls[1][1].body).not.toContain('permissions_active');
  });

  it('shows FastAPI validation detail instead of a generic status', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: [{ msg: 'Invalid model name' }] }, 422)));
    await expect(listProjects()).rejects.toMatchObject({
      name: 'ApiError', status: 422, message: 'Invalid model name',
    });
  });

  it('rejects malformed profile responses instead of presenting invented data', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json([{ ...profile, permissions_active: undefined }])));
    await expect(listProfiles('project-1')).rejects.toBeInstanceOf(ApiError);
  });

  it('rejects a missing or malformed reporting line instead of inventing a root', async () => {
    for (const invalid of [undefined, '', 42]) {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json([{ ...profile, reports_to_profile_id: invalid }])));
      await expect(listProfiles('project-1')).rejects.toMatchObject({
        name: 'ApiError', message: 'Invalid reporting line.',
      });
    }
  });

  it('sends versioned project brief and parses the raw starter profile list', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(json({ id: 'project-1', name: 'Studio', goal: 'Find users', desired_outcome: 'A brief', version: 2 }))
      .mockResolvedValueOnce(json([profile]));
    vi.stubGlobal('fetch', fetcher);
    expect((await updateProjectBrief('project-1', { goal: 'Find users', desired_outcome: 'A brief', expected_version: 1 })).version).toBe(2);
    expect(await fillStarterProfiles('project-1')).toEqual([profile]);
    expect(fetcher.mock.calls[0][0]).toBe('/api/projects/project-1/brief');
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ goal: 'Find users', desired_outcome: 'A brief', expected_version: 1 });
    expect(fetcher.mock.calls[1][0]).toBe('/api/projects/project-1/profiles/starter');
    expect(fetcher.mock.calls[1][1].method).toBe('POST');
  });

  it('rejects a project response missing either brief field', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json([{ id: 'project-1', name: 'Studio', goal: '' }])));
    await expect(listProjects()).rejects.toBeInstanceOf(ApiError);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json([{ id: 'project-1', name: 'Studio', desired_outcome: '' }])));
    await expect(listProjects()).rejects.toBeInstanceOf(ApiError);
  });
});
