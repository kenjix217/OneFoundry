import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ConnectedAccounts } from '../src/ConnectedAccounts';
import { MailInboxes } from '../src/MailInboxes';
import type { Connector } from '../src/connectorApi';

const path = '/api/projects/project-1/connectors';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('requires explicit confirmation before signing out all Microsoft apps and browsers', async () => {
  let item = { id: 'c-1', provider: 'microsoft', label: 'Work 365', client_id: 'client', tenant: 'common',
    features: ['mail', 'sign_out_everywhere'], status: 'connected', version: 2,
    last_error_code: null as string | null, granted_scopes: 'Mail.Read User.RevokeSessions.All' };
  const submitted: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === path) return json([item]);
    if (url === `${path}/c-1/sign-out-everywhere`) {
      submitted.push(JSON.parse(init?.body as string));
      item = { ...item, status: 'failed', version: 3, last_error_code: 'signed_out_everywhere' };
      return json({ state: 'completed' });
    }
    throw new Error(`Unexpected ${url}`);
  }));
  const user = userEvent.setup();
  render(<ConnectedAccounts projectId="project-1" />);
  await user.click(await screen.findByRole('button', { name: 'Sign out everywhere' }));
  expect(submitted).toHaveLength(0);
  expect(screen.getByText(/including apps outside OneFoundry/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(submitted).toHaveLength(0);
  await user.click(screen.getByRole('button', { name: 'Sign out everywhere' }));
  await user.click(screen.getByRole('button', { name: 'Confirm sign out of all apps and browsers' }));
  expect(await screen.findByRole('status')).toHaveTextContent('Microsoft accepted signing out all apps and browsers');
  expect(submitted).toEqual([expect.objectContaining({ expected_version: 2, request_id: expect.any(String),
    confirmation: 'sign_out_all_apps_and_browsers' })]);
});

it('adds a Microsoft account, shows the device code, and connects after checking', async () => {
  let account: Record<string, unknown> | null = null;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === path && init?.method === 'POST') {
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      expect(body.client_secret).toBeUndefined();
      account = { id: 'c-1', provider: 'microsoft', label: body.label, client_id: body.client_id, tenant: body.tenant,
        features: body.features, status: 'pending', last_error_code: null, granted_scopes: '', connected_at: null, version: 1,
        created_at: '2026-10-01T00:00:00Z' };
      return json(account, 201);
    }
    if (url === path) return json(account ? [account] : []);
    if (url === `${path}/c-1/sign-in`) return json({ kind: 'device', user_code: 'WXYZ-1234', verification_uri: 'https://microsoft.com/devicelogin', expires_at: 0, interval: 5 });
    if (url === `${path}/c-1/poll`) { account = { ...account, status: 'connected', version: 2 }; return json(account); }
    throw new Error(`Unexpected ${url}`);
  }));
  const user = userEvent.setup();
  render(<ConnectedAccounts projectId="project-1" />);
  expect(await screen.findByText('No accounts connected.')).toBeInTheDocument();
  await user.type(screen.getByLabelText('Name'), 'Work 365');
  await user.type(screen.getByLabelText('Client ID'), '11111111-aaaa');
  await user.click(screen.getByRole('button', { name: 'Save account' }));
  expect(await screen.findByText(/Saved “Work 365”/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Sign in' }));
  expect(await screen.findByText('WXYZ-1234')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Check sign-in' }));
  expect(await screen.findByText('Account connected.')).toBeInTheDocument();
});

it('offers account-based mailboxes only for connected accounts with mail access', async () => {
  const connectors: Connector[] = [{ id: 'c-1', provider: 'microsoft', label: 'Work 365', client_id: 'x', tenant: 'common',
    features: ['mail'], status: 'connected', last_error_code: null, granted_scopes: 'Mail.Read', version: 2 }];
  const posted: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/projects/project-1/mail/accounts/api') {
      posted.push(JSON.parse(init?.body as string));
      return json({ id: 'm-1', label: 'Work inbox', protocol: 'graph', host: null, port: null, username: null, folder: 'INBOX',
        enabled: false, status: 'unverified', last_error_code: null, capabilities: [], last_probe_at: null, last_poll_at: null,
        version: 1, created_at: '2026-10-01T00:00:00Z', credential_present: true, connector_account_id: 'c-1' }, 201);
    }
    if (url === '/api/projects/project-1/mail/accounts') return json([]);
    throw new Error(`Unexpected ${url}`);
  }));
  const user = userEvent.setup();
  render(<MailInboxes projectId="project-1" connectors={connectors} />);
  await user.selectOptions(await screen.findByLabelText('Connection type'), 'gmail');
  expect(screen.getByText(/Connect a Google account with mail access above first/)).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('Connection type'), 'graph');
  await user.type(screen.getByLabelText('Name'), 'Work inbox');
  await user.selectOptions(screen.getByLabelText('Account'), 'c-1');
  await user.click(screen.getByRole('button', { name: 'Save mailbox' }));
  expect(await screen.findByText(/Saved “Work inbox”/)).toBeInTheDocument();
  expect(posted[0]).toEqual({ label: 'Work inbox', protocol: 'graph', connector_account_id: 'c-1' });
});
