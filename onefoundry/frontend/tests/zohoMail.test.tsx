import { afterEach, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ZohoMail } from '../src/ZohoMail';

vi.mock('../src/workApi', () => ({ listRuns: vi.fn(async () => [{ id: 'run-1', mode: 'manual_planning', status: 'running', work_order_title: 'Reply' }]) }));

const root = '/api/projects/project-1/mail/zoho';
const initial = { id: 'zoho-1', label: 'Zoho Mail', status: 'needs_credentials', enabled: false,
  credential_present: false, mail_account_id: null, from_address: null, last_error_code: null,
  last_probe_at: null, version: 1, created_at: '2026-10-13T12:00:00Z' };
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
}
afterEach(() => vi.unstubAllGlobals());

it('does not display a previous project connection when an in-flight save completes', async () => {
  let finish: (response: Response) => void = () => {};
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'POST') return pending;
    return json([]);
  }));
  const user = userEvent.setup();
  const view = render(<ZohoMail projectId="project-1" />);
  await screen.findByText('No Zoho connection configured.');
  await user.click(screen.getByRole('button', { name: 'Save Zoho setup' }));
  view.rerender(<ZohoMail projectId="project-2" />);
  await screen.findByText('No Zoho connection configured.');
  await act(async () => { finish(json({ ...initial, label: 'Old account' }, 201)); });
  expect(screen.queryByText('Old account')).not.toBeInTheDocument();
  expect(screen.getByText('No Zoho connection configured.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Save Zoho setup' })).toBeEnabled();
});

it('saves setup without credentials or provider access and keeps use disabled', async () => {
  const posts: unknown[] = [];
  let saved = false;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `${root}/connections` && init?.method === 'POST') {
      posts.push(JSON.parse(init.body as string)); saved = true; return json(initial, 201);
    }
    if (url === `${root}/connections`) return json(saved ? [initial] : []);
    throw new Error(`Unexpected request ${url}`);
  }));
  const user = userEvent.setup();
  render(<ZohoMail projectId="project-1" />);
  expect(await screen.findByText('No Zoho connection configured.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Save Zoho setup' }));
  expect(await screen.findByText(/Credentials needed · Access off/)).toBeInTheDocument();
  expect(posts).toEqual([{ label: 'Zoho Mail' }]);
  expect(screen.getByRole('button', { name: 'Check Zoho connection' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Turn access on' })).toBeDisabled();
  expect(screen.queryByRole('button', { name: 'Request mail approval' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Advanced connection settings' }));
  expect(screen.getByLabelText('Private Zoho MCP URL')).toHaveAttribute('type', 'password');
  expect(screen.getByLabelText('Zoho MCP OAuth access token (optional, manual setup)')).toHaveAttribute('type', 'password');
});

it('clears credential inputs and saves drafts as approval proposals rather than sends', async () => {
  let row = { ...initial, status: 'ready', credential_present: true, enabled: true, from_address: 'owner@example.test' };
  const proposals: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `${root}/connections`) return json([row]);
    if (url.endsWith('/runs')) return json([{ id: 'run-1', mode: 'manual_planning', status: 'running', work_order_title: 'Reply' }]);
    if (url.endsWith('/credentials')) { row = { ...row, status: 'unverified', enabled: false }; return json(row); }
    if (url === `${root}/actions` && init?.method === 'POST') {
      proposals.push(JSON.parse(init.body as string)); return json({ state: 'pending', action_id: 'action-1' }, 201);
    }
    throw new Error(`Unexpected request ${url}`);
  }));
  const user = userEvent.setup();
  render(<ZohoMail projectId="project-1" />);
  await screen.findByRole('button', { name: 'Request mail approval' });
  await user.selectOptions(screen.getByLabelText('Zoho connection'), 'zoho-1');
  await screen.findByRole('option', { name: 'Reply' });
  await user.selectOptions(await screen.findByLabelText('Running manual work order'), 'run-1');
  await user.type(screen.getByLabelText('To'), 'buyer@example.test');
  await user.type(screen.getByLabelText('Subject'), 'Hello');
  await user.type(screen.getByLabelText('Body'), 'A draft.');
  await user.click(screen.getByRole('button', { name: 'Request mail approval' }));
  expect(await screen.findByText(/waiting in Approvals/)).toBeInTheDocument();
  expect(proposals).toEqual([{ connection_id: 'zoho-1', run_id: 'run-1', operation: 'draft',
    to_address: 'buyer@example.test', subject: 'Hello', body: 'A draft.' }]);
  await user.click(screen.getByRole('button', { name: 'Advanced connection settings' }));
  await user.type(screen.getByLabelText('Private Zoho MCP URL'), 'https://mail-fixture.zohomcp.com/private');
  await user.type(screen.getByLabelText('Zoho MCP OAuth access token (optional, manual setup)'), 'synthetic-value');
  await user.click(screen.getByRole('button', { name: 'Save credentials' }));
  await screen.findByText(/Credentials saved securely/);
  expect(screen.queryByLabelText('Zoho MCP OAuth access token (optional, manual setup)')).not.toBeInTheDocument();
  expect(screen.queryByText('synthetic-value')).not.toBeInTheDocument();
});


it('offers normal Zoho sign-in without asking the operator to copy a token', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json([{ ...initial, endpoint_present: true }])));
  render(<ZohoMail projectId="project-1" />);
  await screen.findByText(/Credentials needed · Access off/);
  expect(screen.getByRole('button', { name: 'Connect to Zoho' })).toBeEnabled();
  expect(screen.queryByLabelText('Zoho MCP OAuth access token (optional, manual setup)')).not.toBeInTheDocument();
});

it('starts sign-in with the selected version and navigates through its local authorize route', async () => {
  const authorize = vi.fn();
  const posts: unknown[] = [];
  const path = `${root}/connections/zoho-1/oauth/authorize`;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/sign-in')) {
      posts.push(JSON.parse(init?.body as string));
      return json({ authorize_path: path });
    }
    return json([{ ...initial, endpoint_present: true }]);
  }));
  render(<ZohoMail projectId="project-1" onAuthorize={authorize} />);
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Connect to Zoho' }));
  expect(posts).toEqual([{ expected_version: 1 }]);
  expect(authorize).toHaveBeenCalledExactlyOnceWith(path);
});

it('rejects an unexpected sign-in destination', async () => {
  const authorize = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url.endsWith('/sign-in')
    ? json({ authorize_path: 'https://foreign.example/authorize' })
    : json([{ ...initial, endpoint_present: true }])));
  render(<ZohoMail projectId="project-1" onAuthorize={authorize} />);
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Connect to Zoho' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not confirm the Zoho sign-in page');
  expect(authorize).not.toHaveBeenCalled();
});

it('does not navigate to an old project when a delayed sign-in request completes', async () => {
  const authorize = vi.fn();
  let finish: (response: Response) => void = () => {};
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.endsWith('/sign-in')) return pending;
    return json(url.includes('project-2') ? [] : [{ ...initial, endpoint_present: true }]);
  }));
  const view = render(<ZohoMail projectId="project-1" onAuthorize={authorize} />);
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Connect to Zoho' }));
  view.rerender(<ZohoMail projectId="project-2" onAuthorize={authorize} />);
  await screen.findByText('No Zoho connection configured.');
  await act(async () => { finish(json({ authorize_path: `${root}/connections/zoho-1/oauth/authorize` })); });
  expect(authorize).not.toHaveBeenCalled();
});
