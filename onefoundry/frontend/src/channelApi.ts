import { ApiError, request } from './api';

export type ChannelPlatform = 'discord' | 'wechat';
export type ReplyPolicy = 'approval_each' | 'bound_dm';

export interface ChannelStatus {
  id: string; project_id: string; platform: ChannelPlatform;
  configured: boolean; enabled: boolean; reply_policy: ReplyPolicy;
  sender_id: string | null; chat_id: string | null; bot_id: string | null;
  guild_id: string | null;
  work_order_id: string | null; profile_id: string | null;
  credential_present: boolean; connected: boolean; identity_confirmed: boolean;
  observed_sender_id: string | null; observed_chat_id: string | null; observed_at: string | null;
  last_heartbeat_at: string | null; last_inbound_at: string | null;
  last_outbound_at: string | null; last_error_code: string | null;
  setup_readiness_reason: string | null;
}

export interface OutboundStatus {
  id: string; channel_id: string;
  status: 'approval_pending' | 'pending' | 'claimed' | 'accepted_by_platform' | 'failed' | 'unknown';
  reason: string | null; remote_message_id: string | null; created_at: string; finished_at: string | null;
}

export interface PairingChallenge {
  id: string; channel_id: string; scanner_id: string; bot_id: string; expires_at: string;
}

export interface WeChatPairingSession {
  session_id: string;
  status: 'waiting' | 'scanned' | 'needs_code' | 'expired' | 'failed' | 'confirmed';
  qr_image_data_url: string | null;
  expires_at: string;
  challenge_id: string | null; channel_id: string | null;
  scanner_id: string | null; bot_id: string | null;
}

export interface ChannelConfig {
  sender_id: string; chat_id: string | null; bot_id?: string | null;
  guild_id?: string | null;
  profile_id: string; reply_policy: ReplyPolicy; enabled: boolean;
}

