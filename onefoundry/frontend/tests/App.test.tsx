import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { App } from '../src/App';

const project = { id: 'project-1', name: 'Studio', goal: '', desired_outcome: '', created_at: '2026-09-25T00:00:00Z' };
const engineer = {
  id: 'engineer-1', project_id: project.id, name: 'Engineer', kind: 'builtin', reports_to_profile_id: null, model: null,
  instructions: '', memory_scope: 'project', allowed_tools: [], autonomy_level: 'manual',
  configured: false, permissions_active: false,
};
const chief = { ...engineer, id: 'chief-1', name: 'Chief of Staff', reports_to_profile_id: null };
const modelAccount = { id: 'account-1', project_id: project.id, provider: 'openai', label: 'Company API',
  auth_kind: 'api_key', endpoint: null, credential_present: true, catalog_valid: true, model_count: 1, last_refreshed_at: null };
const catalogModel = { id: 'catalog-1', account_id: modelAccount.id, provider: 'openai', model_id: 'model-a',
  display_name: 'Model A', category: 'text', available: true };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

async function openWorkspaceMenu(name = 'Studio') {
  const button = await screen.findByRole('button', { name: `${name} ⌄` });
  const projectsButton = screen.queryByRole('button', { name: 'Projects' });
  if (projectsButton) await userEvent.setup().click(projectsButton);
  return button;
}

afterEach(() => { vi.unstubAllGlobals(); window.localStorage.clear(); window.history.replaceState(null, '', '/'); });

it('opens Chief directly and keeps configuration behind Settings', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === '/api/projects') return json([project]);
    if (path === '/api/projects/project-1') return json(project);
    if (path.endsWith('/profiles')) return json([engineer]);
    return json([]);
  }));
  render(<App />);
  expect(await screen.findByRole('combobox', { name: 'Conversation agent' })).toHaveValue('');
  expect(screen.getByRole('textbox', { name: 'Message Chief' })).toBeInTheDocument();
  expect(screen.queryByRole('navigation', { name: 'Project sections' })).not.toBeInTheDocument();
  await openWorkspaceMenu();
  const navigation = screen.getByRole('navigation', { name: 'Project sections' });
  expect(within(navigation).getAllByRole('button').map((button) => button.textContent)).toEqual(['Chief', 'Settings', 'Integrations']);
});

it('opens Integrations from the rendered Chief sidebar in the default workspace', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === '/api/projects') return json([project]);
    if (path === '/api/projects/project-1') return json(project);
    if (path.endsWith('/profiles')) return json([engineer]);
    if (path.endsWith('/extensions/catalog')) return json({ tools: [], packages: [], skills: [] });
    if (path.endsWith('/extensions/worker')) return json({ available: false, reason: 'not_configured' });
    if (path.endsWith('/extensions/packages') || path.endsWith('/extensions/connections') || path.endsWith('/extensions/drafts') || path.endsWith('/extensions/invocations') || path.endsWith('/extensions/webhooks')) return json([]);
    return json([]);
  }));
  const user = userEvent.setup();
  render(<App />);
  const sidebar = await screen.findByRole('complementary', { name: 'Conversation sidebar' });
  expect(screen.getByRole('textbox', { name: 'Message Chief' })).toBeInTheDocument();
  await user.click(within(sidebar).getByRole('button', { name: 'Integrations' }));
  expect(await screen.findByRole('heading', { name: 'Integrations' })).toBeInTheDocument();
  expect(within(sidebar).getByRole('button', { name: 'Integrations' })).toHaveAttribute('aria-current', 'page');
  expect(screen.getByRole('navigation', { name: 'Integration Hub views' })).toBeInTheDocument();
});

