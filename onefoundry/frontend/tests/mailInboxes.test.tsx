import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { MailInboxes } from '../src/MailInboxes';

const path = '/api/projects/project-1/mail/accounts';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('saves a Zoho mailbox without echoing the password, checks it, and turns it on', async () => {
  let account: Record<string, unknown> | null = null;
  const posted: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === path && init?.method === 'POST') {
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      posted.push(body);
      account = { id: 'mail-1', label: body.label, protocol: 'imap', host: body.host, port: body.port, username: body.username,
        folder: body.folder, enabled: false, status: 'unverified', last_error_code: null, capabilities: [], last_probe_at: null,
        last_poll_at: null, version: 1, created_at: '2026-09-30T12:00:00Z', credential_present: true };
      return json(account, 201);
    }
    if (url === path) return json(account ? [account] : []);
    if (url === `${path}/mail-1/probe`) {
      account = { ...account, status: 'ready', capabilities: ['IMAP4REV1', 'IDLE'], last_probe_at: '2026-09-30T12:01:00Z', version: 2 };
      return json(account);
    }
    if (url === `${path}/mail-1/enabled`) { account = { ...account, enabled: true, version: 3 }; return json(account); }
    throw new Error(`Unexpected ${url}`);
  }));
  const user = userEvent.setup();
  render(<MailInboxes projectId="project-1" />);
  expect(await screen.findByText('No mailboxes connected.')).toBeInTheDocument();
  expect(screen.getByText(/Zoho Mail: server imap\.zoho\.com/)).toBeInTheDocument();
  await user.type(screen.getByLabelText('Name'), 'Sales inbox');
  await user.type(screen.getByLabelText('IMAP server'), 'imap.zoho.com');
  await user.type(screen.getByLabelText('Username'), 'operator@example.test');
  await user.type(screen.getByLabelText(/^App password/), 'app-specific');
  await user.click(screen.getByRole('button', { name: 'Save mailbox' }));
  expect(await screen.findByText(/Saved “Sales inbox”/)).toBeInTheDocument();
  expect(posted[0]).toMatchObject({ host: 'imap.zoho.com', port: 993, folder: 'INBOX', password: 'app-specific' });
  expect(screen.queryByText('app-specific')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Turn on' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Check connection' }));
  expect(await screen.findByText(/Connection works/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Turn on' }));
  expect(await screen.findByText('Mailbox turned on.')).toBeInTheDocument();
  expect(screen.getByText(/^On ·/)).toBeInTheDocument();
});
