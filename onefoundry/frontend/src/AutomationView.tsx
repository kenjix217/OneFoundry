import { useEffect, useState, type FormEvent } from 'react';

import { ApiError, type Profile } from './api';
import {
  createRule, getCoordination, listRules, listSignals, reviseRule, setAgentRuleAccess, setPaused, setRuleEnabled, setToolCeiling,
  type CoordinationRule, type CoordinationSignal, type CoordinationStatus, type DeliverableKind, type RuleFields, type Schedule,
  type SignalIntent,
} from './coordinationApi';

const SIGNAL_LABELS: Record<string, string> = {
  'work_order.done': 'A work order is completed',
  'work_order.needs_review': 'A work order needs review',
  'work_order.failed': 'A work order fails',
  'work_order.unknown': "A work order's outcome is unknown",
  'work_order.blocked': 'A work order is blocked',
  'approval.approved': 'An approval is granted',
  'approval.rejected': 'An approval is rejected',
  'organization.qualified': 'An organization is qualified (built-in Sales draft)',
  'schedule.fired': 'On a schedule',
  'agent.finding': 'An agent reports a finding',
  'email.received': 'A new email arrives',
  'document.created': 'A new file appears in a watched folder',
  'document.changed': 'A file changes in a watched folder',
};

const TOPICS = ['lead', 'opportunity', 'risk', 'deadline', 'bug', 'customer', 'other'];

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

function describeSchedule(value: Schedule | null, timezone: string): string {
  if (!value) return '';
  if (value.kind === 'interval') return `every ${value.every_hours} hour${value.every_hours === 1 ? '' : 's'}`;
  const day = value.kind === 'weekly' ? `every ${WEEKDAYS[value.weekday ?? 0]}` : 'every day';
  return `${day} at ${value.at} (${timezone})`;
}

const MODE_TEXT: Record<CoordinationStatus['mode'], string> = {
  off: 'Off. No events are recorded and no rules run.',
  draft_only: 'Drafts only. Rules create draft work orders; you decide what starts.',
  auto: 'Auto. Rules can create ready work or start it automatically.',
};

const REASONS: Record<string, string> = {
  no_matching_rule: 'No rule matched',
  chain_depth_exceeded: 'Stopped: event chain limit reached',
  rule_daily_limit: "Stopped: rule's daily limit reached",
  project_daily_limit: "Stopped: project's daily limit reached",
  project_inactive: 'Skipped: project is not active',
  target_profile_missing: 'Stopped: assigned agent no longer exists',
  fulfilment_failed: 'Stopped after repeated failures',
  ask_operator: 'Chief of Staff asks you to decide',
  triage_ignored: 'Chief of Staff decided no work is needed',
  triage_unavailable: 'Chief of Staff could not triage (check its model in Team)',
  triage_model_failed: 'The Chief of Staff model rejected the request',
  triage_outcome_unknown: 'Triage outcome unknown; not retried',
  triage_interrupted: 'Triage was interrupted; not retried',
  chief_missing: 'No Chief of Staff profile',
  auto_mode_off: 'Not started: automation is not in Auto mode',
  work_order_changed: 'Not started: the work order changed first',
  auto_start_daily_limit: "Not started: the project's daily auto-start limit was reached",
  rule_model_attempt_limit: "Not started: this rule's daily model-attempt limit was reached",
  rule_quiet_hours: 'Waiting until this rule’s quiet hours end',
  auto_start_unavailable: 'Not started: the assigned agent cannot run yet (check its model in Team)',
  auto_start_failed: 'The model rejected the run',
  auto_start_outcome_unknown: 'Run outcome unknown; not retried',
  launch_interrupted: 'Start was interrupted; not retried',
};

const DELIVERABLES: [DeliverableKind, string][] = [
  ['operator_review', 'Operator review'], ['text_reply', 'Text reply'], ['artifact', 'Saved artifact'],
  ['document', 'Document'], ['code_change', 'Code change'], ['external_action', 'External action'],
];

function intentSource(intent: SignalIntent): string {
  if (intent.action_kind === 'chief_triage') return 'Chief of Staff triage';
  if (intent.action_kind === 'auto_start') return 'Automatic start';
  return intent.rule_name ?? 'Built-in continuation';
}

