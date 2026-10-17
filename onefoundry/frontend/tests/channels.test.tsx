import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ChannelsView } from '../src/ChannelsView';
import type { Profile } from '../src/api';
import type { ChannelStatus } from '../src/channelApi';

const projectId = 'project-1';
const chief: Profile = { id: 'chief-1', project_id: projectId, kind: 'builtin', name: 'Chief of Staff',
  reports_to_profile_id: null, model: 'model-1', instructions: 'Help the operator', memory_scope: 'project',
  allowed_tools: [], autonomy_level: 'manual', configured: true, permissions_active: false };
const initial: ChannelStatus = { id: 'channel-1', project_id: projectId, platform: 'discord', configured: false,
  enabled: false, reply_policy: 'approval_each', sender_id: null, chat_id: null, bot_id: null,
  guild_id: null,
  work_order_id: null, profile_id: null, credential_present: false, connected: false,
  identity_confirmed: false, observed_sender_id: null, observed_chat_id: null, observed_at: null,
  last_heartbeat_at: null, last_inbound_at: null, last_outbound_at: null, last_error_code: null,
  setup_readiness_reason: 'operator_identity_unbound' };
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}
afterEach(() => vi.unstubAllGlobals());

it('keeps Messaging for channel setup and directs web chat to the Task workspace', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith('/channels') || path.endsWith('/channels/outbound') ||
        path.endsWith('/channels/pairing-challenges')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<ChannelsView projectId={projectId} profiles={[chief]} />);
  await screen.findByRole('heading', { name: 'Discord' });
  expect(screen.getByText(/Work → Task workspace/)).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Chief of Staff inbox' })).not.toBeInTheDocument();
  expect(screen.queryByRole('textbox', { name: 'Message Chief of Staff' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.every(([path]) => !path.endsWith('/chief-inbox/turns'))).toBe(true);
});

it('shows the complete server setup by default when Discord has no saved route', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/channels')) return json([initial]);
    if (path.endsWith('/channels/outbound') || path.endsWith('/channels/pairing-challenges')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<ChannelsView projectId={projectId} profiles={[chief]} />);
  expect(await screen.findByLabelText('Discord application ID')).toBeInTheDocument();
  expect(screen.getByLabelText('Where should the Chief reply?')).toHaveValue('server');
  expect(screen.getByLabelText('Discord server ID')).toBeInTheDocument();
  expect(screen.getByLabelText('Discord channel ID')).toBeInTheDocument();
  expect(screen.getByLabelText('Discord bot token')).toBeInTheDocument();
  expect(screen.getByText(/Developer Mode/)).toBeInTheDocument();
});

