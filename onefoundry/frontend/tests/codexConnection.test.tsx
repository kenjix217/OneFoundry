import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { ModelsView } from '../src/ModelsView';
import { listModelAccounts } from '../src/modelApi';

afterEach(() => vi.unstubAllGlobals());
const provider = { id: 'openai_codex_runtime', name: 'OpenAI Codex Runtime', auth_kind: 'account_runtime',
  catalog_supported: true, text_runtime_available: true, connectable: true };
const account = { id: 'codex-1', project_id: 'project-1', provider: provider.id, label: 'Personal',
  auth_kind: 'account_runtime', endpoint: null, credential_present: true, catalog_valid: false,
  model_count: 1, last_refreshed_at: null, connection_status: 'expired' };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});

it('shows expired credentials and reconnects the same account with explicit completion', async () => {
  let status = 'expired';
  let finishCalls = 0;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    const saved = { ...account, connection_status: status, credential_present: status !== 'pending' };
    if (path.endsWith('/model/providers')) return json([provider]);
    if (path.endsWith('/model/accounts')) return json([saved]);
    if (path.endsWith('/model/catalog')) return json([]);
    if (path.endsWith('/codex/reconnect') && init?.method === 'POST') {
      status = 'pending';
      return json({ account: { ...saved, credential_present: false, connection_status: status },
        verification_url: 'https://auth.openai.com/codex/device', user_code: 'RECONNECT-CODE',
        expires_at: Math.floor(Date.now() / 1000) + 600, interval_seconds: 600 });
    }
    if (path.endsWith('/codex/finish') && init?.method === 'POST') {
      finishCalls += 1;
      if (finishCalls === 1) return json({ detail: 'Complete Codex sign-in, then check again' }, 409);
      status = 'connected';
      return json({ ...saved, credential_present: true, connection_status: status });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);
  const card = (await screen.findByRole('heading', { name: 'Personal' })).closest('article')!;
  expect(within(card).getByText('Reconnect required')).toBeInTheDocument();
  expect(within(card).getByRole('alert')).toHaveTextContent('Codex login expired');
  expect(within(card).queryByText('Device signed in')).not.toBeInTheDocument();
  await user.click(within(card).getByRole('button', { name: 'Reconnect account' }));
  expect(await within(card).findByText('RECONNECT-CODE')).toBeInTheDocument();
  await waitFor(() => expect(finishCalls).toBe(1));
  await user.click(within(card).getByRole('button', { name: 'Complete login' }));
  await waitFor(() => expect(within(card).getByText('Connected')).toBeInTheDocument());
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/codex/reconnect'))).toHaveLength(1);
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/codex/start'))).toBe(false);
  expect(fetcher.mock.calls.some(([path]) => path.includes('RECONNECT-CODE'))).toBe(false);
});

it('refreshes status without starting a login or making a model request', async () => {
  let reads = 0;
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith('/model/providers')) return json([provider]);
    if (path.endsWith('/model/accounts')) { reads += 1; return json([{ ...account, connection_status: reads > 1 ? 'expired' : 'connected' }]); }
    if (path.endsWith('/model/catalog')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);
  const card = (await screen.findByRole('heading', { name: 'Personal' })).closest('article')!;
  await user.click(within(card).getByRole('button', { name: 'Refresh status' }));
  expect(await within(card).findByText('Reconnect required')).toBeInTheDocument();
  expect(reads).toBe(2);
});

it.each([['connected'], {}, 'invalid'])('rejects malformed account status %j', async (connection_status) => {
  vi.stubGlobal('fetch', vi.fn(async () => json([{ ...account, connection_status }])));
  await expect(listModelAccounts('project-1')).rejects.toThrow('Invalid model connection status.');
});