function intentOutcome(intent: SignalIntent): string {
  if (intent.error_code) return REASONS[intent.error_code] ?? intent.error_code;
  if (intent.action_kind === 'auto_start') return intent.state === 'done' ? 'run started' : intent.state === 'pending' ? 'waiting to start' : intent.state;
  if (intent.work_order_title) return `created “${intent.work_order_title}”`;
  if (intent.decision?.action === 'ignore') return REASONS.triage_ignored;
  return intent.state === 'pending' ? 'queued' : intent.state;
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : 'The result is unknown. Refresh Automation before retrying.';
}

function emptyRule(profiles: Profile[]): RuleFields {
  return { name: '', signal_type: 'work_order.done', schedule: null, filters: {}, target_profile_id: profiles[0]?.id ?? '',
    title: '', instructions: '', deliverable_kind: 'operator_review', autonomy: 'draft', max_per_day: 10,
    max_model_attempts: 10, quiet_hours: null, max_chain_depth: null };
}

function when(item: { signal_type: string; filters: Record<string, string>; schedule: Schedule | null }, timezone: string): string {
  if (item.schedule) return describeSchedule(item.schedule, timezone);
  const role = item.filters.profile_name ? ` for ${item.filters.profile_name}` : '';
  const label = SIGNAL_LABELS[item.signal_type] ?? item.signal_type;
  return `${label.charAt(0).toLowerCase()}${label.slice(1)}${role}`;
}

