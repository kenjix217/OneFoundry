import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { App } from '../src/App';
import type { Profile } from '../src/api';
import { OrganizationChart } from '../src/OrganizationChart';
import { ProfileEditor } from '../src/ProfileEditor';

function profile(id: string, name: string, parent: string | null, kind: Profile['kind'] = 'builtin'): Profile {
  return { id, project_id: 'project-1', name, kind, reports_to_profile_id: parent, model: null,
    instructions: '', memory_scope: 'project', allowed_tools: [], autonomy_level: 'manual',
    configured: false, permissions_active: false };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => { vi.unstubAllGlobals(); window.localStorage.clear(); });

it('shows operator, Chief of Staff, and specialist reporting levels with an accessible edit action', async () => {
  const onEdit = vi.fn();
  const chief = profile('chief', 'Chief of Staff', null);
  const engineer = profile('engineer', 'Engineer', chief.id);
  const qa = profile('qa', 'QA', chief.id);
  render(<OrganizationChart profiles={[qa, engineer, chief]} onEdit={onEdit} />);

  const chart = screen.getByRole('group', { name: 'Organization chart' });
  expect(within(chart).getByText('You')).toBeInTheDocument();
  const chiefBranch = within(chart).getByText('Chief of Staff').closest('li');
  expect(chiefBranch).not.toBeNull();
  expect(within(chiefBranch!).getByText('Engineer')).toBeInTheDocument();
  expect(within(chiefBranch!).getByText('QA')).toBeInTheDocument();
  expect(screen.getByText(/do not grant tools, approvals, or autonomous delegation/i)).toBeInTheDocument();
  await userEvent.setup().click(within(chart).getByRole('button', { name: 'Edit Engineer reporting and profile settings' }));
  expect(onEdit).toHaveBeenCalledWith(engineer);
});

it('places a reassigned custom profile under a lower-case Chief of Staff root', () => {
  const chief = profile('chief', 'chief of staff', null, 'custom');
  const researcher = profile('research', 'Researcher', chief.id);
  const strategist = profile('strategy', 'Strategist', researcher.id, 'custom');
  render(<OrganizationChart profiles={[strategist, researcher, chief]} onEdit={() => {}} />);
  const chiefNode = screen.getByText('chief of staff').closest('.org-node') as HTMLElement;
  expect(chiefNode).toHaveClass('org-chief');
  expect(within(chiefNode!).getByText('Coordinator')).toBeInTheDocument();
  const researcherBranch = within(screen.getByRole('group', { name: 'Organization chart' })).getByText('Researcher').closest('li');
  expect(within(researcherBranch!).getByText('Strategist')).toBeInTheDocument();
});

it('keeps a confusable-name child under the saved root and allows its manager to be edited', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/model/catalog') || path.endsWith('/model/accounts')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  const chief = profile('chief', 'Chief of Staff', null);
  const confusable = profile('lookalike', 'chief of ſtaff', chief.id, 'custom');
  render(<OrganizationChart profiles={[confusable, chief]} onEdit={() => {}} />);
  const chart = screen.getByRole('group', { name: 'Organization chart' });
  const childNode = within(chart).getByText('chief of ſtaff').closest('.org-node') as HTMLElement;
  expect(childNode).not.toHaveClass('org-chief');
  expect(within(childNode).getByText('Custom profile')).toBeInTheDocument();
  expect(within(within(chart).getByText('Chief of Staff').closest('li')!).getByText('chief of ſtaff')).toBeInTheDocument();

  const save = vi.fn(async () => {});
  render(<ProfileEditor projectId="project-1" profile={confusable} profiles={[chief, confusable]}
    saving={false} error={null} onClose={() => {}} onSave={save} />);
  expect(screen.getByRole('combobox', { name: 'Reports to' })).toHaveValue(chief.id);
  await userEvent.setup().click(screen.getByRole('button', { name: 'Save profile' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ reports_to_profile_id: chief.id }));
});

it('treats a new profile as a child even when its typed name matches Chief of Staff', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/model/catalog') || path.endsWith('/model/accounts')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  const chief = profile('chief', 'Chief of Staff', null);
  const user = userEvent.setup();
  render(<ProfileEditor projectId="project-1" profile={null} profiles={[chief]}
    saving={false} error={null} onClose={() => {}} onSave={async () => {}} />);
  await user.type(screen.getByPlaceholderText('e.g. Product strategist'), 'Chief of Staff');
  expect(screen.getByRole('combobox', { name: 'Reports to' })).toHaveValue(chief.id);
});

