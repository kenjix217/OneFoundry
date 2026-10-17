import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ModelsView } from '../src/ModelsView';
import { ProfileEditor } from '../src/ProfileEditor';

const chief = { id: 'chief-1', project_id: 'project-1', kind: 'builtin' as const, name: 'Chief of Staff',
  reports_to_profile_id: null, model: null, instructions: '', memory_scope: 'project' as const,
  allowed_tools: [], autonomy_level: 'manual' as const, configured: false, permissions_active: false };

const account = { id: 'account-1', project_id: 'project-1', provider: 'openai', label: 'Company API',
  auth_kind: 'api_key', endpoint: null, credential_present: true, catalog_valid: true, model_count: 2, last_refreshed_at: null };
const providers = [
  { id: 'openai', name: 'OpenAI', auth_kind: 'api_key', catalog_supported: true, text_runtime_available: true, connectable: true },
  { id: 'grok', name: 'xAI Grok', auth_kind: 'api_key', catalog_supported: true, text_runtime_available: false, connectable: true },
  { id: 'ollama', name: 'Local Ollama', auth_kind: 'none', catalog_supported: true, text_runtime_available: false, connectable: true },
  { id: 'bedrock', name: 'Amazon Bedrock', auth_kind: 'api_key', catalog_supported: false, text_runtime_available: false, connectable: false },
];
const models = [
  { id: 'model-1', account_id: account.id, provider: 'openai', model_id: 'text-a', display_name: 'Text A', category: 'text', available: true },
  { id: 'model-2', account_id: account.id, provider: 'openai', model_id: 'image-a', display_name: 'Image A', category: 'image', available: false },
];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('shows provider/category groups and calls discovery only after a click', async () => {
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json(providers);
    if (path.endsWith('/model/accounts') && !init?.method) return json([account]);
    if (path.endsWith('/model/catalog')) return json(models);
    if (path.endsWith('/model/accounts/account-1/refresh')) return json({ account, models });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);

  expect(await screen.findByRole('heading', { name: 'Browse models' })).toBeInTheDocument();
  expect(screen.getByRole('option', { name: 'Amazon Bedrock · adapter unavailable' })).toBeDisabled();
  const browser = screen.getByRole('heading', { name: 'Browse models' }).closest('section')!;
  expect(within(browser).getByRole('heading', { name: 'OpenAI' })).toBeInTheDocument();
  expect(within(browser).getByRole('heading', { name: 'text' })).toBeInTheDocument();
  expect(within(browser).getByRole('heading', { name: 'image' })).toBeInTheDocument();
  expect(within(browser).getByText('Text A')).toBeInTheDocument();
  expect(within(browser).getByText('OneFoundry text tasks')).toBeInTheDocument();
  expect(screen.getByText('Text task path implemented · live generation unverified')).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Subscription runtimes' })).not.toBeInTheDocument();
  expect(within(browser).getByText('Image A')).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/refresh'))).toBe(false);
  await user.click(screen.getByRole('button', { name: 'Discover models' }));
  await waitFor(() => expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/refresh'))).toHaveLength(1));
});

it('sends an API key only in the request body and clears the password input', async () => {
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json(providers);
    if (path.endsWith('/model/accounts') && init?.method === 'POST') return json(account, 201);
    if (path.endsWith('/model/accounts')) return json([]);
    if (path.endsWith('/model/catalog')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);
  await screen.findByText(/No model connections yet/);
  await user.type(screen.getByLabelText('Account label'), 'Company API');
  await user.type(screen.getByLabelText('API key'), 'example-secret');
  await user.click(screen.getByRole('button', { name: 'Add connection' }));
  await waitFor(() => expect(screen.getByLabelText('API key')).toHaveValue(''));
  const post = fetcher.mock.calls.find(([, init]) => init?.method === 'POST')!;
  expect(post[0]).toBe('/api/projects/project-1/model/accounts');
  expect(JSON.parse(post[1]?.body as string)).toEqual({ provider: 'openai', label: 'Company API', api_key: 'example-secret' });
  expect(fetcher.mock.calls.every(([path]) => !path.includes('example-secret'))).toBe(true);
});

