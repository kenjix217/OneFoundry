import { useEffect, useState, type FormEvent } from 'react';

import { ApiError, type Profile } from './api';
import {
  configureChannel, confirmChannelIdentity, confirmPairing, getChannel, listChannels,
  listOutboundStatus, listPairingChallenges, storeChannelCredential, startWeChatPairing,
  getWeChatPairing, submitWeChatPairingCode, type ChannelStatus, type OutboundStatus, type PairingChallenge,
  type WeChatPairingSession,
} from './channelApi';

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Could not confirm the saved channel state. Refresh before retrying.';
}
function time(value: string | null): string { return value ? new Date(value).toLocaleString() : 'None recorded'; }
function savedDiscordMode(row?: ChannelStatus): 'dm' | 'server' {
  if (row?.guild_id) return 'server';
  return row?.sender_id || row?.chat_id || row?.identity_confirmed ? 'dm' : 'server';
}
function readiness(reason: string | null): string {
  const labels: Record<string, string> = {
    operator_identity_unbound: 'Exact operator and chat identity has not been bound.',
    chief_profile_unbound: 'This project needs a Chief of Staff profile.',
    chief_profile_changed: 'The saved Chief of Staff profile changed.',
    chief_model_unconfigured: 'Configure the Chief of Staff text model and instructions.',
    chief_model_unavailable: 'The Chief of Staff text model is unavailable.',
    chief_model_credential_unavailable: 'The Chief of Staff model credential is unavailable.',
  };
  return reason ? labels[reason] ?? reason.replaceAll('_', ' ') : 'Chief of Staff model checks are ready.';
}