it('uses the confirmed brief version for an immediate archive action', async () => {
  let saved = { ...project, status: 'active', version: 1, trash_from_status: null as string | null };
  let archivedVersion: number | null = null;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/chief-workspace')) return json([]);
    if (path.endsWith('/coordination')) return json({ mode: 'off', paused: false, max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', version: 0, signal_types: {}, profile_tool_ceiling: [], registered_tools: ['search_read', 'file_read'], agent_rules_enabled: false });
    if (path === '/api/projects') return json(saved.status === 'active' ? [saved] : []);
    if (path === '/api/projects/project-1/brief' && init?.method === 'PATCH') {
      const fields = JSON.parse(String(init.body));
      saved = { ...saved, goal: fields.goal, desired_outcome: fields.desired_outcome, version: 2 };
      return json(saved);
    }
    if (path === '/api/projects/project-1/archive' && init?.method === 'POST') {
      archivedVersion = JSON.parse(String(init.body)).expected_version;
      if (archivedVersion !== saved.version) return json({ detail: 'stale project version' }, 409);
      saved = { ...saved, status: 'archived', version: 3 }; return json(saved);
    }
    if (path === '/api/projects/project-1') return json(saved);
    if (path === '/api/projects/project-1/profiles') return json([engineer]);
    if (path.endsWith('/model/accounts') || path.endsWith('/model/providers') || path.endsWith('/model/catalog') || path.endsWith('/work-orders') || path.endsWith('/runs')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup(); render(<App />);
  await openWorkspaceMenu();
  await user.click(screen.getByRole('button', { name: /^Settings$/ }));
  await user.type(await screen.findByLabelText('Project goal'), 'Find users');
  await user.type(screen.getByLabelText('Desired outcome'), 'A cited brief');
  await user.click(screen.getByRole('button', { name: 'Save project brief' }));
  expect(await screen.findByText('Project brief saved and confirmed.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Projects' }));
  if (!screen.getByLabelText('Switch project').parentElement?.hasAttribute('open')) await user.click(screen.getByLabelText('Switch project'));
  await user.click(screen.getByRole('button', { name: 'Project menu for Studio' }));
  await user.click(screen.getByRole('button', { name: /^Archive$/ }));
  await user.click(screen.getByRole('button', { name: 'Archive project' }));
  await waitFor(() => expect(archivedVersion).toBe(2));
  expect(saved.status).toBe('archived');
});

it('restores the last selected active project after reload', async () => {
  const second = { ...project, id: 'project-2', name: 'Field' };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/chief-workspace')) return json([]);
    if (path.endsWith('/coordination')) return json({ mode: 'off', paused: false, max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', version: 0, signal_types: {}, profile_tool_ceiling: [], registered_tools: ['search_read', 'file_read'], agent_rules_enabled: false });
    if (path === '/api/projects') return json([project, second]);
    if (path === '/api/projects/project-1') return json(project);
    if (path === '/api/projects/project-2') return json(second);
    if (path.endsWith('/profiles')) return json([engineer]);
    if (path.endsWith('/model/accounts') || path.endsWith('/model/providers') || path.endsWith('/model/catalog') || path.endsWith('/work-orders') || path.endsWith('/runs')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup(); const first = render(<App />);
  await openWorkspaceMenu();
  await user.click(screen.getByLabelText('Switch project'));
  await user.click(screen.getByRole('button', { name: 'Field' }));
  expect(await openWorkspaceMenu('Field')).toBeInTheDocument();
  first.unmount();
  render(<App />);
  expect(await openWorkspaceMenu('Field')).toBeInTheDocument();
});

it('archives, restores, and moves a project to recoverable Trash from its menu', async () => {
  let saved = { ...project, status: 'active', version: 1, trash_from_status: null as string | null };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/chief-workspace')) return json([]);
    if (path.endsWith('/coordination')) return json({ mode: 'off', paused: false, max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', version: 0, signal_types: {}, profile_tool_ceiling: [], registered_tools: ['search_read', 'file_read'], agent_rules_enabled: false });
    if (path === '/api/projects') return json(saved.status === 'active' ? [saved] : []);
    if (path === '/api/projects?status=archived') return json(saved.status === 'archived' ? [saved] : []);
    if (path === '/api/projects?status=trashed') return json(saved.status === 'trashed' ? [saved] : []);
    if (path === '/api/projects/project-1' && !init?.method) return json(saved);
    if (path === '/api/projects/project-1/profiles') return json([engineer]);
    if (path === '/api/projects/project-1/model/accounts') return json([]);
    if (path === '/api/projects/project-1/model/catalog') return json([]);
    const match = path.match(/^\/api\/projects\/project-1\/(archive|unarchive|trash|restore)$/);
    if (match && init?.method === 'POST') {
      const input = JSON.parse(init.body as string) as { expected_version: number; confirm_name?: string };
      if (input.expected_version !== saved.version) return json({ detail: 'stale' }, 409);
      if (match[1] === 'trash' && input.confirm_name !== saved.name) return json({ detail: 'wrong name' }, 409);
      const prior = saved.status;
      saved = { ...saved, status: match[1] === 'archive' ? 'archived' : match[1] === 'unarchive' ? 'active' :
        match[1] === 'trash' ? 'trashed' : saved.trash_from_status ?? 'active',
      version: saved.version + 1, trash_from_status: match[1] === 'trash' ? prior : null };
      return json(saved);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<App />);
  await openWorkspaceMenu();
  if (!screen.getByLabelText('Switch project').parentElement?.hasAttribute('open')) await user.click(screen.getByLabelText('Switch project'));
  await user.click(screen.getByRole('button', { name: 'Project menu for Studio' }));
  await user.click(screen.getByRole('button', { name: /^Archive$/ }));
  expect(screen.getByRole('dialog')).toHaveTextContent('work and history stay saved');
  await user.click(screen.getByRole('button', { name: 'Archive project' }));
  await waitFor(() => expect(saved.status).toBe('archived'));
  await user.click(screen.getByRole('button', { name: /Archived/ }));
  const archived = await screen.findByText('Studio');
  await user.click(within(archived.closest('.project-stored-row')!).getByRole('button', { name: 'Restore' }));
  await waitFor(() => expect(saved.status).toBe('active'));
  await openWorkspaceMenu();
  await screen.findByRole('button', { name: 'Project menu for Studio' });
  if (!screen.getByLabelText('Switch project').parentElement?.hasAttribute('open')) await user.click(screen.getByLabelText('Switch project'));
  await user.click(screen.getByRole('button', { name: 'Project menu for Studio' }));
  await user.click(screen.getByRole('button', { name: 'Delete to Trash' }));
  const move = screen.getByRole('button', { name: 'Move to Trash' });
  expect(move).toBeDisabled();
  await user.type(screen.getByLabelText('Type the project name to confirm'), 'Studio');
  await user.click(move);
  await waitFor(() => expect(saved.status).toBe('trashed'));
  await user.click(screen.getByRole('button', { name: /Trash/ }));
  const trashed = await screen.findByText('Studio');
  await user.click(within(trashed.closest('.project-stored-row')!).getByRole('button', { name: 'Restore' }));
  await waitFor(() => expect(saved.status).toBe('active'));
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/projects/project-1').length).toBeGreaterThanOrEqual(3);
});

it('creates a project, reads it back, and displays real built-in profile state', async () => {
  let projectExists = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/projects/project-1/model/accounts') return json([modelAccount]);
    if (path === '/api/projects/project-1/model/catalog') return json([catalogModel]);
    if (path === '/api/projects' && init?.method === 'POST') {
      projectExists = true;
      return json(project, 201);
    }
    if (path.endsWith('/chief-workspace')) return json([]);
    if (path.endsWith('/coordination')) return json({ mode: 'off', paused: false, max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', version: 0, signal_types: {}, profile_tool_ceiling: [], registered_tools: ['search_read', 'file_read'], agent_rules_enabled: false });
    if (path === '/api/projects') return json(projectExists ? [project] : []);
    if (path === '/api/projects/project-1') return json(project);
    if (path === '/api/projects/project-1/profiles') return json([engineer]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<App />);
  expect(await screen.findByText('Create your first project.')).toBeInTheDocument();
  expect(screen.getByText('OneFoundry', { selector: '.brand strong' })).toBeInTheDocument();
  await user.click(screen.getAllByRole('button', { name: 'Create project' })[0]);
  await user.type(screen.getByLabelText('New project'), 'Studio');
  await user.click(screen.getByRole('button', { name: /^Create$/ }));

  expect(await screen.findByRole('heading', { name: 'Launch Studio' })).toBeInTheDocument();
  await user.click(within(screen.getByRole('complementary', { name: 'Conversation sidebar' })).getByRole('button', { name: 'Settings' }));
  await user.selectOptions(screen.getByRole('combobox', { name: 'Settings page' }), 'team');
  expect(await screen.findByRole('heading', { name: 'Engineer' })).toBeInTheDocument();
  expect(screen.getByText('Model unconfigured')).toBeInTheDocument();
  expect(screen.getByText('0 · inactive')).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => path === '/api/projects/project-1')).toBe(true);
  expect(screen.queryByRole('button', { name: /run agent/i })).not.toBeInTheDocument();
});

it('saves a project attachment before opening Setup', async () => {
  let projectExists = false;
  let attached: Record<string, unknown> | null = null;
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/projects' && init?.method === 'POST') {
      calls.push('project'); projectExists = true; return json(project, 201);
    }
    if (path.endsWith('/chief-workspace')) return json([]);
    if (path.endsWith('/coordination')) return json({ mode: 'off', paused: false, max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', version: 0, signal_types: {}, profile_tool_ceiling: [], registered_tools: ['search_read', 'file_read'], agent_rules_enabled: false });
    if (path === '/api/projects') return json(projectExists ? [project] : []);
    if (path === '/api/projects/project-1') return json(project);
    if (path === '/api/projects/project-1/profiles') return json([engineer]);
    if (path === '/api/projects/project-1/model/accounts' || path === '/api/projects/project-1/model/catalog' ||
        path === '/api/projects/project-1/model/providers' || path === '/api/projects/project-1/work-orders' ||
        path === '/api/projects/project-1/runs') return json([]);
    if (path === '/api/projects/project-1/input-files') return json(attached ? [attached] : []);
    if (path.startsWith('/api/projects/project-1/input-files/') && !init?.method) return json({ detail: 'missing' }, 404);
    if (path.startsWith('/api/projects/project-1/input-files?') && init?.method === 'POST') {
      calls.push('file');
      const headers = init.headers as Record<string, string>;
      attached = { id: headers['X-Client-File-Id'], project_id: project.id,
        display_name: 'brief.md', sha256: 'a'.repeat(64), byte_size: 5, status: 'pending' };
      return json(attached, 201);
    }
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup(); render(<App />);
  await screen.findByText('Create your first project.');
  await user.click(screen.getAllByRole('button', { name: 'Create project' })[0]);
  await user.type(screen.getByLabelText('New project'), 'Studio');
  await user.upload(screen.getByLabelText('Project files'), new File(['brief'], 'brief.md'));
  await user.click(screen.getByRole('button', { name: /^Create$/ }));
  expect(await screen.findByRole('heading', { name: 'Launch Studio' })).toBeInTheDocument();
  expect(calls).toEqual(['project', 'file']);
  expect(await screen.findByText(/brief.md/)).toBeInTheDocument();
});

it('keeps a project visible and allows another creation after attachment failure', async () => {
  let projectExists = false;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/projects' && init?.method === 'POST') {
      projectExists = true; return json(project, 201);
    }
    if (path.endsWith('/chief-workspace')) return json([]);
    if (path.endsWith('/coordination')) return json({ mode: 'off', paused: false, max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', version: 0, signal_types: {}, profile_tool_ceiling: [], registered_tools: ['search_read', 'file_read'], agent_rules_enabled: false });
    if (path === '/api/projects') return json(projectExists ? [project] : []);
    if (path === '/api/projects/project-1') return json(project);
    if (path === '/api/projects/project-1/profiles' ||
        path === '/api/projects/project-1/model/accounts' ||
        path === '/api/projects/project-1/model/catalog' ||
        path === '/api/projects/project-1/model/providers' ||
        path === '/api/projects/project-1/work-orders' ||
        path === '/api/projects/project-1/runs' ||
        path === '/api/projects/project-1/input-files') return json([]);
    if (path.startsWith('/api/projects/project-1/input-files/') && !init?.method) return json({}, 404);
    if (path.startsWith('/api/projects/project-1/input-files?') && init?.method === 'POST')
      return json({ detail: 'Input storage unavailable.' }, 503);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup(); render(<App />);
  await screen.findByText('Create your first project.');
  await user.click(screen.getAllByRole('button', { name: 'Create project' })[0]);
  await user.type(screen.getByLabelText('New project'), 'Studio');
  await user.upload(screen.getByLabelText('Project files'), new File(['brief'], 'brief.md'));
  await user.click(screen.getByRole('button', { name: /^Create$/ }));
  expect(await screen.findByText(/Project saved\. Attachment upload needs retry/)).toBeInTheDocument();
  expect(screen.getAllByText('Studio').length).toBeGreaterThan(0);
  await user.click(screen.getByRole('button', { name: 'Cancel' }));
  await openWorkspaceMenu();
  await user.click(screen.getAllByRole('button', { name: 'Create project' })[0]);
  expect(screen.getByLabelText('New project')).toBeEnabled();
  expect(screen.getByLabelText('New project')).toHaveValue('');
});

it('edits a built-in profile without changing its name or granting tools', async () => {
  let current: Record<string, unknown> = { ...engineer, reports_to_profile_id: chief.id };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/projects/project-1/model/accounts') return json([modelAccount]);
    if (path === '/api/projects/project-1/model/catalog') return json([catalogModel]);
    if (path.endsWith('/chief-workspace')) return json([]);
    if (path.endsWith('/coordination')) return json({ mode: 'off', paused: false, max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', version: 0, signal_types: {}, profile_tool_ceiling: [], registered_tools: ['search_read', 'file_read'], agent_rules_enabled: false });
    if (path === '/api/projects') return json([project]);
    if (path === '/api/projects/project-1') return json(project);
    if (path === '/api/projects/project-1/profiles') return json([chief, current]);
    if (path === '/api/projects/project-1/profiles/engineer-1' && init?.method === 'PATCH') {
      const fields = JSON.parse(init.body as string) as Record<string, unknown>;
      current = { ...current, ...fields, configured: true };
      return json(current);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<App />);
  await openWorkspaceMenu();
  await user.click(within(screen.getByRole('navigation', { name: 'Project sections' })).getByRole('button', { name: 'Settings' }));
  await user.selectOptions(screen.getByRole('combobox', { name: 'Settings page' }), 'team');
  const card = (await screen.findByRole('heading', { name: 'Engineer' })).closest('article');
  expect(card).not.toBeNull();
  await user.click(within(card!).getByRole('button', { name: 'Edit profile' }));
  expect(screen.getByText('· Built-in')).toBeInTheDocument();
  expect(screen.getByText(/Profile settings do not grant tool access/)).toBeInTheDocument();
  await screen.findByRole('option', { name: 'Model A · text' });
  await user.selectOptions(screen.getByLabelText('Model'), 'catalog-1');
  await user.type(screen.getByPlaceholderText('Describe the role’s responsibilities and working style'), 'Build carefully');
  await user.click(screen.getByRole('button', { name: 'Save profile' }));

  await waitFor(() => expect(screen.getByText('Profile saved. Its capabilities are checked when work starts.')).toBeInTheDocument());
  expect(screen.getByText('Profile ready')).toBeInTheDocument();
  const patchCall = fetcher.mock.calls.find(([path, init]) =>
    path === '/api/projects/project-1/profiles/engineer-1' && init?.method === 'PATCH');
  const body = JSON.parse(patchCall?.[1]?.body as string) as Record<string, unknown>;
  expect(body).not.toHaveProperty('name');
  expect(body).not.toHaveProperty('kind');
  expect(body).not.toHaveProperty('permissions_active');
});

it('creates a custom profile and shows the server readback with inactive tools', async () => {
  const profiles: Record<string, unknown>[] = [chief, engineer];
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/projects/project-1/model/accounts') return json([modelAccount]);
    if (path === '/api/projects/project-1/model/catalog') return json([catalogModel]);
    if (path.endsWith('/chief-workspace')) return json([]);
    if (path.endsWith('/coordination')) return json({ mode: 'off', paused: false, max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', version: 0, signal_types: {}, profile_tool_ceiling: [], registered_tools: ['search_read', 'file_read'], agent_rules_enabled: false });
    if (path === '/api/projects') return json([project]);
    if (path === '/api/projects/project-1') return json(project);
    if (path === '/api/projects/project-1/profiles' && init?.method === 'POST') {
      const fields = JSON.parse(init.body as string) as Record<string, unknown>;
      const created = { ...fields, id: 'custom-1', project_id: project.id, kind: 'custom',
        configured: true, permissions_active: false };
      profiles.push(created);
      return json(created, 201);
    }
    if (path === '/api/projects/project-1/profiles') return json(profiles);
    if (/^\/api\/projects\/project-1\/profiles\/[^/]+\/skills$/.test(path)) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<App />);
  await openWorkspaceMenu();
  await user.click(within(screen.getByRole('navigation', { name: 'Project sections' })).getByRole('button', { name: 'Settings' }));
  await user.selectOptions(screen.getByRole('combobox', { name: 'Settings page' }), 'team');
  await screen.findByRole('heading', { name: 'Engineer' });
  await user.click(screen.getByRole('button', { name: 'Add a profile' }));
  expect(screen.getByText(/Save the new profile, then reopen it to pin any SKILL.md versions/)).toBeInTheDocument();
  expect(screen.getByRole('option', { name: 'Company + this project' })).toBeInTheDocument();
  expect(screen.getByText("Company includes company memory across projects and this project's project memory.")).toBeInTheDocument();
  await user.type(screen.getByPlaceholderText('e.g. Product strategist'), 'Strategist');
  await screen.findByRole('option', { name: 'Model A · text' });
  await user.selectOptions(screen.getByLabelText('Model'), 'catalog-1');
  await user.type(screen.getByPlaceholderText('Describe the role’s responsibilities and working style'), 'Plan product strategy');
  await user.click(await screen.findByRole('checkbox', { name: 'Search read' }));
  await user.click(screen.getByRole('checkbox', { name: 'File read' }));
  await user.selectOptions(screen.getByLabelText('Memory scope'), 'company');
  await user.selectOptions(screen.getByLabelText('Autonomy level'), 'supervised');
  await user.click(screen.getByRole('button', { name: 'Save profile' }));

  expect(await screen.findByRole('heading', { name: 'Strategist' })).toBeInTheDocument();
  const card = screen.getByRole('heading', { name: 'Strategist' }).closest('article');
  expect(card).not.toBeNull();
  expect(within(card!).getByText('Profile ready')).toBeInTheDocument();
  expect(within(card!).getByText('2 · inactive')).toBeInTheDocument();
  expect(within(card!).getByText('Company + project')).toBeInTheDocument();
  expect(within(card!).getByText('Procedures: None · live text can match project skills')).toBeInTheDocument();
  const postCall = fetcher.mock.calls.find(([path, init]) =>
    path === '/api/projects/project-1/profiles' && init?.method === 'POST');
  expect(JSON.parse(postCall?.[1]?.body as string)).toEqual({
    name: 'Strategist', model: 'model-a', model_account_id: 'account-1', reasoning_effort: null,
    instructions: 'Plan product strategy',
    memory_scope: 'company', autonomy_level: 'supervised', allowed_tools: ['search_read', 'file_read'],
    reports_to_profile_id: chief.id,
  });
});

it('shows backend validation errors in the custom profile form', async () => {
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/projects/project-1/model/accounts') return json([modelAccount]);
    if (path === '/api/projects/project-1/model/catalog') return json([catalogModel]);
    if (path.endsWith('/chief-workspace')) return json([]);
    if (path.endsWith('/coordination')) return json({ mode: 'off', paused: false, max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', version: 0, signal_types: {}, profile_tool_ceiling: [], registered_tools: ['search_read', 'file_read'], agent_rules_enabled: false });
    if (path === '/api/projects') return json([project]);
    if (path === '/api/projects/project-1') return json(project);
    if (path === '/api/projects/project-1/profiles' && init?.method === 'POST') {
      return json({ detail: [{ msg: 'Model name is not available' }] }, 422);
    }
    if (path === '/api/projects/project-1/profiles') return json([engineer]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<App />);
  await openWorkspaceMenu();
  await user.click(within(screen.getByRole('navigation', { name: 'Project sections' })).getByRole('button', { name: 'Settings' }));
  await user.selectOptions(screen.getByRole('combobox', { name: 'Settings page' }), 'team');
  await screen.findByRole('heading', { name: 'Engineer' });
  await user.click(screen.getByRole('button', { name: 'Add a profile' }));
  await user.type(screen.getByPlaceholderText('e.g. Product strategist'), 'Strategist');
  await screen.findByRole('option', { name: 'Model A · text' });
  await user.selectOptions(screen.getByLabelText('Model'), 'catalog-1');
  await user.type(screen.getByPlaceholderText('Describe the role’s responsibilities and working style'), 'Plan product strategy');
  await user.click(screen.getByRole('button', { name: 'Save profile' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Model name is not available');
  expect(screen.getByRole('heading', { name: 'Add a custom profile' })).toBeInTheDocument();
});

it('recovers a retired project link to Home without carrying its task into another project', async () => {
  window.history.replaceState(null, '', '#project/retired-project/task?order=old-order');
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === '/api/projects') return json([project]);
    if (path === '/api/projects/project-1') return json(project);
    if (path === '/api/projects/project-1/profiles') return json([engineer]);
    if (path.endsWith('/work-orders') || path.endsWith('/approvals?status=pending') || path.endsWith('/skills')) return json([]);
    return json({ detail: 'Not available' }, 404);
  }));
  render(<App />);
  expect(await openWorkspaceMenu()).toBeInTheDocument();
  expect(screen.getByText(/The linked project is not active/)).toBeInTheDocument();
  expect(window.location.hash).toBe('#project/project-1/home');
  expect(screen.queryByRole('region', { name: 'Task' })).not.toBeInTheDocument();
});

it('opens a newly created project instead of routing back to the previous project', async () => {
  const second = { ...project, id: 'project-2', name: 'Field' };
  let created = false;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/projects' && init?.method === 'POST') { created = true; return json(second, 201); }
    if (path === '/api/projects') return json(created ? [project, second] : [project]);
    if (path === '/api/projects/project-1') return json(project);
    if (path === '/api/projects/project-2') return json(second);
    if (path.endsWith('/profiles')) return json([engineer]);
    return json([]);
  }));
  const user = userEvent.setup();
  render(<App />);
  await openWorkspaceMenu();
  await user.click(screen.getByLabelText('Switch project'));
  await user.click(screen.getByRole('button', { name: 'Create project' }));
  await user.type(screen.getByRole('textbox', { name: 'New project' }), 'Field');
  await user.click(screen.getByRole('button', { name: 'Create' }));
  expect(await screen.findByRole('heading', { name: 'Launch Field' })).toBeInTheDocument();
  expect(window.location.hash).toBe('#project/project-2/setup');
  expect(screen.getByLabelText('Switch project').parentElement).not.toHaveAttribute('open');
});