it('keeps a lower-case custom Chief at the operator root and lets a built-in specialist change manager', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/model/catalog') || path.endsWith('/model/accounts')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  const chief = profile('chief', 'chief of staff', null, 'custom');
  const engineer = profile('engineer', 'Engineer', chief.id);
  const qa = profile('qa', 'QA', chief.id);
  const saveChief = vi.fn(async () => {});
  const user = userEvent.setup();
  const chiefView = render(<ProfileEditor projectId="project-1" profile={chief} profiles={[chief, engineer, qa]}
    saving={false} error={null} onClose={() => {}} onSave={saveChief} />);
  expect(screen.getByText('You · company operator')).toBeInTheDocument();
  expect(screen.queryByRole('combobox', { name: 'Reports to' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Save profile' }));
  expect(saveChief).toHaveBeenCalledWith(expect.not.objectContaining({ reports_to_profile_id: expect.anything() }));
  chiefView.unmount();

  const saveEngineer = vi.fn(async () => {});
  render(<ProfileEditor projectId="project-1" profile={engineer} profiles={[chief, engineer, qa]}
    saving={false} error={null} onClose={() => {}} onSave={saveEngineer} />);
  expect(screen.getByRole('combobox', { name: 'Reports to' })).toHaveValue(chief.id);
  await user.selectOptions(screen.getByRole('combobox', { name: 'Reports to' }), qa.id);
  await user.click(screen.getByRole('button', { name: 'Save profile' }));
  expect(saveEngineer).toHaveBeenCalledWith(expect.objectContaining({ reports_to_profile_id: qa.id }));
});

it('keeps the editor open on a save error and confirms a custom reassignment by readback', async () => {
  const chief = profile('chief', 'Chief of Staff', null);
  const researcher = profile('research', 'Researcher', chief.id);
  let strategist = profile('strategy', 'Strategist', chief.id, 'custom');
  let rejectNext = true;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/projects') return json([{ id: 'project-1', name: 'Studio', goal: '', desired_outcome: '', status: 'active', version: 1, trash_from_status: null }]);
    if (path === '/api/projects/project-1') return json({ id: 'project-1', name: 'Studio', goal: '', desired_outcome: '', status: 'active', version: 1, trash_from_status: null });
    if (path === '/api/projects/project-1/profiles/strategy' && init?.method === 'PATCH') {
      if (rejectNext) { rejectNext = false; return json({ detail: 'Reporting cycle rejected' }, 409); }
      strategist = { ...strategist, ...(JSON.parse(String(init.body)) as Partial<Profile>) };
      return json(strategist);
    }
    if (path === '/api/projects/project-1/profiles') return json([chief, researcher, strategist]);
    if (path.endsWith('/model/catalog') || path.endsWith('/model/accounts')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole('combobox', { name: 'Conversation agent' });
  await user.click(screen.getByRole('button', { name: 'Settings' }));
  await user.selectOptions(screen.getByRole('combobox', { name: 'Settings page' }), 'team');
  await user.click(screen.getByRole('button', { name: 'Team structure' }));
  await user.click(screen.getByRole('button', { name: 'Edit Strategist reporting and profile settings' }));
  expect(screen.queryByRole('option', { name: /company operator/i })).not.toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('Reports to'), researcher.id);
  await user.click(screen.getByRole('button', { name: 'Save profile' }));
  expect(await screen.findByText('Reporting cycle rejected')).toBeInTheDocument();
  expect(screen.getByLabelText('Reports to')).toHaveValue(researcher.id);
  await user.click(screen.getByRole('button', { name: 'Save profile' }));
  expect(await screen.findByText(/Profile saved/)).toBeInTheDocument();
  expect(strategist.reports_to_profile_id).toBe(researcher.id);
  const researcherBranch = within(screen.getByRole('group', { name: 'Organization chart' })).getByText('Researcher').closest('li');
  expect(within(researcherBranch!).getByText('Strategist')).toBeInTheDocument();
  const patches = fetcher.mock.calls.filter(([path, init]) => path === '/api/projects/project-1/profiles/strategy' && init?.method === 'PATCH');
  expect(JSON.parse(String(patches[1][1]?.body)).reports_to_profile_id).toBe(researcher.id);
});