it('keeps Discord disabled until exact observed DM confirmation and explicit enablement', async () => {
  let saved = { ...initial };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/projects/project-1/channels' && !init?.method) return json([saved]);
    if (path === '/api/projects/project-1/chief-inbox/turns') return json([]);
    if (path === '/api/projects/project-1/channels/outbound') return json([]);
    if (path === '/api/projects/project-1/channels/pairing-challenges') return json([]);
    if (path === '/api/projects/project-1/channels/channel-1' && !init?.method) return json(saved);
    if (path === '/api/projects/project-1/channels/discord' && init?.method === 'PUT') {
      const body = JSON.parse(init.body as string);
      saved = { ...saved, ...body, configured: !!body.chat_id, identity_confirmed: saved.identity_confirmed, enabled: body.enabled };
      return json(saved);
    }
    if (path === '/api/projects/project-1/channels/channel-1/credential' && init?.method === 'PUT') {
      expect(JSON.parse(init.body as string)).toEqual({ credential: 'secret-bot-token' });
      saved = { ...saved, credential_present: true };
      return json(saved);
    }
    if (path === '/api/projects/project-1/channels/channel-1/identity/confirm' && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ sender_id: '12345', chat_id: '67890' });
      saved = { ...saved, chat_id: '67890', configured: true, identity_confirmed: true };
      return json(saved);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ChannelsView projectId={projectId} profiles={[chief]} />);
  await screen.findByRole('heading', { name: 'Discord' });
  await user.selectOptions(screen.getByLabelText('Where should the Chief reply?'), 'dm');
  expect(screen.getByRole('button', { name: 'Enable standing DM replies' })).toBeDisabled();
  await user.type(screen.getByLabelText('My Discord user ID'), '12345');
  await user.click(screen.getByRole('button', { name: 'Save binding disabled' }));
  await waitFor(() => expect(saved.sender_id).toBe('12345'));
  expect(saved.enabled).toBe(false);
  await user.type(screen.getByLabelText('Discord bot token'), 'secret-bot-token');
  await user.click(screen.getByRole('button', { name: 'Store bot token' }));
  await waitFor(() => expect(saved.credential_present).toBe(true));
  expect(screen.getByLabelText('Discord bot token')).toHaveValue('');
  expect(screen.queryByText('secret-bot-token')).not.toBeInTheDocument();
  saved = { ...saved, observed_sender_id: '12345', observed_chat_id: '67890', observed_at: '2026-09-27T10:00:00Z' };
  await user.click(screen.getByRole('button', { name: 'Refresh saved state' }));
  const confirm = await screen.findByRole('button', { name: 'Confirm exact personal DM' });
  await waitFor(() => expect(confirm).toBeEnabled());
  await user.click(confirm);
  await waitFor(() => expect(saved.identity_confirmed).toBe(true));
  const enable = screen.getByRole('button', { name: 'Enable standing DM replies' });
  await waitFor(() => expect(enable).toBeEnabled());
  await user.click(enable);
  await waitFor(() => expect(saved.enabled).toBe(true));
  expect(saved.reply_policy).toBe('bound_dm');
  expect(screen.getByText('Enabled · offline')).toBeInTheDocument();
  expect(screen.getByText(/does not prove the message was delivered or read/)).toBeInTheDocument();
});

it('configures a mention-gated Discord server channel with application and server IDs', async () => {
  let saved = { ...initial };
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/projects/project-1/channels' && !init?.method) return json([saved]);
    if (path.endsWith('/channels/outbound') || path.endsWith('/channels/pairing-challenges')) return json([]);
    if (path.endsWith('/channels/channel-1') && !init?.method) return json(saved);
    if (path.endsWith('/channels/discord') && init?.method === 'PUT') {
      const body = JSON.parse(init.body as string);
      expect(body).toMatchObject({ sender_id: '12345', bot_id: '33333', guild_id: '44444',
        chat_id: '55555', enabled: false });
      saved = { ...saved, ...body, configured: true };
      return json(saved);
    }
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<ChannelsView projectId={projectId} profiles={[chief]} />);
  expect(await screen.findByLabelText('Where should the Chief reply?')).toHaveValue('server');
  await user.type(screen.getByLabelText('My Discord user ID'), '12345');
  await user.type(screen.getByLabelText('Discord application ID'), '33333');
  await user.type(screen.getByLabelText('Discord server ID'), '44444');
  await user.type(screen.getByLabelText('Discord channel ID'), '55555');
  expect(screen.getByRole('link', { name: 'Add this bot to your server' })).toHaveAttribute('href',
    'https://discord.com/oauth2/authorize?client_id=33333&scope=bot&permissions=3072&guild_id=44444&disable_guild_select=true');
  await user.click(screen.getByRole('button', { name: 'Save binding disabled' }));
  await waitFor(() => expect(saved.guild_id).toBe('44444'));
  expect(screen.getByText(/mention the bot to start or continue work/i)).toBeInTheDocument();
  expect(screen.queryByRole('textbox', { name: 'Message Chief of Staff' })).not.toBeInTheDocument();
});

it('rejects a mismatched observed sender and leaves confirmation unavailable', async () => {
  const saved = { ...initial, sender_id: '12345', profile_id: chief.id,
    credential_present: true, observed_sender_id: 'wrong-user', observed_chat_id: '67890', observed_at: '2026-09-27T10:00:00Z' };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/channels')) return json([saved]);
    if (path.endsWith('/chief-inbox/turns')) return json([]);
    if (path.endsWith('/channels/outbound') || path.endsWith('/channels/pairing-challenges')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<ChannelsView projectId={projectId} profiles={[chief]} />);
  const confirm = await screen.findByRole('button', { name: 'Confirm exact personal DM' });
  expect(screen.getByLabelText('Where should the Chief reply?')).toHaveValue('dm');
  expect(confirm).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Enable standing DM replies' })).toBeDisabled();
});

