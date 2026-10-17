import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SetupView } from '../src/SetupView';

const id = 'project-1';
const project = { id, name: 'Studio', goal: '', desired_outcome: '', status: 'active', version: 1, trash_from_status: null };
const role = (name: string) => ({ id: name.toLowerCase(), project_id: id, kind: 'builtin', name, reports_to_profile_id: null, model: null, model_account_id: null,
  instructions: '', memory_scope: 'project', allowed_tools: [], autonomy_level: 'manual', configured: false, permissions_active: false });
const names = ['Engineer', 'QA', 'Researcher', 'GTM', 'Sales'];
const account = { id: 'account-1', project_id: id, provider: 'openai', label: 'Company API', auth_kind: 'api_key', endpoint: null,
  credential_present: true, catalog_valid: true, model_count: 1, last_refreshed_at: '2026-09-26T00:00:00Z' };
const provider = { id: 'openai', name: 'OpenAI', auth_kind: 'api_key', catalog_supported: true, text_runtime_available: true, connectable: true };
const model = { id: 'catalog-1', account_id: account.id, provider: 'openai', model_id: 'text-1', display_name: 'Text One', category: 'text', available: true,
  reasoning_efforts: [] as string[] };
const order = { id: 'order-1', project_id: id, title: 'First draft', instructions: 'Investigate', profile_id: 'researcher',
  status: 'draft', version: 1, block_reason: null, blocked_from_status: null, created_at: '2026-09-26T00:00:00Z' };
const run = { id: 'run-1', project_id: id, work_order_id: order.id, work_order_title: order.title, work_order_instructions: order.instructions,
  profile_id: 'researcher', profile_model: null, status: 'done', mode: 'manual_planning', tokens_used: 0, cost_usd: 0,
  outcome_reason: 'Reviewed', started_at: '2026-09-26T00:00:00Z', finished_at: '2026-09-26T00:10:00Z' };
function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
type Fixture = { project: typeof project; roles: ReturnType<typeof role>[]; accounts: typeof account[]; providers: typeof provider[];
  models: typeof model[]; orders: typeof order[]; runs: typeof run[] };
