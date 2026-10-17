import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ProjectPlanPanel } from '../src/ProjectPlanPanel';

afterEach(() => vi.unstubAllGlobals());

it('saves and reads back the separate versioned project plan', async () => {
  let saved = { project_id: 'project-1', body: 'Research once.', version: 1, actor_id: 'operator',
    updated_at: '2026-10-03T12:00:00Z' };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    expect(path).toBe('/api/projects/project-1/plan');
    if (init?.method === 'PATCH') {
      expect(JSON.parse(String(init.body))).toEqual({
        body: 'Research and have QA review.', expected_version: 1,
      });
      saved = { ...saved, body: 'Research and have QA review.', version: 2 };
    }
    return new Response(JSON.stringify(saved), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ProjectPlanPanel projectId="project-1" />);
  const editor = await screen.findByLabelText('Saved project-wide plan · version 1');
  await user.clear(editor);
  await user.type(editor, 'Research and have QA review.');
  await user.click(screen.getByRole('button', { name: 'Save project plan' }));
  expect(await screen.findByText('Project plan saved (version 2).')).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(3);
});

it('keeps the operator draft when the saved plan version changes', async () => {
  let version = 1;
  vi.stubGlobal('fetch', vi.fn(async (_path: string, init?: RequestInit) => {
    if (init?.method === 'PATCH') {
      version = 2;
      return new Response(JSON.stringify({ detail: 'project plan changed; reload before saving' }), {
        status: 409, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ project_id: 'project-1',
      body: version === 1 ? 'Original plan.' : 'Another saved plan.', version,
      actor_id: 'operator', updated_at: '2026-10-03T12:00:00Z' }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  }));
  const user = userEvent.setup();
  render(<ProjectPlanPanel projectId="project-1" />);
  const editor = await screen.findByLabelText('Saved project-wide plan · version 1');
  await user.clear(editor);
  await user.type(editor, 'My proposed plan.');
  await user.click(screen.getByRole('button', { name: 'Save project plan' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('project plan changed');
  expect(screen.getByLabelText('Saved project-wide plan · version 2')).toHaveValue('My proposed plan.');
  await user.click(screen.getByText('Current saved plan · version 2'));
  expect(screen.getByText('Another saved plan.')).toBeVisible();
});