it('starts optional WeChat QR pairing without selecting a work order', async () => {
  const image = `data:image/svg+xml;base64,${btoa('<svg></svg>')}`;
  let wechatEnabled = false;
  let codeSubmitted = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/channels') && !init?.method) return json([initial]);
    if (path.endsWith('/chief-inbox/turns')) return json([]);
    if (path.endsWith('/channels/outbound') || path.endsWith('/channels/pairing-challenges')) return json([]);
    if (path.endsWith('/channels/wechat/pairing/start') && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ profile_id: chief.id, reply_policy: 'bound_dm' });
      return json({ session_id: 'session-1', qr_image_data_url: image, expires_at: '2099-01-01T00:00:00Z' });
    }
    if (path.endsWith('/channels/wechat/pairing/session-1')) return json(codeSubmitted ? {
      session_id: 'session-1', status: 'confirmed', qr_image_data_url: null, expires_at: '2099-01-01T00:00:00Z',
      challenge_id: 'challenge-1', channel_id: 'wechat-1', scanner_id: 'scanner-1', bot_id: 'bot-1',
    } : { session_id: 'session-1', status: 'needs_code', expires_at: '2099-01-01T00:00:00Z' });
    if (path.endsWith('/channels/wechat/pairing/session-1/verify-code') && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ code: '492801' });
      codeSubmitted = true;
      return json({ session_id: 'session-1', status: 'waiting', expires_at: '2099-01-01T00:00:00Z' });
    }
    if (path.endsWith('/channels/wechat-1/pairing/challenge-1/confirm')) return json({
      ...initial, id: 'wechat-1', platform: 'wechat', sender_id: 'scanner-1', bot_id: 'bot-1',
      chat_id: 'scanner-1', identity_confirmed: true, credential_present: true,
      profile_id: chief.id,
    });
    if (path.endsWith('/channels/wechat-1')) return json({
      ...initial, id: 'wechat-1', platform: 'wechat', sender_id: 'scanner-1', bot_id: 'bot-1',
      chat_id: 'scanner-1', identity_confirmed: true, credential_present: true,
      profile_id: chief.id, enabled: wechatEnabled,
    });
    if (path.endsWith('/channels/wechat') && init?.method === 'PUT') {
      const body = JSON.parse(init.body as string);
      expect(body).toEqual({ sender_id: 'scanner-1', chat_id: 'scanner-1', bot_id: 'bot-1',
        profile_id: chief.id, reply_policy: 'bound_dm', enabled: true });
      wechatEnabled = true;
      return json({ ...initial, id: 'wechat-1', platform: 'wechat', ...body,
        configured: true, credential_present: true, identity_confirmed: true });
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ChannelsView projectId={projectId} profiles={[chief]} />);
  const start = await screen.findByRole('button', { name: 'Start WeChat QR pairing' });
  expect(start).toBeEnabled();
  await user.click(start);
  expect(await screen.findByAltText('WeChat pairing QR code')).toHaveAttribute('src', image);
  await user.click(screen.getByRole('button', { name: 'Check scan status' }));
  expect(await screen.findByText(/WeChat requires a verification code before pairing can continue/)).toBeInTheDocument();
  await user.type(screen.getByLabelText('WeChat verification code'), '492801');
  await user.click(screen.getByRole('button', { name: 'Submit verification code' }));
  await waitFor(() => expect(codeSubmitted).toBe(true));
  expect(screen.queryByText('492801')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Check scan status' }));
  expect(await screen.findByText((_, element) => element?.tagName === 'P' && element.textContent?.includes('Scanner: scanner-1') === true)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Confirm observed pairing' }));
  await screen.findByText('WeChat pairing confirmed locally.');
  expect(screen.getByLabelText('Where should the Chief reply?')).toHaveValue('server');
  expect(screen.getByLabelText('Discord application ID')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Enable standing WeChat replies' }));
  await waitFor(() => expect(wechatEnabled).toBe(true));
});