function fixture(): Fixture { return { project: { ...project }, roles: names.map(role), accounts: [], providers: [provider], models: [], orders: [], runs: [] }; }
function serve(state: Fixture) {
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${id}`) return json(state.project);
    if (path.endsWith('/brief') && init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body));
      if (body.expected_version !== state.project.version) return json({ detail: 'Version conflict' }, 409);
      state.project = { ...state.project, goal: body.goal, desired_outcome: body.desired_outcome, version: state.project.version + 1 };
      return json(state.project);
    }
    if (path.endsWith('/profiles/starter') && init?.method === 'POST') {
      state.roles = state.roles.map((item) => ({ ...item, instructions: item.instructions || `Useful ${item.name} instructions` }));
      return json(state.roles);
    }
    if (path.endsWith('/profiles') && init?.method !== 'PATCH') return json(state.roles);
    if (path.includes('/profiles/') && init?.method === 'PATCH') {
      const fields = JSON.parse(String(init.body));
      state.roles = state.roles.map((item) => item.id === path.split('/').at(-1) ? { ...item, ...fields, configured: true } : item);
      return json(state.roles.find((item) => item.id === path.split('/').at(-1)));
    }
    if (path.endsWith('/model/accounts')) return json(state.accounts);
    if (path.endsWith('/model/providers')) return json(state.providers);
    if (path.endsWith('/model/catalog')) return json(state.models);
    if (path.endsWith('/work-orders') && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)); state.orders = [{ ...order, ...body }]; return json(state.orders[0]);
    }
    if (path.endsWith('/work-orders')) return json(state.orders);
    if (path.endsWith('/runs')) return json(state.runs);
    if (path === '/api/source-roots') return json([]);
    if (path === '/api/source-roots/workspace/repositories') return json({ available: false, truncated: false, repositories: [] });
    if (path === `/api/projects/${id}/sources`) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}
afterEach(() => vi.unstubAllGlobals());

it('navigates setup steps without rereading or clearing saved state', async () => {
  const fetcher = serve(fixture()); const user = userEvent.setup();
  render(<SetupView project={project} onNavigate={vi.fn()} />);
  await screen.findByLabelText('0 of 4 core steps recorded');
  const initialGets = fetcher.mock.calls.filter(([path, init]) => path === `/api/projects/${id}` && !init?.method).length;
  await user.click(screen.getByRole('button', { name: 'Next' }));
  expect(screen.getByRole('heading', { name: 'Give the team a useful starting point' })).toBeInTheDocument();
  await user.click(within(screen.getByRole('navigation', { name: 'Setup steps' })).getByRole('button', { name: /Model connection/ }));
  expect(screen.getByRole('heading', { name: 'Connect a text model' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Continue without AI or a repository' }));
  expect(screen.getByRole('heading', { name: 'Write the first work order' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Back' }));
  expect(screen.getByRole('heading', { name: 'Select a project repository' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Continue without a repository' }));
  expect(screen.getByLabelText('0 of 4 core steps recorded')).toBeInTheDocument();
  expect(screen.queryByText('Reading saved setup state…')).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path, init]) => path === `/api/projects/${id}` && !init?.method)).toHaveLength(initialGets);
});

it('resumes an unconnected project at its manual outcome instead of treating AI setup as required', async () => {
  const state = fixture();
  state.project.goal = 'Understand customers'; state.project.desired_outcome = 'A reviewed brief';
  state.roles = state.roles.map((item) => ({ ...item, instructions: `Plan ${item.name} work` }));
  state.orders = [order];
  const fetcher = serve(state);
  render(<SetupView project={project} onNavigate={vi.fn()} />);
  expect(await screen.findByRole('heading', { name: 'Inspect a saved outcome' })).toBeInTheDocument();
  expect(screen.getByText(/No model or repository is needed for manual planning/)).toBeInTheDocument();
  expect(screen.getByLabelText('3 of 4 core steps recorded')).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path, init]) => path.endsWith('/model/accounts') && init?.method === 'POST')).toBe(false);
});

it('reads saved repository state and explains when the local root is unavailable', async () => {
  const fetcher = serve(fixture()); const navigate = vi.fn(); const user = userEvent.setup();
  render(<SetupView project={project} onNavigate={navigate} />);
  await screen.findByLabelText('0 of 4 core steps recorded');
  await user.click(within(screen.getByRole('navigation', { name: 'Setup steps' })).getByRole('button', { name: /Project repository/ }));
  expect(await screen.findByText('No local repository folder is mounted. You can continue without one. To add a local checkout, the person running OneFoundry must mount its folder read-only into the app, then refresh this page.')).toBeInTheDocument();
  expect(screen.getByText('python3 scripts/local_repositories.py add /absolute/path/to/repository')).toBeInTheDocument();
  expect(screen.getByText('No repository is saved for this project yet.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open repositories to select' }));
  expect(navigate).toHaveBeenCalledWith('integrations');
  expect(fetcher.mock.calls.some(([path]) => path === `/api/projects/${id}/sources`)).toBe(true);
});

it('selects a discovered local repository and verifies project source readback', async () => {
  const state = fixture(); const revision = 'b'.repeat(40);
  const selected = { id: 'source-1', project_id: id, kind: 'local', display_name: 'studio', root_id: 'workspace',
    relative_path: 'studio', account_id: null, owner: null, repo: null, branch: 'main', revision,
    availability: 'verified_at_selection' };
  const sources: typeof selected[] = [];
  const original = serve(state).getMockImplementation()!;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/source-roots') return json([{ id: 'workspace', label: 'Allowed workspace', available: true }]);
    if (path === '/api/source-roots/workspace/repositories') return json({ available: true, truncated: false,
      repositories: [{ relative_path: 'studio', display_name: 'studio', branch: 'main', revision, dirty: false }] });
    if (path === `/api/projects/${id}/sources/local` && init?.method === 'POST') {
      expect(JSON.parse(String(init.body))).toEqual({ root_id: 'workspace', relative_path: 'studio' });
      sources.push(selected); return json(selected, 201);
    }
    if (path === `/api/projects/${id}/sources`) return json(sources);
    return original(path, init);
  }));
  const user = userEvent.setup(); render(<SetupView project={project} onNavigate={vi.fn()} />);
  await screen.findByLabelText('0 of 4 core steps recorded');
  await user.click(within(screen.getByRole('navigation', { name: 'Setup steps' })).getByRole('button', { name: /Project repository/ }));
  await screen.findByRole('option', { name: 'studio · main' });
  await user.selectOptions(screen.getByLabelText('Local repository'), 'studio');
  await user.click(screen.getByRole('button', { name: 'Select local repository' }));
  expect(await screen.findByText('Project repository saved and confirmed.')).toBeInTheDocument();
  expect(screen.getByText(/studio · main · Verified at selection/)).toBeInTheDocument();
});

it('saves the brief, fills only missing starter instructions, and creates a draft with readback', async () => {
  const state = fixture(); state.roles[0].instructions = 'Keep my Engineer instructions';
  const fetcher = serve(state); const user = userEvent.setup();
  render(<SetupView project={project} onNavigate={vi.fn()} />);
  expect(await screen.findByLabelText('0 of 4 core steps recorded')).toBeInTheDocument();
  await user.type(screen.getByLabelText('Project goal'), 'Understand customers');
  await user.type(screen.getByLabelText('Desired outcome'), 'A cited brief');
  await user.click(screen.getByRole('button', { name: 'Save project brief' }));
  expect(await screen.findByText('Project brief saved and confirmed.')).toBeInTheDocument();
  expect(JSON.parse(String(fetcher.mock.calls.find(([path]) => path.endsWith('/brief'))?.[1]?.body))).toEqual({
    goal: 'Understand customers', desired_outcome: 'A cited brief', expected_version: 1,
  });
  await user.click(screen.getByRole('button', { name: 'Next' }));
  expect(screen.getByText('Keep my Engineer instructions')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Fill missing starter instructions' }));
  expect(await screen.findByText('Starter instructions saved and confirmed.')).toBeInTheDocument();
  expect(state.roles[0].instructions).toBe('Keep my Engineer instructions');
  await user.click(within(screen.getByRole('navigation', { name: 'Setup steps' })).getByRole('button', { name: /First work order/ }));
  await user.click(screen.getByRole('button', { name: 'Use project brief in draft' }));
  expect((screen.getByLabelText('Instructions') as HTMLTextAreaElement).value).toContain('Desired outcome: A cited brief');
  await user.type(screen.getByLabelText('Instructions'), 'Investigate');
  await user.selectOptions(screen.getByLabelText('Assign to role'), 'researcher');
  await user.click(screen.getByRole('button', { name: 'Save draft work order' }));
  expect(await screen.findByText('Draft work order saved and confirmed.')).toBeInTheDocument();
  expect(state.orders[0].status).toBe('draft');
  expect(state.orders[0].instructions).toContain('Project goal: Understand customers');
  expect(state.orders[0].instructions).toContain('Desired outcome: A cited brief');
  expect(fetcher.mock.calls.some(([path, init]) => path.endsWith('/runs') && init?.method === 'POST')).toBe(false);
});

it('adds project files and first work order files during Setup', async () => {
  const state = fixture();
  const baseline = serve(state);
  const files: Record<string, unknown>[] = [];
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.startsWith(`/api/projects/${id}/input-files/`) && !init?.method) return json({}, 404);
    if (path.startsWith(`/api/projects/${id}/input-files?`) && init?.method === 'POST') {
      const headers = init.headers as Record<string, string>;
      const name = decodeURIComponent(headers['X-File-Name']);
      const row = { id: headers['X-Client-File-Id'], project_id: id,
        display_name: name, sha256: 'c'.repeat(64), byte_size: name === 'goal.md' ? 4 : 5,
        status: 'pending', scope: path.includes('scope=project') ? 'project' : 'work_order' };
      files.push(row); return json(row, 201);
    }
    if (path === `/api/projects/${id}/input-files`) return json(files.filter((item) => item.scope === 'project'));
    if (path.startsWith(`/api/projects/${id}/input-files?work_order_id=`)) return json(files);
    return baseline(path, init);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); render(<SetupView project={project} onNavigate={vi.fn()} />);
  await screen.findByLabelText('0 of 4 core steps recorded');
  await user.upload(screen.getByLabelText('Add project files'), new File(['goal'], 'goal.md'));
  await user.click(screen.getByRole('button', { name: 'Save project files' }));
  expect(await screen.findByText('Project files saved. Processing status appears below.')).toBeInTheDocument();
  await user.click(within(screen.getByRole('navigation', { name: 'Setup steps' })).getByRole('button', { name: /First work order/ }));
  await user.type(screen.getByPlaceholderText('What should this role deliver?'), 'First draft');
  await user.type(screen.getByPlaceholderText('Scope, expected result, and boundaries'), 'Write a brief');
  await user.selectOptions(screen.getByLabelText('Assign to role'), 'researcher');
  await user.upload(screen.getByLabelText('First work order files'), new File(['brief'], 'brief.md'));
  await user.click(screen.getByRole('button', { name: 'Save draft work order' }));
  expect(await screen.findByText('1 work orders saved')).toBeInTheDocument();
  expect(files.map((item) => item.display_name)).toEqual(['goal.md', 'brief.md']);
});

it('keeps pending sign-in separate from catalog and assigns only a ready text model with readback', async () => {
  const state = fixture(); state.project.goal = 'Goal'; state.project.desired_outcome = 'Result';
  state.accounts = [{ ...account, credential_present: false, catalog_valid: false, model_count: 0, auth_kind: 'account_runtime' }];
  state.models = [model]; const fetcher = serve(state); const user = userEvent.setup();
  render(<SetupView project={project} onNavigate={vi.fn()} />);
  await screen.findByLabelText('1 of 4 core steps recorded');
  await user.click(within(screen.getByRole('navigation', { name: 'Setup steps' })).getByRole('button', { name: /Model connection/ }));
  expect(screen.getByText(/sign-in pending/)).toBeInTheDocument();
  expect(screen.queryByLabelText('Available text model')).not.toBeInTheDocument();
  state.accounts = [{ ...account }];
  await user.click(screen.getByRole('button', { name: 'Refresh saved progress' }));
  expect(await screen.findByLabelText('Available text model')).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('Available text model'), model.id);
  await user.click(screen.getByRole('button', { name: 'Assign model to Researcher and GTM' }));
  expect(await screen.findByText('Both model and reasoning settings saved and confirmed.')).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path, init]) => path.includes('/profiles/') && init?.method === 'PATCH')).toHaveLength(2);
  for (const [, init] of fetcher.mock.calls.filter(([path, options]) => path.includes('/profiles/') && options?.method === 'PATCH')) {
    expect(JSON.parse(String(init?.body))).toEqual({ model: model.model_id, model_account_id: account.id,
      reasoning_effort: null });
  }
  expect(screen.getByLabelText('1 of 4 core steps recorded')).toBeInTheDocument();
});

it('saves separate discovered reasoning choices for each starter agent in Setup', async () => {
  const state = fixture();
  state.accounts = [{ ...account, provider: 'openai_codex_runtime', auth_kind: 'account_runtime' }];
  state.providers = [{ ...provider, id: 'openai_codex_runtime', auth_kind: 'account_runtime' }];
  state.models = [{ ...model, provider: 'openai_codex_runtime', reasoning_efforts: ['low', 'high', 'xhigh', 'ultra'] }];
  const fetcher = serve(state); const user = userEvent.setup();
  render(<SetupView project={project} onNavigate={vi.fn()} />);
  await screen.findByLabelText('0 of 4 core steps recorded');
  await user.click(within(screen.getByRole('navigation', { name: 'Setup steps' })).getByRole('button', { name: /Model connection/ }));
  await user.selectOptions(screen.getByLabelText('Available text model'), model.id);
  await user.selectOptions(screen.getByLabelText('Researcher reasoning effort'), 'low');
  await user.selectOptions(screen.getByLabelText('GTM reasoning effort'), 'ultra');
  await user.click(screen.getByRole('button', { name: 'Assign model to Researcher and GTM' }));
  expect(await screen.findByText('Both model and reasoning settings saved and confirmed.')).toBeInTheDocument();
  const writes = fetcher.mock.calls.filter(([path, init]) => path.includes('/profiles/') && init?.method === 'PATCH');
  expect(writes.map(([, init]) => JSON.parse(String(init?.body)).reasoning_effort)).toEqual(['low', 'ultra']);
  expect(state.roles.find((item) => item.name === 'Researcher')).toMatchObject({ model: model.model_id, reasoning_effort: 'low' });
  expect(state.roles.find((item) => item.name === 'GTM')).toMatchObject({ model: model.model_id, reasoning_effort: 'ultra' });
});

it('reports a failed brief write and never marks it saved', async () => {
  const state = fixture(); const fetcher = serve(state);
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation(async (path: string, init?: RequestInit) => path.endsWith('/brief') ? json({ detail: 'Version conflict' }, 409) : original(path, init));
  const user = userEvent.setup(); render(<SetupView project={project} onNavigate={vi.fn()} />);
  await screen.findByLabelText('0 of 4 core steps recorded');
  await user.type(screen.getByLabelText('Project goal'), 'Goal');
  await user.type(screen.getByLabelText('Desired outcome'), 'Outcome');
  await user.click(screen.getByRole('button', { name: 'Save project brief' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Version conflict');
  expect(screen.getByLabelText('0 of 4 core steps recorded')).toBeInTheDocument();
});

it('does not claim completion when the brief write lacks saved readback', async () => {
  const state = fixture(); const fetcher = serve(state);
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation(async (path: string, init?: RequestInit) => path.endsWith('/brief')
    ? json({ ...state.project, goal: 'Goal', desired_outcome: 'Outcome', version: 2 }) : original(path, init));
  const user = userEvent.setup(); render(<SetupView project={project} onNavigate={vi.fn()} />);
  await screen.findByLabelText('0 of 4 core steps recorded');
  await user.type(screen.getByLabelText('Project goal'), 'Goal');
  await user.type(screen.getByLabelText('Desired outcome'), 'Outcome');
  await user.click(screen.getByRole('button', { name: 'Save project brief' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('saved state did not confirm it');
  expect(screen.getByLabelText('0 of 4 core steps recorded')).toBeInTheDocument();
});

it('does not confirm a draft whose saved instructions lost the project context', async () => {
  const state = fixture(); state.project.goal = 'Research customers'; state.project.desired_outcome = 'A cited brief';
  const fetcher = serve(state); const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/work-orders') && init?.method === 'POST') {
      const body = JSON.parse(String(init.body));
      state.orders = [{ ...order, ...body, instructions: 'Context was lost' }];
      return json({ ...order, ...body });
    }
    return original(path, init);
  });
  const user = userEvent.setup(); render(<SetupView project={project} onNavigate={vi.fn()} />);
  await screen.findByLabelText('1 of 4 core steps recorded');
  await user.click(within(screen.getByRole('navigation', { name: 'Setup steps' })).getByRole('button', { name: /First work order/ }));
  await user.type(screen.getByLabelText('Title'), 'First draft');
  await user.type(screen.getByLabelText('Instructions'), 'Investigate');
  await user.selectOptions(screen.getByLabelText('Assign to role'), 'researcher');
  await user.click(screen.getByRole('button', { name: 'Save draft work order' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('saved state did not confirm it');
  expect(screen.queryByText('Draft work order saved and confirmed.')).not.toBeInTheDocument();
});

it('resumes from persisted progress and opens a saved outcome', async () => {
  const state = fixture(); state.project.goal = 'Goal'; state.project.desired_outcome = 'Result';
  state.roles = state.roles.map((item) => ({ ...item, instructions: 'Instructions' }));
  state.orders = [order]; state.runs = [run]; serve(state);
  const navigate = vi.fn(); const user = userEvent.setup();
  render(<SetupView project={project} onNavigate={navigate} />);
  expect(await screen.findByLabelText('4 of 4 core steps recorded')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Inspect a saved outcome' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: /First draft/ }));
  expect(navigate).toHaveBeenCalledWith('activity', run.id);
});

it('keeps an unknown run unresolved and links it to Activity', async () => {
  const state = fixture(); state.project.goal = 'Goal'; state.project.desired_outcome = 'Result';
  state.roles = state.roles.map((item) => ({ ...item, instructions: 'Instructions' }));
  state.orders = [order]; state.runs = [{ ...run, status: 'unknown', outcome_reason: 'Provider state unclear' }];
  serve(state);
  const navigate = vi.fn(); const user = userEvent.setup();
  render(<SetupView project={project} onNavigate={navigate} />);
  expect(await screen.findByLabelText('3 of 4 core steps recorded')).toBeInTheDocument();
  await user.click(within(screen.getByRole('navigation', { name: 'Setup steps' })).getByRole('button', { name: /Saved outcome/ }));
  expect(screen.getByText('No confirmed outcome yet. An unknown run needs review in Activity.')).toBeInTheDocument();
  const unresolved = screen.getByRole('heading', { name: 'Unresolved runs' }).parentElement!;
  await user.click(within(unresolved).getByRole('button', { name: /First draft/ }));
  expect(navigate).toHaveBeenCalledWith('activity', run.id);
});