export function ChannelsView({ projectId, profiles }: { projectId: string; profiles: Profile[] }) {
  const [channels, setChannels] = useState<ChannelStatus[]>([]);
  const [outbound, setOutbound] = useState<OutboundStatus[]>([]);
  const [challenges, setChallenges] = useState<PairingChallenge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [senderId, setSenderId] = useState('');
  const [chatId, setChatId] = useState('');
  const [guildId, setGuildId] = useState('');
  const [applicationId, setApplicationId] = useState('');
  const [discordMode, setDiscordMode] = useState<'dm' | 'server'>('server');
  const [credential, setCredential] = useState('');
  const [wechatSession, setWeChatSession] = useState<WeChatPairingSession | null>(null);
  const [wechatCode, setWeChatCode] = useState('');

  const chief = profiles.find((item) => item.name.trim().toLowerCase() === 'chief of staff');
  const discord = channels.find((item) => item.platform === 'discord');
  const wechat = channels.find((item) => item.platform === 'wechat');
  const latestOutbound = outbound.filter((item) => item.channel_id === discord?.id).slice(0, 5);
  const configuredFieldsMatch = !!discord && senderId.trim() === (discord.sender_id ?? '') &&
    chatId.trim() === (discord.chat_id ?? '') && guildId.trim() === (discord.guild_id ?? '') &&
    applicationId.trim() === (discord.bot_id ?? '') && discordMode === (discord.guild_id ? 'server' : 'dm');

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null); setCredential('');
    void Promise.all([
      listChannels(projectId, controller.signal),
      listOutboundStatus(projectId, controller.signal), listPairingChallenges(projectId, controller.signal),
    ]).then(([saved, sent, pairing]) => {
      if (controller.signal.aborted) return;
      setChannels(saved); setOutbound(sent); setChallenges(pairing);
      const row = saved.find((item) => item.platform === 'discord');
      setSenderId(row?.sender_id ?? ''); setChatId(row?.chat_id ?? '');
      setGuildId(row?.guild_id ?? ''); setApplicationId(row?.bot_id ?? '');
      setDiscordMode(savedDiscordMode(row));
      setLoading(false);
    }).catch((failure: unknown) => {
      if (controller.signal.aborted) return;
      setError(errorText(failure)); setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, refresh]);

  async function mutate(action: () => Promise<ChannelStatus>, success: string) {
    setBusy(true); setError(null); setNotice(null);
    try {
      const result = await action();
      const confirmed = await getChannel(projectId, result.id);
      if (confirmed.project_id !== projectId || confirmed.id !== result.id) throw new ApiError('Channel readback did not match the saved project.', null);
      setChannels((old) => [...old.filter((item) => item.platform !== confirmed.platform), confirmed]);
      if (confirmed.platform === 'discord') {
        setSenderId(confirmed.sender_id ?? ''); setChatId(confirmed.chat_id ?? '');
        setGuildId(confirmed.guild_id ?? ''); setApplicationId(confirmed.bot_id ?? '');
        setDiscordMode(savedDiscordMode(confirmed));
      }
      setCredential(''); setNotice(success);
      if (confirmed.platform === 'wechat') { setWeChatSession(null); setChallenges([]); }
    } catch (failure) { setError(errorText(failure)); }
    finally { setCredential(''); setBusy(false); }
  }

  function saveBinding(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!chief || !senderId.trim()) return;
    void mutate(() => configureChannel(projectId, 'discord', {
      sender_id: senderId.trim(), chat_id: chatId.trim() || null,
      bot_id: discordMode === 'server' ? applicationId.trim() : null,
      guild_id: discordMode === 'server' ? guildId.trim() : null,
      profile_id: chief.id,
      reply_policy: 'bound_dm', enabled: false,
    }), 'Discord binding saved disabled. Mention the bot in the bound server channel or send a personal DM, then refresh to inspect the observed IDs.');
  }

  function setEnabled(enabled: boolean) {
    if (!discord?.sender_id || !discord.chat_id || !discord.profile_id) return;
    void mutate(() => configureChannel(projectId, 'discord', {
      sender_id: discord.sender_id!, chat_id: discord.chat_id!,
      profile_id: discord.profile_id!, bot_id: discord.bot_id, guild_id: discord.guild_id,
      reply_policy: 'bound_dm', enabled,
    }), enabled ? 'Standing replies enabled for the confirmed Discord route.' : 'Discord replies disabled.');
  }

  function setWeChatEnabled(enabled: boolean) {
    if (!wechat?.sender_id || !wechat.chat_id || !wechat.profile_id) return;
    void mutate(() => configureChannel(projectId, 'wechat', {
      sender_id: wechat.sender_id!, chat_id: wechat.chat_id!, bot_id: wechat.bot_id,
      profile_id: wechat.profile_id!,
      reply_policy: 'bound_dm', enabled,
    }), enabled ? 'Standing WeChat replies enabled for the confirmed scanner.' : 'WeChat replies disabled.');
  }

  const canConfirmIdentity = !!discord?.observed_sender_id && !!discord.observed_chat_id &&
    discord.observed_sender_id === discord.sender_id &&
    (!discord.chat_id || discord.observed_chat_id === discord.chat_id) && !discord.identity_confirmed;

  async function pairWeChat(start: boolean) {
    if ((!start && !wechatSession) || (start && !chief)) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      const session = start ? await startWeChatPairing(projectId, chief!.id)
        : await getWeChatPairing(projectId, wechatSession!.session_id);
      setWeChatSession(session);
      if (start || session.status !== 'needs_code') setWeChatCode('');
      if (session.status === 'expired' || session.status === 'failed') setError(`WeChat pairing ${session.status}. Start a new local QR session.`);
      else if (session.status === 'needs_code') setNotice('WeChat requires a verification code. Enter it below to continue this pairing session.');
      else if (session.status === 'confirmed') setNotice('WeChat scan observed. Compare the scanner and bot IDs, then confirm them locally.');
    } catch (failure) { setError(errorText(failure)); }
    finally { setBusy(false); }
  }

  async function submitWeChatCode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!wechatSession || wechatSession.status !== 'needs_code' || !wechatCode.trim()) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      const session = await submitWeChatPairingCode(projectId, wechatSession.session_id, wechatCode.trim());
      setWeChatSession(session);
      setNotice('Verification code submitted. Check scan status to continue pairing.');
    } catch (failure) { setError(errorText(failure)); }
    finally { setWeChatCode(''); setBusy(false); }
  }

  const stagedWeChatChallenge: PairingChallenge | null = wechatSession?.status === 'confirmed' &&
    wechatSession.challenge_id && wechatSession.channel_id && wechatSession.scanner_id && wechatSession.bot_id
    ? { id: wechatSession.challenge_id, channel_id: wechatSession.channel_id,
      scanner_id: wechatSession.scanner_id, bot_id: wechatSession.bot_id, expires_at: wechatSession.expires_at }
    : null;

  return <section className="channels-page" aria-labelledby="channels-title">
    <div className="view-heading"><div><p className="eyebrow">Remote conversation</p><h1 id="channels-title">Messaging</h1>
      <p>Configure remote channels here. For web chat, use Work → Task workspace. Verified remote messages reach this project’s Chief of Staff without selecting a work order.</p></div>
      <button className="button button-outline" type="button" disabled={busy} onClick={() => setRefresh((value) => value + 1)}>Refresh saved state</button></div>
    {loading && <p role="status" className="setup-readback">Reading channel status…</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {notice && <p className="setup-readback" role="status">{notice}</p>}
    {!loading && <>
      <div className="channels-grid">
        <section className="channel-panel" aria-labelledby="discord-title">
          <div className="channel-panel-head"><div><p className="eyebrow">Primary channel</p><h2 id="discord-title">Discord</h2></div>
            <span className={`status-chip ${discord?.connected ? 'status-soft' : 'status-muted'}`}>
              {discord?.connected ? 'Connected' : discord?.enabled ? 'Enabled · offline' : 'Disabled'}</span></div>
          <p>Use a dedicated Discord bot. Choose a personal DM or one server text channel. Only your verified user ID can reach the Chief of Staff. In a server channel, mention the bot to start or continue work.</p>
          <div className="channel-state-grid">
            <div><span>Binding</span><strong>{discord?.configured ? 'Configured' : 'Incomplete'}</strong></div>
            <div><span>Bot credential</span><strong>{discord?.credential_present ? 'Stored' : 'Missing'}</strong></div>
            <div><span>Observed identity</span><strong>{discord?.identity_confirmed ? 'Confirmed' : discord?.observed_at ? 'Needs confirmation' : 'Not observed'}</strong></div>
            <div><span>Transport</span><strong>{discord?.connected ? 'Connected' : 'No live heartbeat'}</strong></div>
          </div>
          <p className="channel-readiness">{readiness(discord?.setup_readiness_reason ?? null)}</p>
          <form onSubmit={saveBinding} className="channel-form">
            <label>Where should the Chief reply?<select value={discordMode} onChange={(event) => { setDiscordMode(event.target.value as 'dm' | 'server'); setChatId(''); setGuildId(''); setApplicationId(''); }}>
              <option value="dm">Personal DM</option><option value="server">Server channel</option></select></label>
            {discordMode === 'server' && <p className="channel-help">Create a bot in the <a href="https://discord.com/developers/applications" target="_blank" rel="noopener noreferrer">Discord Developer Portal</a> and copy its application ID and bot token. Turn on <a href="https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID" target="_blank" rel="noopener noreferrer">Developer Mode</a> to copy your user, server, and text-channel IDs.</p>}
            <label>My Discord user ID<input value={senderId} onChange={(event) => setSenderId(event.target.value)} placeholder="Exact numeric user ID" required /></label>
            {discordMode === 'server' && <>
              <label>Discord application ID<input value={applicationId} onChange={(event) => setApplicationId(event.target.value)} placeholder="Application ID from Developer Portal" required /></label>
              <label>Discord server ID<input value={guildId} onChange={(event) => setGuildId(event.target.value)} placeholder="Exact server ID" required /></label>
            </>}
            <label>{discordMode === 'server' ? 'Discord channel ID' : 'Personal DM channel ID'}<input value={chatId} onChange={(event) => setChatId(event.target.value)} placeholder={discordMode === 'server' ? 'Exact text channel ID' : 'Can be filled from observed DM'} required={discordMode === 'server'} /></label>
            {discordMode === 'server' && /^[1-9][0-9]{0,19}$/.test(applicationId) && /^[1-9][0-9]{0,19}$/.test(guildId) && <a href={`https://discord.com/oauth2/authorize?client_id=${applicationId}&scope=bot&permissions=3072&guild_id=${guildId}&disable_guild_select=true`} target="_blank" rel="noopener noreferrer">Add this bot to your server</a>}
            {discordMode === 'server' && <p className="channel-help">Give the bot access to this channel, save the binding, then store the token below. Mention the bot once in the channel, refresh, confirm the observed IDs, and enable replies. A client secret, public key, and webhook URL are not needed for this bot connection.</p>}
            {!chief && <p className="form-error">This project has no Chief of Staff profile.</p>}
            <button className="button button-outline" type="submit" disabled={busy || !chief}>Save binding disabled</button>
          </form>
          <form className="channel-form channel-credential" onSubmit={(event) => {
            event.preventDefault(); if (discord && credential) void mutate(() => storeChannelCredential(projectId, discord.id, credential), 'Bot token stored. Replies remain disabled.');
          }}>
            <label>Discord bot token<input type="password" autoComplete="off" value={credential} onChange={(event) => setCredential(event.target.value)} placeholder={discord?.credential_present ? 'Replace stored token' : 'Paste bot token'} /></label>
            <p className="channel-help">The token is sent to local credential storage. It is never shown again by this page.</p>
            <button className="button button-outline" type="submit" disabled={busy || !discord || !credential}>Store bot token</button>
          </form>
          <div className="channel-observation">
            <h3>Confirm the observed {discordMode === 'server' ? 'server message' : 'DM'}</h3>
            <p>After the bot observes your {discordMode === 'server' ? 'mention in the configured server channel' : 'personal DM'}, compare these IDs with your Discord account before confirming.</p>
            <dl><div><dt>Observed user</dt><dd>{discord?.observed_sender_id ?? 'None'}</dd></div>
              <div><dt>Observed channel</dt><dd>{discord?.observed_chat_id ?? 'None'}</dd></div>
              <div><dt>Observed at</dt><dd>{time(discord?.observed_at ?? null)}</dd></div></dl>
            <button className="button button-outline" type="button" disabled={busy || !canConfirmIdentity || !configuredFieldsMatch}
              onClick={() => { if (discord?.observed_sender_id && discord.observed_chat_id)
                void mutate(() => confirmChannelIdentity(projectId, discord.id, discord.observed_sender_id!, discord.observed_chat_id!), 'Exact Discord route confirmed locally.'); }}>
              Confirm exact {discordMode === 'server' ? 'server channel' : 'personal DM'}</button>
          </div>
          <div className="channel-enable">
            <strong>Standing replies</strong><p>When enabled, the Chief of Staff may reply in this one confirmed {discordMode === 'server' ? 'server channel' : 'DM'} without approving each reply. All other outbound actions still use their own approvals. Disabling stops new send claims; an already claimed send may finish.</p>
            <button className={discord?.enabled ? 'button button-outline' : 'button button-primary'} type="button" disabled={busy || !discord?.credential_present || !discord?.identity_confirmed || !configuredFieldsMatch}
              onClick={() => setEnabled(!discord?.enabled)}>{discord?.enabled ? 'Disable Discord replies' : `Enable standing ${discordMode === 'server' ? 'server' : 'DM'} replies`}</button>
          </div>
        </section>
        <div className="channel-side">
          <section className="channel-panel" aria-labelledby="channel-activity-title"><h2 id="channel-activity-title">Connection and delivery</h2>
            <dl className="channel-activity"><div><dt>Last heartbeat</dt><dd>{time(discord?.last_heartbeat_at ?? null)}</dd></div>
              <div><dt>Last inbound</dt><dd>{time(discord?.last_inbound_at ?? null)}</dd></div>
              <div><dt>Last outbound</dt><dd>{time(discord?.last_outbound_at ?? null)}</dd></div>
              <div><dt>Last error code</dt><dd>{discord?.last_error_code ?? 'None recorded'}</dd></div></dl>
            <h3>Recent outbound</h3>{latestOutbound.length ? <ul className="channel-outbound">{latestOutbound.map((item) =>
              <li key={item.id}><strong>{item.status.replaceAll('_', ' ')}</strong><span>{time(item.finished_at ?? item.created_at)}</span>
                {item.reason && <small>{item.reason}</small>}</li>)}</ul> : <p>No outbound attempts recorded.</p>}
            <p className="channel-help">“Accepted by platform” means the platform accepted a send attempt; it does not prove the message was delivered or read.</p>
          </section>
          <section className="channel-panel" aria-labelledby="wechat-title"><div className="channel-panel-head"><div><p className="eyebrow">Optional channel</p><h2 id="wechat-title">WeChat</h2></div>
            <span className="status-chip status-muted">{wechat?.connected ? 'Connected' : wechat?.enabled ? 'Enabled · offline' : 'Not connected'}</span></div>
            <p>WeChat uses a separate QR pairing session. Scan only the code shown by the local OneFoundry setup, then confirm the exact scanner and bot IDs here.</p>
            {wechat && <p className="channel-readiness">{readiness(wechat.setup_readiness_reason)}</p>}
            <div className="channel-qr-actions"><button className="button button-outline" type="button" disabled={busy || !chief}
              onClick={() => { void pairWeChat(true); }}>Start WeChat QR pairing</button>
              {wechatSession && <button className="button button-outline" type="button" disabled={busy}
                onClick={() => { void pairWeChat(false); }}>Check scan status</button>}</div>
            {wechatSession && <div className="channel-challenge"><strong>QR session: {wechatSession.status}</strong>
              <p>Expires: {time(wechatSession.expires_at)}</p>
              {wechatSession.qr_image_data_url && wechatSession.status === 'waiting' &&
                <img className="channel-qr-image" src={wechatSession.qr_image_data_url} alt="WeChat pairing QR code" />}
              {wechatSession.status === 'needs_code' && <form className="channel-form" onSubmit={(event) => { void submitWeChatCode(event); }}>
                <p>WeChat requires a verification code before pairing can continue. Enter the code shown in your WeChat flow. It is sent only to the local pairing endpoint and cleared here after submission.</p>
                <label>WeChat verification code<input type="password" autoComplete="off" value={wechatCode}
                  maxLength={32} onChange={(event) => setWeChatCode(event.target.value)} required /></label>
                <button className="button button-outline" type="submit" disabled={busy || !wechatCode.trim()}>Submit verification code</button>
              </form>}
              {stagedWeChatChallenge && <><p>Scanner: {stagedWeChatChallenge.scanner_id}<br />Bot: {stagedWeChatChallenge.bot_id}</p>
                <button className="button button-outline" type="button" disabled={busy || new Date(stagedWeChatChallenge.expires_at).getTime() <= Date.now()}
                  onClick={() => { void mutate(() => confirmPairing(projectId, stagedWeChatChallenge.channel_id, stagedWeChatChallenge), 'WeChat pairing confirmed locally.'); }}>
                  Confirm observed pairing</button></>}
            </div>}
            {challenges.filter((item) => item.channel_id === wechat?.id).map((item) => <div key={item.id} className="channel-challenge">
              <strong>Pairing observed</strong><p>Scanner: {item.scanner_id}<br />Bot: {item.bot_id}<br />Expires: {time(item.expires_at)}</p>
              <button className="button button-outline" type="button" disabled={busy || new Date(item.expires_at).getTime() <= Date.now()}
                onClick={() => { if (wechat) void mutate(() => confirmPairing(projectId, wechat.id, item), 'WeChat pairing confirmed locally.'); }}>Confirm observed pairing</button>
            </div>)}
            {wechat?.identity_confirmed && <div className="channel-enable"><strong>Standing replies</strong>
              <p>Allow Chief of Staff replies only to this confirmed WeChat account. Other outbound actions still require their own approvals. Disabling stops new send claims; an already claimed send may finish.</p>
              <button className={wechat.enabled ? 'button button-outline' : 'button button-primary'} type="button"
                disabled={busy || !wechat.credential_present}
                onClick={() => setWeChatEnabled(!wechat.enabled)}>{wechat.enabled ? 'Disable WeChat replies' : 'Enable standing WeChat replies'}</button>
            </div>}
            {!wechat && !wechatSession && <p className="channel-help">No WeChat pairing has been started for this project.</p>}
          </section>
        </div>
      </div>
    </>}
  </section>;
}
