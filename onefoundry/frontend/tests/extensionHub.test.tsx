import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ExtensionHub } from '../src/ExtensionHub';

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const packageItem = { id: 'pkg-1', project_id: 'project-a', package_key: 'customer-tools', package_version: '0.1.0',
  sha256: 'a'.repeat(64), state: 'draft', revision: 1, created_at: '2026-10-16T00:00:00Z',
  manifest: { description: 'Customer lookup', connections: [{ id: 'records', name: 'Customer records', kind: 'http', operations: [] },
    { id: 'events', name: 'Webhook events', kind: 'webhook', operations: [{ id: 'send', method: 'POST', path: '/events' }] }] } };
const tool = { id: 'customer.lookup', version: 1, description: 'Find a customer record', source: 'extension', status: 'draft', package_id: 'pkg-1' };
const opaqueToolId = `ext_${'a'.repeat(52)}_lookup_`;
function mockHub(options: { unknownDraft?: boolean; badConnection?: boolean; initialConnection?: boolean; unknownCredential?: boolean; sourceActive?: boolean; initialInvocation?: boolean; opaqueTool?: boolean; versionedOpaque?: boolean; cancelConflict?: boolean; cancelUnknown?: boolean; workerReason?: 'ready' | 'not_configured' | 'stale' | 'policy_mismatch' | 'unavailable' } = {}) {
  const writes: { path: string; body: unknown }[] = [];
  const drafts: unknown[] = [];
  const packages = options.versionedOpaque ? [
    { ...packageItem, id: 'pkg-old', state: 'revoked', manifest: { ...packageItem.manifest, name: 'Old Records' } },
    { ...packageItem, package_version: '0.2.0', state: 'active', manifest: { ...packageItem.manifest, name: 'New Records' } },
  ] : [{ ...packageItem, state: options.sourceActive ? 'active' : 'draft', manifest: options.opaqueTool ? { ...packageItem.manifest, name: 'Customer Records' } : packageItem.manifest }];
  const bindings: { package_id: string; slot: string; connection_id: string; version: number }[] = [];
  const webhooks: { id: string; name: string; enabled: boolean; version: number; credential_present: boolean; created_at: string }[] = [];
  const invocations: { invocation_id: string; tool_id: string; status: string; approval_id: string | null; reason: string | null }[] =
    options.initialInvocation ? [{ invocation_id: 'invocation-1', tool_id: options.opaqueTool || options.versionedOpaque ? opaqueToolId : 'customer.lookup', status: 'approval_required', approval_id: 'approval-1', reason: null }] : [];
  const connections: { id: string; project_id: string; package_id: string; template_id: string; label: string; config: object; enabled: boolean; version: number; created_at: string }[] =
    options.initialConnection ? [{ id: 'connection-1', project_id: 'project-a', package_id: 'pkg-1', template_id: 'records', label: 'Records',
      config: { transport: 'streamable_http', endpoint: 'https://example.com', auth_method: 'bearer' }, enabled: false, version: 1, created_at: '2026-10-16T00:00:00Z' }] : [];
  let credentialPresent = false;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const project = path.includes('/project-b/') ? 'project-b' : 'project-a';
    if (path.endsWith('/worker')) return json({ available: options.workerReason === 'ready', reason: options.workerReason ?? 'not_configured', ...(options.workerReason === 'ready' ? { age_seconds: 1 } : {}) });
    if (path.endsWith('/catalog')) return json({ tools: project !== 'project-a' ? [] : options.versionedOpaque ? [
      { ...tool, id: opaqueToolId, declared_id: 'lookup_customer', package_id: 'pkg-old', status: 'revoked' },
      { ...tool, id: opaqueToolId, declared_id: 'lookup_customer', package_id: 'pkg-1', status: 'active' },
    ] : [{ ...tool, ...(options.opaqueTool ? { id: opaqueToolId, declared_id: 'lookup_customer' } : {}), status: options.sourceActive ? 'active' : 'draft' }],
      packages, skills: options.opaqueTool || options.versionedOpaque ? [{ id: 'customer-brief', name: 'Customer brief', package_id: 'pkg-1', status: 'active', required_tools: [opaqueToolId] }] : [] });
    if (path.endsWith('/webhooks') && init?.method === 'POST') {
      writes.push({ path, body: JSON.parse(String(init.body)) });
      const item = { id: 'webhook-1', name: (JSON.parse(String(init.body)) as { name: string }).name, enabled: false,
        version: 1, credential_present: true, created_at: '2026-10-16T00:00:00Z' };
      webhooks.push(item); return json(item, 201);
    }
    if (path.endsWith('/webhooks')) return json(webhooks);
    if (path.endsWith('/credential') && path.includes('/webhooks/') && init?.method === 'PUT') {
      writes.push({ path, body: JSON.parse(String(init.body)) });
      webhooks[0] = { ...webhooks[0], version: webhooks[0].version + 1 };
      return json(webhooks[0]);
    }
    if (path.includes('/webhooks/') && init?.method === 'PATCH') {
      writes.push({ path, body: JSON.parse(String(init.body)) });
      webhooks[0] = { ...webhooks[0], enabled: !webhooks[0].enabled, version: webhooks[0].version + 1 };
      return json(webhooks[0]);
    }
    if (path.endsWith('/bindings')) return json(bindings);
    if (path.includes('/bindings/') && init?.method === 'PUT') {
      const body = JSON.parse(String(init.body)) as { connection_id: string; expected_version: number };
      writes.push({ path, body });
      const item = { package_id: 'pkg-1', slot: 'records', connection_id: body.connection_id, version: body.expected_version + 1 };
      bindings[0] = item; return json(item);
    }
    if (path.endsWith('/invocations')) return json(invocations);
    if (path.endsWith('/cancel') && path.includes('/invocations/') && init?.method === 'POST') {
      writes.push({ path, body: JSON.parse(String(init.body)) });
      if (options.cancelUnknown) throw new TypeError('network failed');
      if (options.cancelConflict) return json({ detail: 'An in-flight remote effect cannot be cancelled; await its exact receipt.' }, 409);
      invocations[0] = { ...invocations[0], status: 'cancelled', reason: 'operator_cancelled' };
      return json(invocations[0]);
    }
    if (path.endsWith('/packages/import') && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as { format: string; archive_base64: string };
      writes.push({ path, body });
      const imported = { ...packageItem, id: 'pkg-imported', package_key: 'imported-tools' };
      packages.push(imported);
      return json({ package: imported, report: { format: body.format, components: [{ component: 'skill', status: 'supported', reason: 'Imported' }] } }, 201);
    }
    if (path.endsWith('/packages')) return json(packages);
    if (path.endsWith('/drafts') && init?.method === 'POST') {
      writes.push({ path, body: JSON.parse(String(init.body)) });
      if (options.unknownDraft) throw new TypeError('network failed');
      const item = { id: 'draft-1', project_id: project, kind: 'skill', status: 'pending', sha256: 'b'.repeat(64),
        source_run_id: null, created_at: '2026-10-16T00:00:00Z', ...(JSON.parse(String(init.body)) as object) };
      drafts.push(item); return json(item, 201);
    }
    if (path.includes('/drafts/') && path.endsWith('/decision') && init?.method === 'POST') {
      writes.push({ path, body: JSON.parse(String(init.body)) });
      const decision = (JSON.parse(String(init.body)) as { decision: 'accept' | 'reject' }).decision;
      drafts[0] = { ...(drafts[0] as object), status: decision === 'accept' ? 'accepted' : 'rejected' };
      return json(drafts[0]);
    }
    if (path.endsWith('/drafts')) return json(drafts);
    if (path.endsWith('/readiness')) return json({ version: connections[0]?.version ?? 1, credential_present: credentialPresent,
      configured: true, enabled: connections[0]?.enabled ?? false, live_test_status: 'not_tested', successful_use: false });
    if (path.endsWith('/credential') && init?.method === 'PUT') {
      writes.push({ path, body: JSON.parse(String(init.body)) });
      if (options.unknownCredential) throw new TypeError('network failed');
      credentialPresent = true; connections[0].version += 1;
      return json({ credential_present: true, version: connections[0].version });
    }
    if (path.endsWith('/connections') && init?.method === 'POST') {
      writes.push({ path, body: JSON.parse(String(init.body)) });
      const body = JSON.parse(String(init.body)) as { package_id: string; template_id: string; label: string; config: object };
      const item = { id: 'connection-1', project_id: project, version: 1, enabled: false, created_at: '2026-10-16T00:00:00Z', ...body };
      connections.push(item); return json(item, 201);
    }
    if (path.includes('/connections/') && init?.method === 'PATCH') {
      writes.push({ path, body: JSON.parse(String(init.body)) });
      const body = JSON.parse(String(init.body)) as { expected_version: number; config: object };
      connections[0] = { ...connections[0], version: body.expected_version + 1, config: body.config };
      return json(connections[0]);
    }
    if (path.endsWith('/connections')) return json(options.badConnection ? { malformed: true } : connections);
    throw new Error(`Unexpected request ${path}`);
  }));
  return writes;
}
afterEach(() => vi.unstubAllGlobals());