function obj(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ApiError('The server returned an unexpected channel response.', null);
  return value as Record<string, unknown>;
}
function str(value: unknown): string {
  if (typeof value !== 'string') throw new ApiError('The server returned an unexpected channel response.', null);
  return value;
}
function nullable(value: unknown): string | null { return value == null ? null : str(value); }
function bool(value: unknown): boolean {
  if (typeof value !== 'boolean') throw new ApiError('The server returned an unexpected channel response.', null);
  return value;
}
function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new ApiError('The server returned an unexpected channel list.', null);
  return value;
}
function channel(value: unknown): ChannelStatus {
  const row = obj(value);
  if (row.platform !== 'discord' && row.platform !== 'wechat') throw new ApiError('Unknown channel platform.', null);
  if (row.reply_policy !== 'approval_each' && row.reply_policy !== 'bound_dm') throw new ApiError('Unknown reply policy.', null);
  return {
    id: str(row.id), project_id: str(row.project_id), platform: row.platform,
    configured: bool(row.configured), enabled: bool(row.enabled), reply_policy: row.reply_policy,
    sender_id: nullable(row.sender_id), chat_id: nullable(row.chat_id), bot_id: nullable(row.bot_id),
    guild_id: nullable(row.guild_id),
    work_order_id: nullable(row.work_order_id), profile_id: nullable(row.profile_id),
    credential_present: bool(row.credential_present), connected: bool(row.connected),
    identity_confirmed: bool(row.identity_confirmed), observed_sender_id: nullable(row.observed_sender_id),
    observed_chat_id: nullable(row.observed_chat_id), observed_at: nullable(row.observed_at),
    last_heartbeat_at: nullable(row.last_heartbeat_at), last_inbound_at: nullable(row.last_inbound_at),
    last_outbound_at: nullable(row.last_outbound_at), last_error_code: nullable(row.last_error_code),
    setup_readiness_reason: nullable(row.setup_readiness_reason),
  };
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/channels`;
const jsonPut = (body: unknown): RequestInit => ({ method: 'PUT', body: JSON.stringify(body) });

export async function listChannels(projectId: string, signal?: AbortSignal): Promise<ChannelStatus[]> {
  return array(await request(base(projectId), { signal })).map(channel);
}
export async function getChannel(projectId: string, channelId: string): Promise<ChannelStatus> {
  return channel(await request(`${base(projectId)}/${encodeURIComponent(channelId)}`));
}
export async function configureChannel(projectId: string, platform: ChannelPlatform, config: ChannelConfig): Promise<ChannelStatus> {
  return channel(await request(`${base(projectId)}/${platform}`, jsonPut(config)));
}
export async function storeChannelCredential(projectId: string, channelId: string, credential: string): Promise<ChannelStatus> {
  return channel(await request(`${base(projectId)}/${encodeURIComponent(channelId)}/credential`, jsonPut({ credential })));
}
export async function confirmChannelIdentity(projectId: string, channelId: string, senderId: string, chatId: string): Promise<ChannelStatus> {
  return channel(await request(`${base(projectId)}/${encodeURIComponent(channelId)}/identity/confirm`,
    { method: 'POST', body: JSON.stringify({ sender_id: senderId, chat_id: chatId }) }));
}
export async function listOutboundStatus(projectId: string, signal?: AbortSignal): Promise<OutboundStatus[]> {
  return array(await request(`${base(projectId)}/outbound`, { signal })).map((value) => {
    const row = obj(value);
    if (!['approval_pending', 'pending', 'claimed', 'accepted_by_platform', 'failed', 'unknown'].includes(String(row.status)))
      throw new ApiError('Unknown outbound status.', null);
    return { id: str(row.id), channel_id: str(row.channel_id), status: row.status as OutboundStatus['status'],
      reason: nullable(row.reason), remote_message_id: nullable(row.remote_message_id),
      created_at: str(row.created_at), finished_at: nullable(row.finished_at) };
  });
}
export async function listPairingChallenges(projectId: string, signal?: AbortSignal): Promise<PairingChallenge[]> {
  return array(await request(`${base(projectId)}/pairing-challenges`, { signal })).map((value) => {
    const row = obj(value);
    return { id: str(row.id), channel_id: str(row.channel_id), scanner_id: str(row.scanner_id),
      bot_id: str(row.bot_id), expires_at: str(row.expires_at) };
  });
}
export async function confirmPairing(projectId: string, channelId: string, challenge: PairingChallenge): Promise<ChannelStatus> {
  return channel(await request(`${base(projectId)}/${encodeURIComponent(channelId)}/pairing/${encodeURIComponent(challenge.id)}/confirm`,
    { method: 'POST', body: JSON.stringify({ scanner_id: challenge.scanner_id, bot_id: challenge.bot_id }) }));
}

function pairingSession(value: unknown): WeChatPairingSession {
  const row = obj(value);
  const status = row.status ?? 'waiting';
  if (!['waiting', 'scanned', 'needs_code', 'expired', 'failed', 'confirmed'].includes(String(status)))
    throw new ApiError('Unknown WeChat pairing status.', null);
  const image = nullable(row.qr_image_data_url);
  if (image && !/^data:image\/(png|jpeg|svg\+xml);base64,[A-Za-z0-9+/=]+$/.test(image))
    throw new ApiError('The pairing QR image was not a valid inline image.', null);
  return { session_id: str(row.session_id), status: status as WeChatPairingSession['status'],
    qr_image_data_url: image, expires_at: str(row.expires_at), challenge_id: nullable(row.challenge_id),
    channel_id: nullable(row.channel_id), scanner_id: nullable(row.scanner_id), bot_id: nullable(row.bot_id) };
}

export async function startWeChatPairing(projectId: string, profileId: string): Promise<WeChatPairingSession> {
  return pairingSession(await request(`${base(projectId)}/wechat/pairing/start`, {
    method: 'POST', body: JSON.stringify({ profile_id: profileId, reply_policy: 'bound_dm' }),
  }));
}
export async function getWeChatPairing(projectId: string, sessionId: string): Promise<WeChatPairingSession> {
  return pairingSession(await request(`${base(projectId)}/wechat/pairing/${encodeURIComponent(sessionId)}`));
}
export async function submitWeChatPairingCode(projectId: string, sessionId: string, code: string): Promise<WeChatPairingSession> {
  return pairingSession(await request(`${base(projectId)}/wechat/pairing/${encodeURIComponent(sessionId)}/verify-code`,
    { method: 'POST', body: JSON.stringify({ code }) }));
}
