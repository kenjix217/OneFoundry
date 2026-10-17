import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { MailOutgoing } from '../src/MailOutgoing';

vi.mock('../src/workApi', () => ({
  listRuns: vi.fn(async () => [{ id: 'run-1', status: 'running', mode: 'manual_planning', work_order_title: 'Reply to buyer' }]),
}));

const root = '/api/projects/project-1/mail/outgoing';
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('keeps SMTP setup off until checked and sends only a proposal for approval', async () => {
  let account: Record<string, unknown> | null = null;
  const posts: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `${root}/accounts` && init?.method === 'POST') {
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      posts.push(body);
      account = { id: 'smtp-1', label: body.label, host: body.host, port: body.port, security: body.security,
        username: body.username, from_address: body.from_address, status: 'unverified', enabled: false,
        last_error_code: null, last_probe_at: null, version: 1, created_at: '2026-10-03T12:00:00Z', credential_present: true };
      return json(account, 201);
    }
    if (url === `${root}/accounts`) return json(account ? [account] : []);
    if (url === `${root}/accounts/smtp-1/probe`) {
      account = { ...account, status: 'ready', version: 2, last_probe_at: '2026-10-03T12:01:00Z' };
      return json(account);
    }
    if (url === `${root}/accounts/smtp-1/enabled`) { account = { ...account, enabled: true, version: 3 }; return json(account); }
    if (url === `${root}/emails` && init?.method === 'POST') {
      posts.push(JSON.parse(init.body as string) as Record<string, unknown>);
      return json({ email_id: 'email-1', approval_id: 'approval-1', state: 'pending' }, 201);
    }
    if (url === `${root}/emails`) return json([]);
    throw new Error(`Unexpected ${url}`);
  }));
  const user = userEvent.setup();
  render(<MailOutgoing projectId="project-1" />);
  expect(await screen.findByText('No outgoing account connected.')).toBeInTheDocument();
  await user.type(screen.getByLabelText('Label'), 'Company');
  await user.type(screen.getByLabelText('SMTP server'), 'smtp.example.test');
  await user.type(screen.getByLabelText('Username'), 'owner@example.test');
  await user.type(screen.getByLabelText('From address'), 'owner@example.test');
  await user.type(screen.getByLabelText('SMTP or app password'), 'fixture-pass');
  await user.click(screen.getByRole('button', { name: 'Save SMTP account' }));
  expect(await screen.findByText(/Saved Company/)).toBeInTheDocument();
  expect(posts[0]).toMatchObject({ host: 'smtp.example.test', password: 'fixture-pass' });
  expect(screen.queryByText('fixture-pass')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Turn on' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Check connection' }));
  expect(await screen.findByText(/SMTP login works/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Turn on' }));
  expect(await screen.findByText(/Each email still needs approval/)).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('Outgoing account'), 'smtp-1');
  await user.selectOptions(screen.getByLabelText('Running manual work order'), 'run-1');
  await user.type(screen.getByLabelText('To'), 'buyer@example.test');
  await user.type(screen.getByLabelText('Subject'), 'Your quote');
  await user.type(screen.getByLabelText('Body'), 'Here is the draft.');
  await user.click(screen.getByRole('button', { name: 'Request email approval' }));
  expect(await screen.findByText(/waiting in Approvals/)).toBeInTheDocument();
  expect(posts[1]).toMatchObject({ account_id: 'smtp-1', run_id: 'run-1', to_address: 'buyer@example.test',
    subject: 'Your quote', body: 'Here is the draft.' });
});