export function AutomationView({ projectId, profiles, onOpenConnections }: { projectId: string; profiles: Profile[]; onOpenConnections?: () => void }) {
  const [section, setSection] = useState<'rules' | 'events' | 'controls'>('rules');
  const [status, setStatus] = useState<CoordinationStatus | null>(null);
  const [rules, setRules] = useState<CoordinationRule[]>([]);
  const [signals, setSignals] = useState<CoordinationSignal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [editing, setEditing] = useState<CoordinationRule | null>(null);
  const [form, setForm] = useState<RuleFields>(() => emptyRule(profiles));
  const [saving, setSaving] = useState(false);
  const [ceiling, setCeiling] = useState<string[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null);
    void Promise.all([getCoordination(projectId, controller.signal), listRules(projectId, controller.signal),
      listSignals(projectId, controller.signal)]).then(([nextStatus, nextRules, nextSignals]) => {
      setStatus(nextStatus); setRules(nextRules); setSignals(nextSignals); setCeiling(nextStatus.profile_tool_ceiling); setLoading(false);
    }).catch((failure: unknown) => {
      if (controller.signal.aborted) return;
      setError(message(failure)); setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, refresh]);

  const agent = (id: string) => profiles.find((profile) => profile.id === id)?.name ?? 'Unknown agent';
  const update = (patch: Partial<RuleFields>) => setForm((current) => ({ ...current, ...patch }));
  const filterKeys = status?.signal_types[form.signal_type] ?? [];

  async function togglePause() {
    if (!status) return;
    setSaving(true); setError(null); setNotice(null);
    try {
      const next = await setPaused(projectId, !status.paused, status.version);
      setStatus(next);
      setNotice(next.paused ? 'Automation paused. New events wait until you resume.' : 'Automation resumed.');
    } catch (failure) { setError(message(failure)); }
    finally { setSaving(false); }
  }

  async function toggleAgentRuleAccess() {
    if (!status) return;
    setSaving(true); setError(null); setNotice(null);
    try {
      const next = await setAgentRuleAccess(projectId, !status.agent_rules_enabled, status.version);
      setStatus(next);
      setNotice(next.agent_rules_enabled ? 'Agents may manage rules in project chats.' : 'Agent rule management is off.');
    } catch (failure) { setError(message(failure)); }
    finally { setSaving(false); }
  }

  async function saveCeiling() {
    if (!status) return;
    setSaving(true); setError(null); setNotice(null);
    try {
      const next = await setToolCeiling(projectId, ceiling, status.version);
      setStatus(next); setCeiling(next.profile_tool_ceiling);
      setNotice(next.profile_tool_ceiling.length ? `Proposed agents may request: ${next.profile_tool_ceiling.map((tool) => tool.replace(/_v[0-9]+$/, '').replaceAll('_', ' ')).join(', ')}.` : 'Proposed agents may not request any tools.');
    } catch (failure) { setError(message(failure)); }
    finally { setSaving(false); }
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true); setFormError(null); setNotice(null);
    const fields: RuleFields = { ...form, name: form.name.trim(), title: form.title.trim(), instructions: form.instructions.trim(),
      filters: Object.fromEntries(Object.entries(form.filters).filter(([key, value]) => value && filterKeys.includes(key))) };
    try {
      const saved = editing ? await reviseRule(projectId, editing.id, fields, editing.version) : await createRule(projectId, fields);
      const readback = await listRules(projectId);
      if (!readback.some((item) => item.id === saved.id && item.current_version === saved.current_version)) {
        throw new ApiError('The rule was saved, but readback did not match. Refresh before retrying.', null);
      }
      setRules(readback); setEditing(null); setForm(emptyRule(profiles));
      setNotice(editing ? `Rule saved as version ${saved.current_version}. Earlier work keeps the version that created it.` : 'Rule created.');
    } catch (failure) { setFormError(message(failure)); }
    finally { setSaving(false); }
  }

  async function toggleRule(item: CoordinationRule) {
    setSaving(true); setError(null); setNotice(null);
    try {
      const saved = await setRuleEnabled(projectId, item.id, !item.enabled, item.version);
      setRules((current) => current.map((rule) => rule.id === saved.id ? saved : rule));
    } catch (failure) { setError(message(failure)); }
    finally { setSaving(false); }
  }

  function edit(item: CoordinationRule) {
    setEditing(item); setFormError(null);
    setForm({ name: item.name, signal_type: item.signal_type, schedule: item.schedule, filters: item.filters, target_profile_id: item.target_profile_id,
      title: item.title, instructions: item.instructions, deliverable_kind: item.deliverable_kind, autonomy: item.autonomy,
      max_per_day: item.max_per_day, max_chain_depth: item.max_chain_depth,
      max_model_attempts: item.max_model_attempts ?? 10, quiet_hours: item.quiet_hours ?? null });
  }

  return <div className="skill-page">
    <header className="view-heading"><div><p className="eyebrow">Event-driven work</p><h1>Automation</h1>
      <p>Rules turn events into work orders, so you don't have to start every next step yourself. Agents never trigger each other directly. Every event, rule decision, and limit is recorded.</p></div>
      <button type="button" className="button button-outline" onClick={() => setRefresh((value) => value + 1)}>Refresh</button></header>
    {notice && <p className="notice" role="status">{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {loading && !status ? <p>Loading automation…</p> : status && <>
      <div className="automation-summary"><span>{status.mode === 'off' ? 'Automation is off' : status.paused ? 'Automation is paused' : status.mode === 'auto' ? 'Automatic work is enabled' : 'Rules create drafts for review'}</span><button type="button" className="button button-quiet" onClick={() => setSection('controls')}>Manage automation</button></div>
      <nav className="workspace-tabs" aria-label="Automation pages">{([['rules', 'Rules'], ['events', 'Recent events'], ['controls', 'Controls & limits']] as const).map(([value, label]) => <button key={value} type="button" aria-current={section === value ? 'page' : undefined} onClick={() => setSection(value)}>{label}</button>)}</nav>
      <section className="models-section" hidden={section !== 'controls'} aria-label="Automation status">
        <h2>Status</h2>
        <p><strong>Mode:</strong> {MODE_TEXT[status.mode]}</p>
        <p>Event chain limit: {status.max_chain_depth} steps · Daily limit: {status.max_orders_per_day} work orders and {status.max_auto_starts_per_day} automatic starts per project</p>
        <p>{status.paused ? 'Paused. New events are recorded but not acted on.' : 'Running.'}</p>
        {status.mode === 'off' && <p className="field-hint">Automation is not enabled for this installation. Your installer can enable it; saved rules remain available here.</p>}
        <button type="button" className="button button-outline" disabled={saving} onClick={() => { void togglePause(); }}>
          {status.paused ? 'Resume automation' : 'Pause automation'}</button>
        <p><strong>Agent rule management:</strong> {status.agent_rules_enabled ? 'On' : 'Off'} for project chats and confirmed operator DMs.</p>
        <button type="button" className="button button-outline" disabled={saving} onClick={() => { void toggleAgentRuleAccess(); }}>
          {status.agent_rules_enabled ? 'Turn off agent rule management' : 'Allow agents to manage rules'}</button>
      </section>

      {onOpenConnections && <div className="automation-connection-link"><span>Email and folder connections are managed in Settings.</span><button type="button" className="button button-outline" onClick={onOpenConnections}>Manage connections →</button></div>}

      <section className="models-section" hidden={section !== 'controls'} aria-label="Agent proposals">
        <h2>Agents the Chief of Staff may propose</h2>
        <p>When the Chief of Staff's allowed tools include the ability to propose agents, it can propose a new agent for a task. Nothing is created until you approve it in Approvals. Proposed agents can only use tools you tick here.</p>
        <fieldset className="tool-ceiling">
          <legend className="field-label">Tools a proposed agent may request</legend>
          {status.registered_tools.map((tool) => <label key={tool} className="checkbox-row">
            <input type="checkbox" checked={ceiling.includes(tool)} disabled={saving}
              onChange={(event) => setCeiling(event.target.checked ? [...ceiling, tool] : ceiling.filter((item) => item !== tool))} />
            <span>{tool.replace(/_v[0-9]+$/, '').replaceAll('_', ' ')}</span></label>)}
        </fieldset>
        <button type="button" className="button button-outline" disabled={saving} onClick={() => { void saveCeiling(); }}>Save tool limit</button>
      </section>

      <section className="models-section" hidden={section !== 'rules'} aria-label="Automation rules">
        <h2>Rules</h2>
        {rules.length === 0 ? <p>No rules yet. Create one below.</p> : <ul className="automation-rules">
          {rules.map((item) => <li key={item.id}>
            <p><strong>{item.name}</strong> · v{item.current_version} · {item.enabled ? 'On' : 'Off'}</p>
            {item.last_agent_action && <p className="field-hint">Last agent action: {item.last_agent_action.decision.replaceAll('_', ' ')}
              {' '}at version {item.last_agent_action.rule_version} via {item.last_agent_action.source_kind.replaceAll('_', ' ')}
              </p>}
            <p>{item.schedule ? 'On a schedule' : 'When'} {when(item, status.timezone)}, create “{item.title}” for {agent(item.target_profile_id)} {item.autonomy === 'draft' ? 'as a draft'
              : status.mode !== 'auto' ? `${item.autonomy === 'ready' ? 'as ready work' : 'and start it'} in Auto mode, a draft for now`
              : item.autonomy === 'ready' ? 'as ready work' : 'and start it automatically'} (up to {item.max_per_day} a day).</p>
            <p>Automatic starts: up to {item.max_model_attempts ?? 10} admitted attempts per day.
              {item.quiet_hours && <> Quiet hours: {item.quiet_hours.start}–{item.quiet_hours.end} ({status?.timezone ?? 'project time'}).</>}</p>
            <button type="button" className="button button-quiet" disabled={saving} onClick={() => edit(item)}>Edit</button>
            <button type="button" className="button button-quiet" disabled={saving} onClick={() => { void toggleRule(item); }}>
              {item.enabled ? 'Turn off' : 'Turn on'}</button>
          </li>)}</ul>}
        <form className="skill-create" onSubmit={(event) => { void save(event); }}>
          <h3>{editing ? `Edit “${editing.name}”` : 'Create rule'}</h3>
          <label className="field"><span className="field-label">Rule name</span>
            <input required maxLength={120} value={form.name} onChange={(event) => update({ name: event.target.value })} placeholder="e.g. Finished engineering goes to QA" /></label>
          <label className="field"><span className="field-label">When</span>
            <select value={form.signal_type} onChange={(event) => update({ signal_type: event.target.value, filters: {},
              schedule: event.target.value === 'schedule.fired' ? { kind: 'daily', at: '09:00' } : null })}>
              {Object.keys(status.signal_types).map((key) => <option key={key} value={key}>{SIGNAL_LABELS[key] ?? key}</option>)}
            </select></label>
          {form.schedule && <fieldset className="field"><legend className="field-label">Schedule ({status.timezone})</legend>
            <select aria-label="Repeat" value={form.schedule.kind} onChange={(event) => {
              const kind = event.target.value as Schedule['kind'];
              update({ schedule: kind === 'interval' ? { kind, every_hours: 24 } : kind === 'weekly' ? { kind, at: '09:00', weekday: 0 } : { kind, at: '09:00' } });
            }}>
              <option value="daily">Every day</option><option value="weekly">Every week</option><option value="interval">Every few hours</option>
            </select>
            {form.schedule.kind === 'weekly' && <select aria-label="Day" value={form.schedule.weekday ?? 0}
              onChange={(event) => update({ schedule: { ...form.schedule as Schedule, weekday: Number(event.target.value) } })}>
              {WEEKDAYS.map((day, index) => <option key={day} value={index}>{day}</option>)}</select>}
            {form.schedule.kind !== 'interval' ? <input aria-label="Time" type="time" required value={form.schedule.at ?? '09:00'}
              onChange={(event) => update({ schedule: { ...form.schedule as Schedule, at: event.target.value } })} />
              : <input aria-label="Hours between runs" type="number" min={1} max={168} required value={form.schedule.every_hours ?? 24}
                onChange={(event) => update({ schedule: { kind: 'interval', every_hours: Number(event.target.value) } })} />}
            <span className="field-hint">Only the most recent missed time runs after a pause or restart; nothing runs for times before the rule was saved.</span>
          </fieldset>}
          {filterKeys.includes('from_domain') && <label className="field"><span className="field-label">Only from this domain (optional)</span>
            <input maxLength={200} value={form.filters.from_domain ?? ''} placeholder="e.g. acme.com"
              onChange={(event) => update({ filters: { ...form.filters, from_domain: event.target.value.trim().toLowerCase() } })} /></label>}
          {filterKeys.includes('source_label') && <label className="field"><span className="field-label">Only this watched folder (optional)</span>
            <input maxLength={80} value={form.filters.source_label ?? ''} placeholder="Folder name as saved below"
              onChange={(event) => update({ filters: { ...form.filters, source_label: event.target.value } })} /></label>}
          {filterKeys.includes('topic') && <label className="field"><span className="field-label">Only this topic (optional)</span>
            <select value={form.filters.topic ?? ''} onChange={(event) => update({ filters: { ...form.filters, topic: event.target.value } })}>
              <option value="">Any topic</option>
              {TOPICS.map((topic) => <option key={topic} value={topic}>{topic}</option>)}
            </select></label>}
          {filterKeys.includes('profile_name') && <label className="field"><span className="field-label">Only for this agent (optional)</span>
            <select value={form.filters.profile_name ?? ''} onChange={(event) => update({ filters: { ...form.filters, profile_name: event.target.value } })}>
              <option value="">Any agent</option>
              {profiles.map((profile) => <option key={profile.id} value={profile.name}>{profile.name}</option>)}
            </select></label>}
          <label className="field"><span className="field-label">Assign new work to</span>
            <select required value={form.target_profile_id} onChange={(event) => update({ target_profile_id: event.target.value })}>
              {profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
            </select></label>
          <label className="field"><span className="field-label">Work order title</span>
            <input required maxLength={160} value={form.title} onChange={(event) => update({ title: event.target.value })} /></label>
          <label className="field"><span className="field-label">Instructions</span>
            <textarea required maxLength={8000} rows={4} value={form.instructions} onChange={(event) => update({ instructions: event.target.value })} />
            <span className="field-hint">The triggering event's details are added below these instructions, labelled as data.</span></label>
          <label className="field"><span className="field-label">Expected deliverable</span>
            <select value={form.deliverable_kind} onChange={(event) => update({ deliverable_kind: event.target.value as DeliverableKind })}>
              {DELIVERABLES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select></label>
          <label className="field"><span className="field-label">Create work as</span>
            <select value={form.autonomy} onChange={(event) => update({ autonomy: event.target.value as RuleFields['autonomy'] })}>
              <option value="draft">Draft (you mark it ready)</option><option value="ready">Ready to start</option>
              <option value="auto_start">Start automatically</option>
            </select>
            <span className="field-hint">“Ready” and “Start automatically” apply only in Auto mode; in Drafts only mode every rule creates drafts. Automatic starts make one model run, never retry an uncertain one, and still need review before the work counts as done.</span></label>
          <label className="field"><span className="field-label">Most work orders per day</span>
            <input type="number" min={1} max={1000} required value={form.max_per_day} onChange={(event) => update({ max_per_day: Number(event.target.value) })} /></label>
          <label className="field"><span className="field-label">Most automatic model attempts per day</span>
            <input type="number" min={0} max={1000} required value={form.max_model_attempts ?? 10}
              onChange={(event) => update({ max_model_attempts: Number(event.target.value) })} />
            <span className="field-hint">Counts admitted automatic starts across this rule’s versions, including failed or uncertain attempts. Zero keeps automatic starts off. Uses {status.timezone}.</span></label>
          <label><input type="checkbox" checked={!!form.quiet_hours}
            onChange={(event) => update({ quiet_hours: event.target.checked ? { start: '22:00', end: '08:00' } : null })} /> Use quiet hours</label>
          {form.quiet_hours && <div className="field">
            <label>Quiet hours start<input type="time" required value={form.quiet_hours.start}
              onChange={(event) => update({ quiet_hours: { ...form.quiet_hours!, start: event.target.value } })} /></label>
            <label>Quiet hours end<input type="time" required value={form.quiet_hours.end}
              onChange={(event) => update({ quiet_hours: { ...form.quiet_hours!, end: event.target.value } })} /></label>
            <span className="field-hint">Incoming work is saved. Automatic starts wait until this window ends in {status.timezone}.</span>
          </div>}
          <label className="field"><span className="field-label">Lower event chain limit (optional)</span>
            <input type="number" min={1} max={status.max_chain_depth} value={form.max_chain_depth ?? ''}
              onChange={(event) => update({ max_chain_depth: event.target.value ? Number(event.target.value) : null })} />
            <span className="field-hint">Leave empty to use the project limit of {status.max_chain_depth}. A rule can lower it, never raise it.</span></label>
          {formError && <p className="form-error" role="alert">{formError}</p>}
          <button className="button button-primary" type="submit" disabled={saving || !form.target_profile_id}>{saving ? 'Saving…' : editing ? 'Save new version' : 'Create rule'}</button>
          {editing && <button className="button button-quiet" type="button" onClick={() => { setEditing(null); setForm(emptyRule(profiles)); }}>Cancel</button>}
        </form>
      </section>

      <section className="models-section" hidden={section !== 'events'} aria-label="Recent events">
        <h2>Recent events</h2>
        {signals.length === 0 ? <p>No events recorded yet.</p> : <ol className="task-activity-list">
          {signals.map((item) => <li key={item.id}>
            <div className="task-activity-head"><strong>{SIGNAL_LABELS[item.signal_type] ?? item.signal_type}</strong>
              <time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString()}</time></div>
            {typeof item.fields.title === 'string' && <p>Work order: “{item.fields.title}”{typeof item.fields.profile_name === 'string' ? ` · ${item.fields.profile_name}` : ''}</p>}
            {typeof item.fields.file_name === 'string' && <p>File: “{item.fields.file_name}”{typeof item.fields.ingest_error === 'string' ? ` (not saved: ${item.fields.ingest_error})` : ''}</p>}
            {typeof item.fields.subject === 'string' && <p>Email from {String(item.fields.from_address)}: “{item.fields.subject}”</p>}
            {typeof item.fields.summary === 'string' && <p>Finding ({String(item.fields.topic)}): “{item.fields.summary}”</p>}
            <small>Event chain step {item.chain_depth} · {item.state === 'new' ? 'Waiting' : item.state === 'matched' ? 'Acted on'
              : item.state === 'triage' ? 'With Chief of Staff' : item.state === 'ignored' ? 'No action' : 'Stopped'}
              {item.reason ? ` · ${REASONS[item.reason] ?? item.reason}` : ''}</small>
            {item.intents.length > 0 && <ul>{item.intents.map((intent) => <li key={intent.id}>
              {intentSource(intent)}: {intentOutcome(intent)}
              {intent.decision?.reason && <> — “{intent.decision.reason}”</>}
            </li>)}</ul>}
          </li>)}</ol>}
      </section>
    </>}
  </div>;
}
