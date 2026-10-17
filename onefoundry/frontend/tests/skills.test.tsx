import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { SkillLibrary } from '../src/SkillLibrary';
import { ProfileSkills } from '../src/ProfileSkills';

const path = '/api/projects/project-1/skills';
const boundPath = '/api/projects/project-1/profiles/profile-1/skills';
const stamp = '2026-09-26T00:00:00Z';
const hash = 'a'.repeat(64);

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('creates a procedure, appends immutable versions, and leaves a profile pinned to v1', async () => {
  const skills: Record<string, unknown>[] = [];
  const versions: Record<string, unknown>[] = [];
  let binding: Record<string, unknown>[] = [];
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === path && init?.method === 'POST') {
      const fields = JSON.parse(init.body as string) as { name: string; description: string };
      const created = { id: 'skill-1', project_id: 'project-1', ...fields, latest_version: null, created_at: stamp };
      skills.push(created); return json(created, 201);
    }
    if (url === path) return json(skills);
    if (url === `${path}/skill-1/versions` && init?.method === 'POST') {
      const fields = JSON.parse(init.body as string) as { content: string };
      const created = { id: `version-${versions.length + 1}`, project_id: 'project-1', skill_id: 'skill-1',
        version: versions.length + 1, content: fields.content, sha256: hash, actor_id: 'operator', created_at: stamp };
      versions.push(created); skills[0].latest_version = created.version;
      return json(created, 201);
    }
    if (url === `${path}/skill-1/versions`) return json(versions);
    if (url === boundPath) return json(binding);
    if (url === `${boundPath}/skill-1` && init?.method === 'PUT') {
      const fields = JSON.parse(init.body as string) as { version: number };
      const selected = versions.find((item) => item.version === fields.version);
      binding = [{ ...selected, id: 'binding-1', profile_id: 'profile-1', skill_name: 'Release review' }];
      return json(binding[0]);
    }
    throw new Error(`Unexpected ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const library = render(<SkillLibrary projectId="project-1" />);
  expect(await screen.findByText('No procedures yet. Create one to start a versioned SKILL.md.')).toBeInTheDocument();
  await user.type(screen.getByPlaceholderText('e.g. Release review'), 'Release review');
  await user.click(screen.getByRole('button', { name: 'Create procedure' }));
  expect(await screen.findByText('Skill created. Add its first SKILL.md version below.')).toBeInTheDocument();
  await user.type(screen.getByPlaceholderText(/# Procedure/), '# Release review\nCheck the tests.');
  await user.click(screen.getByRole('button', { name: 'Add immutable version' }));
  expect(await screen.findByText('Version 1 saved. Existing profile bindings keep their exact versions.')).toBeInTheDocument();
  library.unmount();

  const profile = render(<ProfileSkills projectId="project-1" profileId="profile-1" onChanged={vi.fn()} />);
  expect(await screen.findByText(/Live text work can automatically match project skills/)).toBeInTheDocument();
  await screen.findByRole('option', { name: 'Release review' });
  await user.selectOptions(screen.getByLabelText('Library procedure'), 'skill-1');
  await screen.findByRole('option', { name: 'Version 1' });
  await user.selectOptions(screen.getByLabelText('Exact version'), '1');
  await user.click(screen.getByRole('button', { name: 'Pin version' }));
  expect(await screen.findByText('Release review · v1')).toBeInTheDocument();
  profile.unmount();

  render(<SkillLibrary projectId="project-1" />);
  await screen.findByText('Version 1');
  await user.type(screen.getByPlaceholderText(/# Procedure/), '# Release review\nCheck deployment.');
  await user.click(screen.getByRole('button', { name: 'Add immutable version' }));
  expect(await screen.findByText('Version 2 saved. Existing profile bindings keep their exact versions.')).toBeInTheDocument();
  expect(binding[0].version).toBe(1);
  const writes = fetcher.mock.calls.filter(([url, init]) => url === `${boundPath}/skill-1` && init?.method === 'PUT');
  expect(writes).toHaveLength(1);
});

it('keeps the library form open on a name conflict', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === path && init?.method === 'POST') return json({ detail: 'Skill name already exists' }, 409);
    if (url === path) return json([]);
    throw new Error(`Unexpected ${url}`);
  }));
  const user = userEvent.setup();
  render(<SkillLibrary projectId="project-1" />);
  await screen.findByText('No procedures yet. Create one to start a versioned SKILL.md.');
  await user.type(screen.getByPlaceholderText('e.g. Release review'), 'Release review');
  await user.click(screen.getByRole('button', { name: 'Create procedure' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Skill name already exists');
  expect(screen.getByPlaceholderText('e.g. Release review')).toHaveValue('Release review');
});

it('reports an uncertain binding when the readback does not match the selected version', async () => {
  const skill = { id: 'skill-1', project_id: 'project-1', name: 'Release review', description: '', latest_version: 1, created_at: stamp };
  const version = { id: 'version-1', project_id: 'project-1', skill_id: 'skill-1', version: 1,
    content: '# Review', sha256: hash, actor_id: 'operator', created_at: stamp };
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === path) return json([skill]);
    if (url === `${path}/skill-1/versions`) return json([version]);
    if (url === boundPath) return json([]);
    if (url === `${boundPath}/skill-1` && init?.method === 'PUT') return json({ ...version, skill_name: skill.name });
    throw new Error(`Unexpected ${url}`);
  }));
  const user = userEvent.setup();
  render(<ProfileSkills projectId="project-1" profileId="profile-1" onChanged={vi.fn()} />);
  await screen.findByRole('option', { name: 'Release review' });
  await user.selectOptions(screen.getByLabelText('Library procedure'), 'skill-1');
  await screen.findByRole('option', { name: 'Version 1' });
  await user.selectOptions(screen.getByLabelText('Exact version'), '1');
  await user.click(screen.getByRole('button', { name: 'Pin version' }));
  await waitFor(() => expect(within(screen.getByRole('region', { name: 'Pinned procedures' })).getByRole('alert'))
    .toHaveTextContent('readback did not match'));
});

it('sends the expected binding ID and refreshes stale state after a 409', async () => {
  const skill = { id: 'skill-1', project_id: 'project-1', name: 'Release review', description: '', latest_version: 2, created_at: stamp };
  const versions = [1, 2].map((version) => ({ id: `version-${version}`, project_id: 'project-1', skill_id: 'skill-1',
    version, content: `# Review ${version}`, sha256: hash, actor_id: 'operator', created_at: stamp }));
  let boundVersion = 1;
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === path) return json([skill]);
    if (url === `${path}/skill-1/versions`) return json(versions);
    if (url === boundPath) return json([{ ...versions[boundVersion - 1], id: 'binding-1', profile_id: 'profile-1', skill_name: skill.name }]);
    if (url === `${boundPath}/skill-1` && init?.method === 'PUT') {
      boundVersion = 2;
      return json({ detail: 'Skill binding changed; refresh before retrying' }, 409);
    }
    throw new Error(`Unexpected ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ProfileSkills projectId="project-1" profileId="profile-1" onChanged={vi.fn()} />);
  await screen.findByText('Release review · v1');
  await user.selectOptions(screen.getByLabelText('Library procedure'), 'skill-1');
  await screen.findByRole('option', { name: 'Version 2' });
  await user.selectOptions(screen.getByLabelText('Exact version'), '2');
  await user.click(screen.getByRole('button', { name: 'Pin version' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Skill binding changed; refresh before retrying');
  expect(await screen.findByText('Release review · v2')).toBeInTheDocument();
  const calls = fetcher.mock.calls.filter(([url, init]) => url === `${boundPath}/skill-1` && init?.method === 'PUT');
  expect(calls).toHaveLength(1);
  expect(JSON.parse(calls[0][1]?.body as string)).toEqual({ version: 2, expected_binding_id: 'binding-1' });
});