it('shows worker readiness without treating local tool calls as ready when the worker is absent', async () => {
  mockHub();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  expect(await screen.findByText(/Python and local MCP tool calls cannot run until the worker is configured/)).toBeInTheDocument();
  expect(screen.getByText(/Isolated tool worker: Unavailable/)).toBeInTheDocument();
});

it('shows a ready isolated worker only when the gateway confirms it', async () => {
  mockHub({ workerReason: 'ready' });
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  expect(await screen.findByText(/Python and local MCP tool calls can be picked up by the worker/)).toBeInTheDocument();
  expect(screen.getByText(/Isolated tool worker: Ready/)).toBeInTheDocument();
});

it('opens the existing Team editor for tool role access without changing grants', async () => {
  mockHub({ sourceActive: true }); const onNavigate = vi.fn(); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={onNavigate} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Manage role access' }));
  expect(onNavigate).toHaveBeenCalledWith('team');
  await user.click(screen.getByRole('button', { name: 'Tool Foundry' }));
  await user.click(screen.getByRole('button', { name: 'Team' }));
  expect(onNavigate).toHaveBeenCalledTimes(2);
  expect(onNavigate).toHaveBeenLastCalledWith('team');
});

it('authors runnable Python and outbound webhook starter manifests', async () => {
  mockHub(); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Tool Foundry' }));
  await user.click(screen.getByRole('button', { name: /Plugin package/ }));
  const manifest = screen.getByLabelText('extension.json') as HTMLTextAreaElement;
  const files = screen.getByLabelText('Source files (JSON map)') as HTMLTextAreaElement;
  await user.click(screen.getByRole('button', { name: 'Python tool' }));
  const python = JSON.parse(manifest.value) as { tools: { parameters: { required: string[]; properties: object }; entrypoint: string }[] };
  const pythonFiles = JSON.parse(files.value) as Record<string, string>;
  expect(python.tools[0].parameters.required).toEqual(['text']);
  expect(python.tools[0].entrypoint).toBe('my_tool:run');
  expect(pythonFiles['src/my_tool.py']).toContain('async def run(context, text):');
  expect(pythonFiles['src/my_tool.py']).toContain('return {"text": text}');
  await user.click(screen.getByRole('button', { name: 'Outbound webhook' }));
  const webhook = JSON.parse(manifest.value) as { connections: { id: string; kind: string; operations: { id: string; method: string; path: string }[] }[];
    tools: { id: string; runtime: string; effect: string; connection: string; operation: string }[] };
  expect(webhook.connections[0]).toMatchObject({ id: 'events', kind: 'webhook', operations: [{ id: 'send', method: 'POST', path: '/events' }] });
  expect(webhook.tools[0]).toMatchObject({ id: 'send', runtime: 'http', effect: 'write', connection: 'events', operation: 'send' });
  expect(JSON.parse(files.value)).toEqual({});
});