it('creates a keyless local connection from server descriptors', async () => {
  const local = { ...account, id: 'local-1', provider: 'ollama', label: 'Local', auth_kind: 'none',
    endpoint: 'http://ollama:11434', credential_present: false, model_count: 0 };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json(providers);
    if (path.endsWith('/model/accounts') && init?.method === 'POST') return json(local, 201);
    if (path.endsWith('/model/accounts')) return json([]);
    if (path.endsWith('/model/catalog')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);
  await screen.findByRole('option', { name: 'Local Ollama' });
  await user.selectOptions(screen.getByLabelText('Provider'), 'ollama');
  await user.type(screen.getByLabelText('Account label'), 'Local');
  expect(screen.queryByLabelText('API key')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Add connection' }));
  const post = fetcher.mock.calls.find(([, init]) => init?.method === 'POST')!;
  expect(JSON.parse(post[1]?.body as string)).toEqual({
    provider: 'ollama', label: 'Local', endpoint: 'http://ollama:11434',
  });
});

it('offers separate Codex and Grok device login without an API-key field', async () => {
  const accountRuntimeProviders = [
    ...providers,
    { id: 'openai_codex_runtime', name: 'OpenAI Codex Runtime', auth_kind: 'account_runtime',
      catalog_supported: true, text_runtime_available: true, connectable: true },
    { id: 'xai', name: 'Grok subscription', auth_kind: 'account_runtime',
      catalog_supported: true, text_runtime_available: true, connectable: true },
  ];
  const pending = { ...account, id: 'grok-1', provider: 'xai', label: 'Grok account',
    auth_kind: 'account_runtime', credential_present: false, catalog_valid: false, model_count: 0 };
  let started = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json(accountRuntimeProviders);
    if (path.endsWith('/model/accounts')) return json(started ? [pending] : []);
    if (path.endsWith('/model/catalog')) return json([]);
    if (path.endsWith('/model/grok/start') && init?.method === 'POST') {
      started = true;
      return json({ account: pending, verification_url: 'https://auth.x.ai/device', user_code: 'GROK-123',
        expires_at: Math.floor(Date.now() / 1000) + 300, interval_seconds: 5 }, 201);
    }
    if (path.endsWith('/model/accounts/grok-1/grok/finish') && init?.method === 'POST') {
      return json({ detail: 'Complete Grok sign-in, then check again' }, 409);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);
  await screen.findByRole('option', { name: 'OpenAI Codex Runtime' });
  await user.selectOptions(screen.getByLabelText('Provider'), 'openai_codex_runtime');
  expect(screen.getByRole('button', { name: 'Connect Codex account' })).toBeInTheDocument();
  expect(screen.queryByLabelText('API key')).not.toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('Provider'), 'xai');
  await user.type(screen.getByLabelText('Account label'), 'Grok account');
  await user.click(screen.getByRole('button', { name: 'Connect Grok account' }));
  expect(await screen.findByText('GROK-123')).toBeInTheDocument();
  const post = fetcher.mock.calls.find(([path]) => path.endsWith('/model/grok/start'))!;
  expect(JSON.parse(post[1]?.body as string)).toEqual({ label: 'Grok account' });
  expect(fetcher.mock.calls.every(([path]) => !path.includes('GROK-123'))).toBe(true);
  expect(screen.queryByRole('button', { name: 'Finish connection' })).not.toBeInTheDocument();
});

