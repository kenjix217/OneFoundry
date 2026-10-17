import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import { OneFoundryCapabilityStatus } from '../src/OneFoundryCapabilityStatus';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('separates configured IDs from an unknown run with observed tool admission', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json({
    project_id: 'project-1', profile_id: 'profile-1',
    configured_model_id: 'gpt-test', configured_tool_ids: ['current_work_order'],
    last_run: { id: 'run-1', status: 'unknown', tools_active: true },
  })));
  render(<OneFoundryCapabilityStatus projectId="project-1" profileId="profile-1" />);
  expect(await screen.findByText(/Last saved run: unknown/)).toBeInTheDocument();
  expect(screen.getByText(/Live-text tool route was active for that run/)).toBeInTheDocument();
  expect(screen.getByText(/Every action is checked against the current permissions/)).toBeInTheDocument();
  expect(screen.getByText(/gpt-test/)).toBeInTheDocument();
});

it('describes null admission as unrecorded, not denied', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json({
    project_id: 'project-1', profile_id: 'profile-1',
    configured_model_id: null, configured_tool_ids: [],
    last_run: { id: 'run-2', status: 'done', tools_active: null },
  })));
  render(<OneFoundryCapabilityStatus projectId="project-1" profileId="profile-1" />);
  expect(await screen.findByText(/No live-text tool admission was recorded/)).toBeInTheDocument();
});

it('shows no-run and unavailable readback without claiming admission', async () => {
  const fetcher = vi.fn(async () => json({
    project_id: 'project-1', profile_id: 'profile-1',
    configured_model_id: null, configured_tool_ids: [], last_run: null,
  }));
  vi.stubGlobal('fetch', fetcher);
  const view = render(<OneFoundryCapabilityStatus projectId="project-1" profileId="profile-1" />);
  expect(await screen.findByText(/No saved run for this profile/)).toBeInTheDocument();
  view.unmount();
  vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'database unavailable' }, 503)));
  render(<OneFoundryCapabilityStatus projectId="project-1" profileId="profile-1" />);
  expect(await screen.findByText(/Capability readback unavailable/)).toBeInTheDocument();
});