it('shows live catalog state, creates a skill proposal and confirms exact readback', async () => {
  const writes = mockHub({ sourceActive: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  expect(await screen.findByRole('heading', { name: 'Lookup' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Tool Foundry' }));
  await user.type(screen.getByLabelText('Name'), 'Customer brief');
  await user.type(screen.getByLabelText('When to use it'), 'For account review');
  await user.type(screen.getByLabelText('SKILL.md instructions'), '# Brief\nUse the approved tool.');
  await user.click(screen.getByLabelText('Lookup'));
  await user.click(screen.getByRole('button', { name: 'Save proposal' }));
  expect(await screen.findByText(/Skill proposal saved/)).toBeInTheDocument();
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toMatchObject({ kind: 'skill', required_tools: ['customer.lookup'] });
  expect(screen.getByRole('heading', { name: 'Customer brief' })).toBeInTheDocument();
});

it('shows declared tool labels while saving full canonical tool IDs', async () => {
  const writes = mockHub({ sourceActive: true, opaqueTool: true, initialInvocation: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  expect(await screen.findByRole('heading', { name: 'Customer Records · Lookup Customer' })).toBeInTheDocument();
  expect(opaqueToolId).toHaveLength(64);
  expect(screen.queryByText(opaqueToolId)).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: /Customer brief/ }));
  expect(screen.getByText('Requires Customer Records · Lookup Customer')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Connections' }));
  expect(within(screen.getByText('Recent tool calls').closest('.extension-hub-invocations') as HTMLElement)
    .getByText('Customer Records · Lookup Customer')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Tool Foundry' }));
  const choice = screen.getByRole('checkbox', { name: 'Customer Records · Lookup Customer' }) as HTMLInputElement;
  expect(choice.value).toBe(opaqueToolId);
  await user.type(screen.getByLabelText('Name'), 'Call notes');
  await user.type(screen.getByLabelText('When to use it'), 'For account review');
  await user.type(screen.getByLabelText('SKILL.md instructions'), '# Notes\nUse the approved tool.');
  await user.click(choice);
  await user.click(screen.getByRole('button', { name: 'Save proposal' }));
  expect(await screen.findByText(/Skill proposal saved/)).toBeInTheDocument();
  expect(writes[0].body).toMatchObject({ required_tools: [opaqueToolId] });
  expect(screen.getByText('Requested tools: Customer Records · Lookup Customer')).toBeInTheDocument();
});

it('keeps versioned tool cards distinct and names ID-only uses from the active version', async () => {
  const writes = mockHub({ versionedOpaque: true, initialInvocation: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Old Records · Lookup Customer' });
  const catalog = screen.getByLabelText('Capability catalog');
  await user.click(within(catalog).getByRole('button', { name: /New Records · Lookup Customer/ }));
  const detail = screen.getByRole('region', { name: 'Capability details' });
  expect(within(detail).getByRole('heading', { name: 'New Records · Lookup Customer' })).toBeInTheDocument();
  expect(within(detail).getByText('Version 0.2.0')).toBeInTheDocument();
  expect(within(detail).getByRole('button', { name: 'Request revocation' })).toBeInTheDocument();
  await user.click(within(catalog).getByRole('button', { name: /Customer brief/ }));
  expect(within(detail).getByText('Requires New Records · Lookup Customer')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Connections' }));
  expect(within(screen.getByText('Recent tool calls').closest('.extension-hub-invocations') as HTMLElement)
    .getByText('New Records · Lookup Customer')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Tool Foundry' }));
  const choice = screen.getByRole('checkbox', { name: 'New Records · Lookup Customer' }) as HTMLInputElement;
  expect(choice.value).toBe(opaqueToolId);
  expect(screen.getAllByRole('checkbox', { name: /Lookup Customer/ })).toHaveLength(1);
  await user.type(screen.getByLabelText('Name'), 'Call notes');
  await user.type(screen.getByLabelText('When to use it'), 'For account review');
  await user.type(screen.getByLabelText('SKILL.md instructions'), '# Notes\nUse the approved tool.');
  await user.click(choice);
  await user.click(screen.getByRole('button', { name: 'Save proposal' }));
  expect(await screen.findByText(/Skill proposal saved/)).toBeInTheDocument();
  expect(writes[0].body).toMatchObject({ required_tools: [opaqueToolId] });
  expect(screen.getByText('Requested tools: New Records · Lookup Customer')).toBeInTheDocument();
});

it('distinguishes connection templates from package versions without changing their values', async () => {
  mockHub({ versionedOpaque: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Old Records · Lookup Customer' });
  await user.click(within(screen.getByLabelText('Capability catalog')).getByRole('button', { name: /New Records · Lookup Customer/ }));
  await user.click(within(screen.getByRole('region', { name: 'Capability details' })).getByRole('button', { name: 'Configure connection' }));
  const dialog = screen.getByRole('dialog', { name: 'Configure connection' });
  const chooser = within(dialog).getByLabelText('Connection template');
  expect(within(chooser).getByRole('option', { name: 'Old Records · v0.1.0 · Revoked · Customer records (HTTP)' })).toHaveValue('pkg-old:records');
  expect(within(chooser).getByRole('option', { name: 'New Records · v0.2.0 · Active · Customer records (HTTP)' })).toHaveValue('pkg-1:records');
  await user.selectOptions(chooser, 'pkg-1:records');
  expect(chooser).toHaveValue('pkg-1:records');
});

it('blocks uncertain skill resubmission until saved state is refreshed', async () => {
  const writes = mockHub({ unknownDraft: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Tool Foundry' }));
  await user.type(screen.getByLabelText('Name'), 'Customer brief');
  await user.type(screen.getByLabelText('When to use it'), 'For account review');
  await user.type(screen.getByLabelText('SKILL.md instructions'), '# Brief');
  await user.click(screen.getByRole('button', { name: 'Save proposal' }));
  expect(await screen.findByText(/outcome may be unknown/i)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Save proposal' })).toBeDisabled();
  expect(writes).toHaveLength(1);
});

it('rejects non-HTTPS connection endpoints without sending them and loads the other project cleanly', async () => {
  const writes = mockHub(); const user = userEvent.setup();
  const mounted = render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Connections' }));
  await user.click(screen.getByRole('button', { name: 'Add connection' }));
  const dialog = screen.getByRole('dialog');
  await user.selectOptions(within(dialog).getByLabelText('Connection template'), 'pkg-1:records');
  await user.type(within(dialog).getByLabelText('Label'), 'Records');
  await user.type(within(dialog).getByLabelText('HTTPS endpoint'), 'http://example.com');
  await user.click(within(dialog).getByRole('button', { name: 'Save connection draft' }));
  expect(within(dialog).getByText(/Enter an HTTPS endpoint/)).toBeInTheDocument();
  expect(writes).toHaveLength(0);
  mounted.rerender(<ExtensionHub key="project-b" projectId="project-b" onNavigate={vi.fn()} />);
  await waitFor(() => expect(screen.queryByRole('heading', { name: 'Lookup' })).not.toBeInTheDocument());
  expect(screen.getByRole('heading', { name: 'Customer Tools' })).toBeInTheDocument();
});

it('shows malformed owner state as an error rather than an empty success', async () => {
  mockHub({ badConnection: true });
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('unexpected list');
  expect(screen.queryByText('Lookup')).not.toBeInTheDocument();
});

it('clears a protected credential only after version and readiness readback', async () => {
  const writes = mockHub({ initialConnection: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Connections' }));
  const details = screen.getByRole('region', { name: 'Connection details' });
  const input = within(details).getByLabelText('Protected credential') as HTMLInputElement;
  await user.type(input, 'test-secret-value');
  await user.click(within(details).getByRole('button', { name: 'Save credential' }));
  expect(await screen.findByText(/Credential saved in the protected store/)).toBeInTheDocument();
  expect(input.value).toBe('');
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toEqual({ value: 'test-secret-value', expected_version: 1 });
  expect(screen.getByText('Stored')).toBeInTheDocument();
});

it('preserves an uncertain credential entry and prevents a second write', async () => {
  const writes = mockHub({ initialConnection: true, unknownCredential: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Connections' }));
  const details = screen.getByRole('region', { name: 'Connection details' });
  const input = within(details).getByLabelText('Protected credential') as HTMLInputElement;
  await user.type(input, 'test-secret-value');
  await user.click(within(details).getByRole('button', { name: 'Save credential' }));
  expect(await screen.findByText(/outcome may be unknown/i)).toBeInTheDocument();
  expect(input.value).toBe('test-secret-value');
  expect(within(details).getByRole('button', { name: 'Save credential' })).toBeDisabled();
  expect(writes).toHaveLength(1);
});

it('edits a saved connection with the current version and reads the new version back', async () => {
  const writes = mockHub({ initialConnection: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Connections' }));
  await user.click(screen.getByRole('button', { name: 'Edit settings' }));
  const dialog = screen.getByRole('dialog');
  await user.clear(within(dialog).getByLabelText('HTTPS endpoint'));
  await user.type(within(dialog).getByLabelText('HTTPS endpoint'), 'https://service.example/mcp');
  await user.click(within(dialog).getByRole('button', { name: 'Save connection settings' }));
  expect(await screen.findByText('Connection settings saved and confirmed.')).toBeInTheDocument();
  expect(writes[0].body).toMatchObject({ expected_version: 1, config: { endpoint: 'https://service.example/mcp' } });
  expect(screen.getByText(/Version 2/)).toBeInTheDocument();
});

it('imports a ZIP draft and shows its compatibility report after saved readback', async () => {
  const writes = mockHub(); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Tool Foundry' }));
  await user.click(screen.getByRole('button', { name: /Plugin package/ }));
  await user.selectOptions(screen.getByLabelText('Package format'), 'claude');
  await user.upload(screen.getByLabelText('ZIP archive (up to 4 MiB)'), new File(['PKzip'], 'extension.zip', { type: 'application/zip' }));
  expect((screen.getByLabelText('ZIP archive (up to 4 MiB)') as HTMLInputElement).files).toHaveLength(1);
  await user.click(screen.getByRole('button', { name: 'Import inert draft' }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(await screen.findByText(/Package imported as an inert draft/)).toBeInTheDocument();
  expect(writes[0].body).toMatchObject({ format: 'claude', archive_base64: 'UEt6aXA=' });
  expect(screen.getByText(/skill: supported/)).toBeInTheDocument();
});

it('saves a versioned connection binding only after confirming the persisted slot', async () => {
  const writes = mockHub({ initialConnection: true, sourceActive: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  const bindings = screen.getByText('Connection bindings').closest('.extension-hub-bindings') as HTMLElement;
  await user.selectOptions(within(bindings).getByLabelText('Customer records (HTTP)'), 'connection-1');
  await user.click(within(within(bindings).getByLabelText('Customer records (HTTP)').closest('.extension-hub-binding') as HTMLElement).getByRole('button', { name: 'Save binding' }));
  expect(await screen.findByText('Connection binding saved and confirmed.')).toBeInTheDocument();
  expect(writes[0].body).toEqual({ connection_id: 'connection-1', expected_version: 0 });
  expect(within(bindings).getByText('Saved binding · version 1')).toBeInTheDocument();
  expect(within(bindings).getByLabelText('Customer records (HTTP)')).toHaveValue('connection-1');
  await user.click(screen.getByRole('button', { name: 'Refresh saved state' }));
  await waitFor(() => expect(screen.getByLabelText('Customer records (HTTP)')).toHaveValue('connection-1'));
});

it('creates a disabled signed webhook and clears its write-only secret after saved readback', async () => {
  const writes = mockHub(); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Connections' }));
  const area = screen.getByRole('region', { name: 'Incoming webhooks' });
  await user.type(within(area).getByLabelText('Name'), 'CRM events');
  const secret = within(area).getByLabelText('Signing secret') as HTMLInputElement;
  await user.type(secret, 'a'.repeat(32));
  await user.click(within(area).getByRole('button', { name: 'Save disabled webhook' }));
  expect(await screen.findByText(/Webhook saved disabled/)).toBeInTheDocument();
  expect(writes[0].body).toEqual({ name: 'CRM events', signing_secret: 'a'.repeat(32), enabled: false });
  expect(secret.value).toBe('');
  expect(within(area).getByText('Signing secret: Stored')).toBeInTheDocument();
});

it('creates a reusable outbound webhook connection from its declared template', async () => {
  const writes = mockHub(); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Connections' }));
  await user.click(screen.getByRole('button', { name: 'Add connection' }));
  const dialog = screen.getByRole('dialog');
  await user.selectOptions(within(dialog).getByLabelText('Connection template'), 'pkg-1:events');
  expect(within(dialog).getByText(/POST send/)).toBeInTheDocument();
  await user.type(within(dialog).getByLabelText('Label'), 'Outbound events');
  await user.type(within(dialog).getByLabelText('HTTPS endpoint'), 'https://hooks.example/events');
  await user.click(within(dialog).getByRole('button', { name: 'Save connection draft' }));
  expect(await screen.findByText(/Connection draft saved and confirmed/)).toBeInTheDocument();
  expect(writes[0].body).toMatchObject({ template_id: 'events', config: { endpoint: 'https://hooks.example/events' } });
});

it('cancels an approval-waiting tool call and confirms its saved receipt', async () => {
  const writes = mockHub({ initialInvocation: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Connections' }));
  await user.click(screen.getByRole('button', { name: 'Cancel call' }));
  expect(await screen.findByText('Tool call cancelled and confirmed.')).toBeInTheDocument();
  expect(writes[0].path).toContain('/invocations/invocation-1/cancel');
  expect(screen.queryByRole('button', { name: 'Cancel call' })).not.toBeInTheDocument();
});

it('shows a known remote cancellation conflict without claiming cancellation', async () => {
  const writes = mockHub({ initialInvocation: true, cancelConflict: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Connections' }));
  await user.click(screen.getByRole('button', { name: 'Cancel call' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('in-flight remote effect cannot be cancelled');
  expect(screen.getByRole('button', { name: 'Cancel call' })).toBeEnabled();
  expect(writes).toHaveLength(1);
});

it('blocks cancellation retry when the saved outcome is unknown', async () => {
  const writes = mockHub({ initialInvocation: true, cancelUnknown: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Connections' }));
  await user.click(screen.getByRole('button', { name: 'Cancel call' }));
  expect(await screen.findByText(/outcome may be unknown/i)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Cancel call' })).toBeDisabled();
  expect(writes).toHaveLength(1);
});

it('saves and accepts a skill with a canonical project tool prerequisite', async () => {
  const writes = mockHub({ sourceActive: true }); const user = userEvent.setup();
  render(<ExtensionHub projectId="project-a" onNavigate={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Lookup' });
  await user.click(screen.getByRole('button', { name: 'Tool Foundry' }));
  await user.type(screen.getByLabelText('Name'), 'Customer brief');
  await user.type(screen.getByLabelText('When to use it'), 'For customer review');
  await user.type(screen.getByLabelText('SKILL.md instructions'), '# Brief\nUse the approved lookup tool.');
  await user.click(screen.getByLabelText('Lookup'));
  await user.click(screen.getByRole('button', { name: 'Save proposal' }));
  expect(await screen.findByText(/Skill proposal saved/)).toBeInTheDocument();
  expect(writes[0].body).toMatchObject({ required_tools: ['customer.lookup'] });
  await user.click(screen.getByRole('button', { name: 'Accept exact draft' }));
  expect(await screen.findByText(/Skill draft accepted and confirmed/)).toBeInTheDocument();
  expect(writes[1].body).toMatchObject({ decision: 'accept', expected_sha256: 'b'.repeat(64) });
  expect(screen.queryByRole('button', { name: 'Accept exact draft' })).not.toBeInTheDocument();
});