it('resumes pending Grok login and polls at the provider interval until connected', async () => {
  const grokProvider = { id: 'xai', name: 'Grok subscription', auth_kind: 'account_runtime',
    catalog_supported: true, text_runtime_available: true, connectable: true };
  const pending = { ...account, id: 'grok-1', provider: 'xai', label: 'Personal Grok',
    auth_kind: 'account_runtime', credential_present: false, catalog_valid: false, model_count: 0 };
  let connected = false;
  let finishes = 0;
  let challengeReads = 0;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json([grokProvider]);
    if (path.endsWith('/model/accounts')) return json([{ ...pending, credential_present: connected }]);
    if (path.endsWith('/model/catalog')) return json([]);
    if (path.endsWith('/model/accounts/grok-1/grok/challenge')) {
      challengeReads += 1;
      return json({ account: pending, verification_url: 'https://auth.x.ai/device', user_code: 'GROK-CODE',
        expires_at: Math.floor(Date.now() / 1000) + 300, interval_seconds: challengeReads === 1 ? 5 : 1 });
    }
    if (path.endsWith('/model/accounts/grok-1/grok/finish') && init?.method === 'POST') {
      finishes += 1;
      if (finishes === 1) return json({ detail: 'Complete Grok sign-in, then check again' }, 409);
      connected = true;
      return json({ ...pending, credential_present: true });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<ModelsView projectId="project-1" />);
  expect(await screen.findByText('GROK-CODE')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Finish connection' })).not.toBeInTheDocument();
  await waitFor(() => expect(finishes).toBe(2), { timeout: 2500 });
  expect(challengeReads).toBeGreaterThanOrEqual(2);
  expect(await screen.findByText('Connected')).toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('Grok account sign-in saved');
});

it('shows the Grok provider start failure without claiming a saved account', async () => {
  const grokProvider = { id: 'xai', name: 'Grok subscription', auth_kind: 'account_runtime',
    catalog_supported: true, text_runtime_available: true, connectable: true };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json([grokProvider]);
    if (path.endsWith('/model/accounts')) return json([]);
    if (path.endsWith('/model/catalog')) return json([]);
    if (path.endsWith('/model/grok/start') && init?.method === 'POST') {
      return json({ detail: 'Grok device login endpoint rejected the request (HTTP 403); no account saved' }, 502);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);
  await screen.findByRole('button', { name: 'Connect Grok account' });
  await user.type(screen.getByLabelText('Account label'), 'Personal Grok');
  await user.click(screen.getByRole('button', { name: 'Connect Grok account' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Grok device login endpoint rejected the request (HTTP 403)');
  expect(screen.getByText(/No model connections yet/)).toBeInTheDocument();
});

it('finalizes saved Grok tokens after a reload when account metadata is still pending', async () => {
  const grokProvider = { id: 'xai', name: 'Grok subscription', auth_kind: 'account_runtime',
    catalog_supported: true, text_runtime_available: true, connectable: true };
  const pending = { ...account, id: 'grok-1', provider: 'xai', label: 'Personal Grok',
    auth_kind: 'account_runtime', credential_present: false, catalog_valid: false, model_count: 0 };
  let connected = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json([grokProvider]);
    if (path.endsWith('/model/accounts')) return json([{ ...pending, credential_present: connected }]);
    if (path.endsWith('/model/catalog')) return json([]);
    if (path.endsWith('/model/accounts/grok-1/grok/challenge')) {
      return json({ detail: 'Grok account has saved tokens; complete the saved connection' }, 409);
    }
    if (path.endsWith('/model/accounts/grok-1/grok/finish') && init?.method === 'POST') {
      connected = true;
      return json({ ...pending, credential_present: true });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<ModelsView projectId="project-1" />);
  expect(await screen.findByText('Connected')).toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('Grok account sign-in saved');
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/grok/finish'))).toHaveLength(1);
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/grok/restart'))).toHaveLength(0);
});

it('offers an explicit retry when saved Grok token finalization fails', async () => {
  const grokProvider = { id: 'xai', name: 'Grok subscription', auth_kind: 'account_runtime',
    catalog_supported: true, text_runtime_available: true, connectable: true };
  const pending = { ...account, id: 'grok-1', provider: 'xai', label: 'Personal Grok',
    auth_kind: 'account_runtime', credential_present: false, catalog_valid: false, model_count: 0 };
  let finishAttempts = 0;
  let connected = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json([grokProvider]);
    if (path.endsWith('/model/accounts')) return json([{ ...pending, credential_present: connected }]);
    if (path.endsWith('/model/catalog')) return json([]);
    if (path.endsWith('/model/accounts/grok-1/grok/challenge')) {
      return json({ detail: 'Grok account has saved tokens; complete the saved connection' }, 409);
    }
    if (path.endsWith('/model/accounts/grok-1/grok/finish') && init?.method === 'POST') {
      finishAttempts += 1;
      if (finishAttempts === 1) return json({ detail: 'model account store unavailable' }, 503);
      connected = true;
      return json({ ...pending, credential_present: true });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);
  expect(await screen.findByRole('alert')).toHaveTextContent('model account store unavailable');
  expect(finishAttempts).toBe(1);
  await user.click(screen.getByRole('button', { name: 'Complete saved sign-in' }));
  expect(await screen.findByText('Connected')).toBeInTheDocument();
  expect(finishAttempts).toBe(2);
});

it('resumes a later Grok challenge when the first pending account expired', async () => {
  const grokProvider = { id: 'xai', name: 'Grok subscription', auth_kind: 'account_runtime',
    catalog_supported: true, text_runtime_available: true, connectable: true };
  const first = { ...account, id: 'grok-first', provider: 'xai', label: 'Old Grok',
    auth_kind: 'account_runtime', credential_present: false, catalog_valid: false, model_count: 0 };
  const second = { ...first, id: 'grok-second', label: 'Current Grok' };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json([grokProvider]);
    if (path.endsWith('/model/accounts')) return json([first, second]);
    if (path.endsWith('/model/catalog')) return json([]);
    if (path.endsWith('/model/accounts/grok-first/grok/challenge')) {
      return json({ detail: 'Grok sign-in expired; start fresh sign-in on this account' }, 409);
    }
    if (path.endsWith('/model/accounts/grok-first/grok/finish') && init?.method === 'POST') {
      return json({ detail: 'Grok sign-in expired; start fresh sign-in on this account' }, 409);
    }
    if (path.endsWith('/model/accounts/grok-second/grok/challenge')) {
      return json({ account: second, verification_url: 'https://auth.x.ai/device', user_code: 'CURRENT-CODE',
        expires_at: Math.floor(Date.now() / 1000) + 300, interval_seconds: 5 });
    }
    if (path.endsWith('/model/accounts/grok-second/grok/finish') && init?.method === 'POST') {
      return json({ detail: 'Complete Grok sign-in, then check again' }, 409);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<ModelsView projectId="project-1" />);
  expect(await screen.findByText('CURRENT-CODE')).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/grok-first/grok/finish'))).toHaveLength(0);
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/grok-second/grok/challenge')).length).toBeGreaterThanOrEqual(1);
});

it('restarts an expired Grok login on its existing account and shows terminal exchange errors', async () => {
  const grokProvider = { id: 'xai', name: 'Grok subscription', auth_kind: 'account_runtime',
    catalog_supported: true, text_runtime_available: true, connectable: true };
  const pending = { ...account, id: 'grok-1', provider: 'xai', label: 'Personal Grok',
    auth_kind: 'account_runtime', credential_present: false, catalog_valid: false, model_count: 0 };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json([grokProvider]);
    if (path.endsWith('/model/accounts')) return json([pending]);
    if (path.endsWith('/model/catalog')) return json([]);
    if (path.endsWith('/model/accounts/grok-1/grok/challenge')) {
      return json({ detail: 'Grok sign-in code expired; start fresh sign-in on this account' }, 409);
    }
    if (path.endsWith('/model/accounts/grok-1/grok/restart') && init?.method === 'POST') {
      return json({ account: pending, verification_url: 'https://auth.x.ai/device', user_code: 'FRESH-GROK',
        expires_at: Math.floor(Date.now() / 1000) + 300, interval_seconds: 1 });
    }
    if (path.endsWith('/model/accounts/grok-1/grok/finish') && init?.method === 'POST') {
      return json({ detail: 'xAI denied Grok sign-in; start fresh sign-in on this account' }, 409);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);
  const card = (await screen.findByRole('heading', { name: 'Personal Grok' })).closest('article')!;
  expect(await screen.findByRole('alert')).toHaveTextContent('Grok sign-in code expired');
  await user.click(within(card).getByRole('button', { name: 'Start fresh sign-in' }));
  expect(await within(card).findByText('FRESH-GROK')).toBeInTheDocument();
  expect(await screen.findByRole('alert')).toHaveTextContent('xAI denied Grok sign-in');
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/grok/restart'))).toHaveLength(1);
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/grok/start'))).toHaveLength(0);
});

it('signs in again on a connected Grok account without creating another account', async () => {
  const grokProvider = { id: 'xai', name: 'Grok subscription', auth_kind: 'account_runtime',
    catalog_supported: true, text_runtime_available: true, connectable: true };
  const connectedAccount = { ...account, id: 'grok-1', provider: 'xai', label: 'Personal Grok',
    auth_kind: 'account_runtime', credential_present: true, catalog_valid: true, model_count: 1 };
  let reconnecting = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json([grokProvider]);
    if (path.endsWith('/model/accounts')) return json([{ ...connectedAccount, credential_present: !reconnecting }]);
    if (path.endsWith('/model/catalog')) return json([]);
    if (path.endsWith('/model/accounts/grok-1/grok/reconnect') && init?.method === 'POST') {
      reconnecting = true;
      return json({ account: { ...connectedAccount, credential_present: false },
        verification_url: 'https://auth.x.ai/device', user_code: 'RECONNECT-CODE',
        expires_at: Math.floor(Date.now() / 1000) + 300, interval_seconds: 5 });
    }
    if (path.endsWith('/model/accounts/grok-1/grok/finish') && init?.method === 'POST') {
      return json({ detail: 'Complete Grok sign-in, then check again' }, 409);
    }
    if (path.endsWith('/model/accounts/grok-1/grok/challenge')) {
      return json({ account: { ...connectedAccount, credential_present: false },
        verification_url: 'https://auth.x.ai/device', user_code: 'RECONNECT-CODE',
        expires_at: Math.floor(Date.now() / 1000) + 300, interval_seconds: 5 });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);
  const card = (await screen.findByRole('heading', { name: 'Personal Grok' })).closest('article')!;
  await user.click(within(card).getByRole('button', { name: 'Sign in again' }));
  expect(await within(card).findByText('RECONNECT-CODE')).toBeInTheDocument();
  expect(within(card).getByText('Sign-in pending')).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/grok/reconnect'))).toHaveLength(1);
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/grok/start'))).toHaveLength(0);
});

it('shows a Codex sign-in endpoint failure without claiming a saved connection', async () => {
  const codexProvider = { id: 'openai_codex_runtime', name: 'OpenAI Codex Runtime',
    auth_kind: 'account_runtime', catalog_supported: true, text_runtime_available: true, connectable: true };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json([codexProvider]);
    if (path.endsWith('/model/accounts')) return json([]);
    if (path.endsWith('/model/catalog')) return json([]);
    if (path.endsWith('/model/codex/start') && init?.method === 'POST') {
      return json({ detail: 'OpenAI sign-in endpoint returned HTTP 530; no connection saved' }, 502);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);
  await screen.findByRole('button', { name: 'Connect Codex account' });
  await user.type(screen.getByLabelText('Account label'), 'Personal');
  await user.click(screen.getByRole('button', { name: 'Connect Codex account' }));

  expect(await screen.findByRole('alert')).toHaveTextContent('OpenAI sign-in endpoint returned HTTP 530; no connection saved');
  expect(screen.queryByText(/Could not save the model connection/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Finish connection/)).not.toBeInTheDocument();
  expect(screen.getByText(/No model connections yet/)).toBeInTheDocument();
  const post = fetcher.mock.calls.find(([path]) => path.endsWith('/model/codex/start'))!;
  expect(JSON.parse(post[1]?.body as string)).toEqual({ label: 'Personal' });
});

it('automatically finishes the Codex account whose sign-in code is displayed', async () => {
  const codexProvider = { id: 'openai_codex_runtime', name: 'OpenAI Codex Runtime',
    auth_kind: 'account_runtime', catalog_supported: true, text_runtime_available: true, connectable: true };
  const first = { ...account, id: 'first', provider: 'openai_codex_runtime', label: 'Personal',
    auth_kind: 'account_runtime', credential_present: false, catalog_valid: false, model_count: 0 };
  const second = { ...first, id: 'second', label: 'personal' };
  let connected = false;
  let releaseFinish: (() => void) | undefined;
  const finishGate = new Promise<void>((resolve) => { releaseFinish = resolve; });
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json([codexProvider]);
    if (path.endsWith('/model/accounts')) return json([first, { ...second, credential_present: connected }]);
    if (path.endsWith('/model/catalog')) return json([]);
    if (path.endsWith('/model/accounts/first/codex/challenge')) {
      return json({ detail: 'Codex sign-in code expired; start fresh sign-in on this account' }, 409);
    }
    if (path.endsWith('/model/accounts/second/codex/challenge')) {
      return json({ account: second, verification_url: 'https://auth.openai.com/codex/device',
        user_code: 'SECOND-CODE', expires_at: Math.floor(Date.now() / 1000) + 300, interval_seconds: 5 });
    }
    if (path.endsWith('/model/accounts/second/codex/finish') && init?.method === 'POST') {
      await finishGate;
      connected = true;
      return json({ ...second, credential_present: true });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);
  const firstCard = (await screen.findByRole('heading', { name: /^Personal$/ })).closest('article')!;
  const secondCard = screen.getByRole('heading', { name: /^personal$/ }).closest('article')!;

  await user.click(within(secondCard).getByRole('button', { name: 'Show sign-in code' }));
  expect(await within(secondCard).findByText('SECOND-CODE')).toBeInTheDocument();
  expect(within(firstCard).queryByRole('button', { name: 'Finish connection' })).not.toBeInTheDocument();
  expect(within(firstCard).queryByText('SECOND-CODE')).not.toBeInTheDocument();
  expect(within(secondCard).queryByRole('button', { name: 'Finish connection' })).not.toBeInTheDocument();
  releaseFinish?.();
  await waitFor(() => expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/codex/finish')))
    .toEqual([expect.arrayContaining(['/api/projects/project-1/model/accounts/second/codex/finish'])]));
  expect(await screen.findByText(/Codex device sign-in saved/)).toBeInTheDocument();
  expect(within(secondCard).getByText('Device signed in')).toBeInTheDocument();
  expect(within(secondCard).getByText(/do not confirm subscription inference access/)).toBeInTheDocument();
});

it('restarts an expired Codex sign-in without adding another account', async () => {
  const codexProvider = { id: 'openai_codex_runtime', name: 'OpenAI Codex Runtime',
    auth_kind: 'account_runtime', catalog_supported: true, text_runtime_available: true, connectable: true };
  const pending = { ...account, id: 'pending', provider: 'openai_codex_runtime', label: 'Personal',
    auth_kind: 'account_runtime', credential_present: false, catalog_valid: false, model_count: 0 };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json([codexProvider]);
    if (path.endsWith('/model/accounts')) return json([pending]);
    if (path.endsWith('/model/catalog')) return json([]);
    if (path.endsWith('/model/accounts/pending/codex/challenge')) {
      return json({ detail: 'Codex sign-in code expired; start fresh sign-in on this account' }, 409);
    }
    if (path.endsWith('/model/accounts/pending/codex/restart') && init?.method === 'POST') {
      return json({ account: pending, verification_url: 'https://auth.openai.com/codex/device',
        user_code: 'FRESH-CODE', expires_at: Math.floor(Date.now() / 1000) + 300, interval_seconds: 5 });
    }
    if (path.endsWith('/model/accounts/pending/codex/finish') && init?.method === 'POST') {
      return json({ detail: 'OpenAI token exchange did not return a response. Its outcome is unknown; start fresh sign-in on this account.' }, 502);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ModelsView projectId="project-1" />);
  const card = (await screen.findByRole('heading', { name: 'Personal' })).closest('article')!;
  await user.click(within(card).getByRole('button', { name: 'Show sign-in code' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Codex sign-in code expired');
  await user.click(within(card).getByRole('button', { name: 'Start fresh sign-in' }));
  expect(await within(card).findByText('FRESH-CODE')).toBeInTheDocument();
  expect(within(card).queryByRole('button', { name: 'Finish connection' })).not.toBeInTheDocument();
  await waitFor(() => expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/codex/finish'))).toHaveLength(1));
  expect(await screen.findByRole('alert')).toHaveTextContent('outcome is unknown');
  expect(screen.queryByText(/Fresh sign-in code is shown/)).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/codex/start'))).toHaveLength(0);
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/codex/restart'))).toHaveLength(1);
});

it('binds a discovered model and account to a profile without offering unavailable choices', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith('/model/accounts')) return json([account]);
    if (path.endsWith('/model/catalog')) return json(models);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const save = vi.fn(async () => {});
  const user = userEvent.setup();
  render(<ProfileEditor projectId="project-1" profile={null} profiles={[chief]} saving={false} error={null} onClose={() => {}} onSave={save} />);
  await user.type(screen.getByPlaceholderText('e.g. Product strategist'), 'Writer');
  await user.type(screen.getByPlaceholderText('Describe the role’s responsibilities and working style'), 'Write');
  const textChoice = await screen.findByRole('option', { name: 'Text A · text' });
  expect(textChoice).not.toBeDisabled();
  expect(screen.getByRole('option', { name: 'Image A · image · unavailable' })).toBeDisabled();
  await user.selectOptions(screen.getByLabelText('Model'), 'model-1');
  await user.click(screen.getByRole('button', { name: 'Save profile' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ model: 'text-a', model_account_id: 'account-1' }));
});

it('refreshes an account from the profile editor and keeps the selected model after catalog IDs change', async () => {
  const codex = { ...account, id: 'codex-account', provider: 'openai_codex_runtime', label: 'Personal',
    auth_kind: 'account_runtime' };
  let catalog = [{ id: 'old-id', account_id: codex.id, provider: codex.provider,
    model_id: 'gpt-6-sol', display_name: 'GPT-6 Sol', category: 'text', available: true,
    reasoning_efforts: ['medium'] }];
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/accounts')) return json([codex]);
    if (path.endsWith('/model/catalog')) return json(catalog);
    if (path.endsWith('/model/accounts/codex-account/refresh') && init?.method === 'POST') {
      catalog = [{ ...catalog[0], id: 'new-id' }, { id: 'gpt-6.1-id', account_id: codex.id,
        provider: codex.provider, model_id: 'gpt-6.1-sol', display_name: 'GPT-6.1 Sol',
        category: 'text', available: true, reasoning_efforts: ['medium', 'high'] }];
      return json({ account: { ...codex, model_count: 2 }, models: catalog });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const save = vi.fn(async () => {});
  const user = userEvent.setup();
  render(<ProfileEditor projectId="project-1" profile={null} profiles={[chief]} saving={false} error={null} onClose={() => {}} onSave={save} />);
  await user.type(screen.getByPlaceholderText('e.g. Product strategist'), 'Writer');
  await user.type(screen.getByPlaceholderText('Describe the role’s responsibilities and working style'), 'Write');
  await user.selectOptions(screen.getByLabelText('Model'), 'old-id');
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/refresh'))).toBe(false);
  await user.click(screen.getByRole('button', { name: 'Refresh Personal models' }));
  expect(await screen.findByRole('option', { name: 'GPT-6.1 Sol · text' })).toBeInTheDocument();
  expect(screen.getByLabelText('Model')).toHaveValue('new-id');
  await user.selectOptions(screen.getByLabelText('Model'), 'gpt-6.1-id');
  await user.click(screen.getByRole('button', { name: 'Save profile' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ model: 'gpt-6.1-sol', model_account_id: codex.id }));
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/refresh'))).toHaveLength(1);
});

it('keeps saved model choices visible when a profile-editor refresh fails', async () => {
  let refreshFailed = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/accounts')) return json([{ ...account, catalog_valid: !refreshFailed }]);
    if (path.endsWith('/model/catalog')) return json(models.map((item) => ({ ...item, available: !refreshFailed })));
    if (path.endsWith('/model/accounts/account-1/refresh') && init?.method === 'POST') {
      refreshFailed = true;
      return json({ detail: 'provider model discovery failed' }, 502);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ProfileEditor projectId="project-1" profile={null} profiles={[chief]} saving={false} error={null} onClose={() => {}} onSave={vi.fn()} />);
  expect(await screen.findByRole('option', { name: 'Text A · text' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Refresh Company API models' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('provider model discovery failed');
  expect(screen.getByRole('option', { name: 'Text A · text · unavailable' })).toBeDisabled();
});

it('clears an unsaved model choice when refresh no longer offers it', async () => {
  let catalog = models;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/accounts')) return json([account]);
    if (path.endsWith('/model/catalog')) return json(catalog);
    if (path.endsWith('/model/accounts/account-1/refresh') && init?.method === 'POST') {
      catalog = [];
      return json({ account: { ...account, model_count: 0 }, models: catalog });
    }
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<ProfileEditor projectId="project-1" profile={null} profiles={[chief]} saving={false} error={null} onClose={() => {}} onSave={vi.fn()} />);
  await user.selectOptions(await screen.findByLabelText('Model'), 'model-1');
  await user.click(screen.getByRole('button', { name: 'Refresh Company API models' }));
  expect(await screen.findByText(/previous selection cannot currently be used/)).toBeInTheDocument();
  expect(screen.getByLabelText('Model')).toHaveValue('');
});

it('saves only account-advertised Codex reasoning choices on a profile', async () => {
  const codex = { ...account, id: 'codex-account', provider: 'openai_codex_runtime', label: 'Personal', auth_kind: 'account_runtime' };
  const catalog = [{ id: 'codex-model', account_id: codex.id, provider: codex.provider,
    model_id: 'gpt-test', display_name: 'GPT Test', category: 'text', available: true,
    reasoning_efforts: ['low', 'high', 'xhigh'] }];
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/model/accounts')) return json([codex]);
    if (path.endsWith('/model/catalog')) return json(catalog);
    throw new Error(`Unexpected path ${path}`);
  }));
  const save = vi.fn(async () => {});
  const user = userEvent.setup();
  render(<ProfileEditor projectId="project-1" profile={null} profiles={[chief]} saving={false} error={null} onClose={() => {}} onSave={save} />);
  await user.type(screen.getByPlaceholderText('e.g. Product strategist'), 'Writer');
  await user.type(screen.getByPlaceholderText('Describe the role’s responsibilities and working style'), 'Write');
  await screen.findByRole('option', { name: 'GPT Test · text' });
  await user.selectOptions(screen.getByLabelText('Model'), 'codex-model');
  expect(screen.getByRole('option', { name: 'Extra high (xhigh)' })).toBeInTheDocument();
  expect(screen.queryByRole('option', { name: 'Max' })).not.toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('Reasoning effort'), 'xhigh');
  await user.click(screen.getByRole('button', { name: 'Save profile' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ model: 'gpt-test', reasoning_effort: 'xhigh' }));
});

it('keeps a saved effort when a model catalog no longer advertises it on unrelated edits', async () => {
  const codex = { ...account, id: 'codex-account', provider: 'openai_codex_runtime', auth_kind: 'account_runtime' };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/model/accounts')) return json([codex]);
    if (path.endsWith('/model/catalog')) return json([{ id: 'codex-model', account_id: codex.id,
      provider: codex.provider, model_id: 'gpt-test', display_name: 'GPT Test', category: 'text',
      available: true, reasoning_efforts: ['low'] }]);
    throw new Error(`Unexpected path ${path}`);
  }));
  const save = vi.fn(async () => {});
  const profile = { id: 'profile-1', project_id: 'project-1', kind: 'builtin' as const, name: 'Researcher',
    reports_to_profile_id: chief.id, model: 'gpt-test', model_account_id: codex.id, reasoning_effort: 'high', instructions: 'Research',
    memory_scope: 'project' as const, allowed_tools: [], autonomy_level: 'manual' as const,
    configured: true, permissions_active: false };
  const user = userEvent.setup();
  render(<ProfileEditor projectId="project-1" profile={profile} profiles={[chief, profile]} saving={false} error={null} onClose={() => {}} onSave={save} />);
  await waitFor(() => expect(screen.getByLabelText('Model')).toHaveValue('codex-model'));
  expect(screen.getByRole('option', { name: 'Previously saved: high · no longer advertised' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Save profile' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ reasoning_effort: 'high' }));
});

it('preserves a previously bound model that has become unavailable on unrelated edits', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/model/accounts')) return json([account]);
    if (path.endsWith('/model/catalog')) return json(models);
    throw new Error(`Unexpected path ${path}`);
  }));
  const save = vi.fn(async () => {});
  const profile = { id: 'profile-1', project_id: 'project-1', kind: 'builtin' as const, name: 'Designer',
    reports_to_profile_id: chief.id, model: 'image-a', model_account_id: account.id, instructions: 'Design', memory_scope: 'project' as const,
    allowed_tools: [], autonomy_level: 'manual' as const, configured: true, permissions_active: false };
  const user = userEvent.setup();
  render(<ProfileEditor projectId="project-1" profile={profile} profiles={[chief, profile]} saving={false} error={null} onClose={() => {}} onSave={save} />);
  await waitFor(() => expect(screen.getByLabelText('Model')).toHaveValue('model-2'));
  await user.click(screen.getByRole('button', { name: 'Save profile' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ model: 'image-a', model_account_id: 'account-1' }));
});

it('allows a custom profile to be saved before a model account exists', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/model/accounts') || path.endsWith('/model/catalog')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  const save = vi.fn(async () => {});
  const user = userEvent.setup();
  render(<ProfileEditor projectId="project-1" profile={null} profiles={[chief]} saving={false} error={null} onClose={() => {}} onSave={save} />);
  await user.type(screen.getByPlaceholderText('e.g. Product strategist'), 'Planner');
  await user.type(screen.getByPlaceholderText('Describe the role’s responsibilities and working style'), 'Plan');
  expect(screen.getByRole('option', { name: 'No model yet' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Save profile' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ model: null, model_account_id: null }));
});

it('keeps long pasted profile instructions intact and reports the new limit', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/model/accounts') || path.endsWith('/model/catalog')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  const save = vi.fn(async () => {});
  const user = userEvent.setup();
  render(<ProfileEditor projectId="project-1" profile={null} profiles={[chief]} saving={false} error={null} onClose={() => {}} onSave={save} />);
  await user.type(screen.getByPlaceholderText('e.g. Product strategist'), 'Long profile');
  const editor = screen.getByPlaceholderText('Describe the role’s responsibilities and working style');
  const longInstructions = 'Plan carefully.\n'.repeat(1_000);
  fireEvent.change(editor, { target: { value: longInstructions } });
  expect(editor).toHaveValue(longInstructions);
  await user.click(screen.getByRole('button', { name: 'Save profile' }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ instructions: longInstructions.trim() }));

  const overLimit = 'x'.repeat(50_001);
  fireEvent.change(editor, { target: { value: overLimit } });
  expect(editor).toHaveValue(overLimit);
  expect(screen.getByRole('alert')).toHaveTextContent('50,000 characters');
  expect(screen.getByRole('button', { name: 'Save profile' })).toBeDisabled();
});


it('shows the fresh provider catalog including GPT 6.1 Sol without refreshing on page load', async () => {
  const runtime = { ...account, provider: 'openai_codex_runtime', auth_kind: 'account_runtime', model_count: 1 };
  const descriptors = [{ id: 'openai_codex_runtime', name: 'Codex', auth_kind: 'account_runtime',
    catalog_supported: true, text_runtime_available: true, connectable: true }];
  const old = { ...models[0], provider: 'openai_codex_runtime', model_id: 'gpt-6-sol' };
  const sol = { ...old, id: 'fresh-sol', model_id: 'gpt-6.1-sol', display_name: 'GPT-6.1 Sol', reasoning_efforts: ['ultra'] };
  let refreshed = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json(descriptors);
    if (path.endsWith('/model/accounts')) return json([{ ...runtime, model_count: refreshed ? 2 : 1 }]);
    if (path.endsWith('/model/catalog')) return json(refreshed ? [old, sol] : [old]);
    if (path.endsWith('/refresh') && init?.method === 'POST') {
      refreshed = true;
      return json({ account: { ...runtime, model_count: 2 }, models: [old, sol] });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<ModelsView projectId="project-1" />);
  await screen.findByText('gpt-6-sol');
  expect(screen.queryByText('gpt-6.1-sol')).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/refresh'))).toBe(false);
  await userEvent.setup().click(screen.getByRole('button', { name: 'Discover models' }));
  expect(await screen.findByText('GPT-6.1 Sol')).toBeInTheDocument();
  expect(screen.getByText('gpt-6.1-sol')).toBeInTheDocument();
  expect(await screen.findByText(/2 models returned by the provider/)).toBeInTheDocument();
});

it('reloads unavailable saved models after failed discovery and preserves its error', async () => {
  let failed = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json(providers);
    if (path.endsWith('/model/accounts')) return json([{ ...account, catalog_valid: !failed }]);
    if (path.endsWith('/model/catalog')) return json([ { ...models[0], available: !failed } ]);
    if (path.endsWith('/refresh') && init?.method === 'POST') {
      failed = true;
      return json({ detail: 'Codex model discovery returned HTTP 503; retry discovery later.' }, 502);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<ModelsView projectId="project-1" />);
  await screen.findByText('Text A');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Discover models' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 503');
  expect(await screen.findByText('Unavailable')).toBeInTheDocument();
  expect(screen.queryByText(/model catalog refreshed/)).not.toBeInTheDocument();
  expect(screen.getByText('Text A')).toBeInTheDocument();
});


it('disables saved choices if discovery and saved-state reload both fail', async () => {
  let failed = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/refresh') && init?.method === 'POST') {
      failed = true;
      return json({ detail: 'Discovery transport failed' }, 502);
    }
    if (failed) return json({ detail: 'Catalog store unavailable' }, 503);
    if (path.endsWith('/model/providers')) return json(providers);
    if (path.endsWith('/model/accounts')) return json([account]);
    if (path.endsWith('/model/catalog')) return json([models[0]]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<ModelsView projectId="project-1" />);
  await screen.findByText('Text A');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Discover models' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Discovery transport failed');
  expect(screen.getByRole('alert')).toHaveTextContent('Catalog store unavailable');
  expect(await screen.findByText('Unavailable')).toBeInTheDocument();
  expect(screen.getByText('Text A')).toBeInTheDocument();
});
