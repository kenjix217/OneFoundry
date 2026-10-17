import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { Profile } from '../src/api';
import { AutomationView } from '../src/AutomationView';
import { listRules } from '../src/coordinationApi';

const base = '/api/projects/project-1/coordination';
const stamp = '2026-09-30T12:00:00Z';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function profile(id: string, name: string): Profile {
  return { id, project_id: 'project-1', kind: 'builtin', name, reports_to_profile_id: null, model: null, instructions: '',
    memory_scope: 'company', allowed_tools: [], autonomy_level: 'manual', configured: true, permissions_active: false };
}

const profiles = [profile('engineer', 'Engineer'), profile('qa', 'QA')];
const types = { 'work_order.done': ['profile_id', 'profile_name', 'deliverable_kind', 'origin_rule_id'], 'schedule.fired': [],
  'approval.approved': ['action_type', 'profile_id', 'profile_name'] };

afterEach(() => vi.unstubAllGlobals());

it('reads an agent run as the saved rule origin', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json([{ id: 'rule-run', name: 'Hourly brief', enabled: true,
    version: 1, current_version: 1, source: 'schedule', signal_type: 'schedule.fired',
    schedule: { kind: 'interval', every_hours: 1 }, filters: {}, target_profile_id: 'qa',
    title: 'Write brief', instructions: 'Summarize.', deliverable_kind: 'operator_review',
    acceptance_checks: [], autonomy: 'draft', max_per_day: 10, max_chain_depth: null,
    created_at: stamp, updated_at: stamp,
    last_agent_action: { turn_id: null, run_id: 'run-rule', actor_id: 'profile:qa',
      decision: 'create_rule', rule_version: 1, source_kind: 'agent_run' } }])));
  const rules = await listRules('project-1');
  expect(rules[0].last_agent_action).toMatchObject({ run_id: 'run-rule', source_kind: 'agent_run' });
});

it('creates a rule, pauses, and explains what each event did', async () => {
  let status = { mode: 'draft_only', max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', paused: false, agent_rules_enabled: true, version: 0, signal_types: types, profile_tool_ceiling: [] as string[], registered_tools: ['approved_memory_v1', 'report_signal_v1'] };
  const rules: Record<string, unknown>[] = [];
  const posted: Record<string, unknown>[] = [];
  const signals = [
    { id: 'signal-1', source: 'internal', signal_type: 'work_order.done', fields: { title: 'Build the change', profile_name: 'Engineer' },
      origin_work_order_id: 'order-1', origin_run_id: null, chain_depth: 0, state: 'matched', reason: null, created_at: stamp,
      processed_at: stamp, intents: [{ id: 'intent-1', action_kind: 'rule_work_order', rule_id: 'rule-1', rule_name: 'Engineering to QA',
        rule_version: 1, state: 'done', autonomy: 'draft', error_code: null, work_order_id: 'order-2', work_order_title: 'Verify the change' }] },
    { id: 'signal-2', source: 'internal', signal_type: 'work_order.done', fields: { title: 'Loop step', profile_name: 'QA' },
      origin_work_order_id: 'order-3', origin_run_id: null, chain_depth: 10, state: 'blocked', reason: 'chain_depth_exceeded',
      created_at: stamp, processed_at: stamp, intents: [] },
  ];
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === base) return json(status);
    if (url === `${base}/tool-ceiling` && init?.method === 'PUT') {
      const body = JSON.parse(init.body as string) as { tools: string[] };
      status = { ...status, profile_tool_ceiling: body.tools, version: status.version + 1 }; return json(status);
    }
    if (url === `${base}/agent-rule-access` && init?.method === 'PUT') {
      const body = JSON.parse(init.body as string) as { enabled: boolean; expected_version: number };
      if (body.expected_version !== status.version) return json({ detail: 'stale' }, 409);
      status = { ...status, agent_rules_enabled: body.enabled, version: status.version + 1 };
      return json(status);
    }
    if (url === `${base}/pause` && init?.method === 'POST') { status = { ...status, paused: true, version: 1 }; return json(status); }
    if (url === `${base}/rules` && init?.method === 'POST') {
      const fields = JSON.parse(init.body as string) as Record<string, unknown>;
      posted.push(fields);
      const created = { ...fields, id: 'rule-1', enabled: true, version: 1, current_version: 1, created_at: stamp, updated_at: stamp };
      rules.push(created); return json(created, 201);
    }
    if (url === `${base}/rules`) return json(rules);
    if (url === '/api/projects/project-1/mail/accounts') return json([]);
    if (url === '/api/projects/project-1/connectors') return json([]);
    if (url === '/api/projects/project-1/documents/sources') return json([]);
    if (url === `${base}/signals?limit=50`) return json(signals);
    throw new Error(`Unexpected ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<AutomationView projectId="project-1" profiles={profiles} />);

  await user.click(await screen.findByRole('button', { name: 'Controls & limits' }));
  expect(await screen.findByText(/Drafts only\. Rules create draft work orders/)).toBeInTheDocument();
  expect(screen.getByText(/Event chain limit: 10 steps/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Controls & limits' }));
  await user.click(screen.getByRole('button', { name: 'Turn off agent rule management' }));
  expect(await screen.findByText('Agent rule management is off.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Allow agents to manage rules' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Recent events' }));
  expect(screen.getByText('Engineering to QA: created “Verify the change”')).toBeInTheDocument();
  expect(screen.getByText(/Stopped: event chain limit reached/)).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: 'Rules' }));
  await user.click(screen.getByRole('button', { name: 'Rules' }));
  await user.type(screen.getByLabelText('Rule name'), 'Engineering to QA');
  await user.selectOptions(screen.getByLabelText('Only for this agent (optional)'), 'Engineer');
  await user.selectOptions(screen.getByLabelText('Assign new work to'), 'qa');
  await user.type(screen.getByLabelText('Work order title'), 'Verify the change');
  await user.type(screen.getByLabelText(/^Instructions/), 'Check it.');
  await user.clear(screen.getByLabelText(/^Most automatic model attempts per day/));
  await user.type(screen.getByLabelText(/^Most automatic model attempts per day/), '2');
  await user.click(screen.getByLabelText('Use quiet hours'));
  expect(screen.getByLabelText('Quiet hours start')).toHaveValue('22:00');
  await user.click(screen.getByRole('button', { name: 'Create rule' }));
  expect(await screen.findByText('Rule created.')).toBeInTheDocument();
  expect(posted[0]).toMatchObject({ signal_type: 'work_order.done', filters: { profile_name: 'Engineer' },
    target_profile_id: 'qa', autonomy: 'draft', max_per_day: 10, max_chain_depth: null, schedule: null,
    max_model_attempts: 2, quiet_hours: { start: '22:00', end: '08:00' } });
  const list = screen.getByRole('region', { name: 'Automation rules' });
  expect(within(list).getByText(/When a work order is completed for Engineer, create “Verify the change” for QA as a draft/)).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: 'Controls & limits' }));
  await user.click(screen.getByRole('button', { name: 'Controls & limits' }));
  await user.click(screen.getByRole('checkbox', { name: 'approved memory' }));
  await user.click(screen.getByRole('button', { name: 'Save tool limit' }));
  expect(await screen.findByText('Proposed agents may request: approved memory.')).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: 'Pause automation' }));
  expect(await screen.findByText('Automation paused. New events wait until you resume.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Resume automation' })).toBeInTheDocument();
});

it('creates a weekly schedule rule and describes it in the configured time zone', async () => {
  const status = { mode: 'draft_only', max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'America/New_York', paused: false, agent_rules_enabled: true,
    version: 0, signal_types: types, profile_tool_ceiling: [], registered_tools: [] };
  const rules: Record<string, unknown>[] = [];
  const posted: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === base) return json(status);
    if (url === `${base}/rules` && init?.method === 'POST') {
      const fields = JSON.parse(init.body as string) as Record<string, unknown>;
      posted.push(fields);
      const created = { ...fields, id: 'rule-2', enabled: true, version: 1, current_version: 1, created_at: stamp, updated_at: stamp };
      rules.push(created); return json(created, 201);
    }
    if (url === `${base}/rules`) return json(rules);
    if (url === '/api/projects/project-1/mail/accounts') return json([]);
    if (url === '/api/projects/project-1/connectors') return json([]);
    if (url === '/api/projects/project-1/documents/sources') return json([]);
    if (url === `${base}/signals?limit=50`) return json([]);
    throw new Error(`Unexpected ${url}`);
  }));
  const user = userEvent.setup();
  render(<AutomationView projectId="project-1" profiles={profiles} />);
  await user.selectOptions(await screen.findByLabelText('When'), 'schedule.fired');
  await user.selectOptions(screen.getByLabelText('Repeat'), 'weekly');
  await user.selectOptions(screen.getByLabelText('Day'), '0');
  expect(screen.queryByLabelText('Only for this agent (optional)')).not.toBeInTheDocument();
  await user.type(screen.getByLabelText('Rule name'), 'Monday brief');
  await user.type(screen.getByLabelText('Work order title'), 'Weekly research brief');
  await user.type(screen.getByLabelText(/^Instructions/), 'Summarize the week.');
  await user.click(screen.getByRole('button', { name: 'Create rule' }));
  expect(await screen.findByText('Rule created.')).toBeInTheDocument();
  expect(posted[0]).toMatchObject({ signal_type: 'schedule.fired', schedule: { kind: 'weekly', at: '09:00', weekday: 0 }, filters: {} });
  expect(screen.getByText(/On a schedule every Monday at 09:00 \(America\/New_York\), create “Weekly research brief”/)).toBeInTheDocument();
});

it('shows findings and what the Chief of Staff decided', async () => {
  const status = { mode: 'draft_only', max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', paused: false, agent_rules_enabled: true, version: 0,
    signal_types: { ...types, 'agent.finding': ['topic', 'profile_id', 'profile_name'] }, profile_tool_ceiling: [], registered_tools: [] };
  const signals = [
    { id: 'finding-1', source: 'agent_finding', signal_type: 'agent.finding', fields: { topic: 'lead', summary: 'Springfield plans a pilot.' },
      origin_work_order_id: 'order-1', origin_run_id: 'run-1', chain_depth: 0, state: 'blocked', reason: 'ask_operator', created_at: stamp,
      processed_at: stamp, intents: [{ id: 'intent-9', action_kind: 'chief_triage', rule_id: null, rule_name: null, rule_version: null,
        state: 'blocked', autonomy: null, error_code: 'ask_operator', decision: { action: 'ask_operator', reason: 'Pricing needs your call.' },
        work_order_id: null, work_order_title: null }] },
  ];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === base) return json(status);
    if (url === `${base}/rules`) return json([]);
    if (url === '/api/projects/project-1/mail/accounts') return json([]);
    if (url === '/api/projects/project-1/connectors') return json([]);
    if (url === '/api/projects/project-1/documents/sources') return json([]);
    if (url === `${base}/signals?limit=50`) return json(signals);
    throw new Error(`Unexpected ${url}`);
  }));
  const user = userEvent.setup();
  render(<AutomationView projectId="project-1" profiles={profiles} />);
  await user.click(await screen.findByRole('button', { name: 'Recent events' }));
  expect(await screen.findByText('Finding (lead): “Springfield plans a pilot.”')).toBeInTheDocument();
  expect(screen.getByText(/Chief of Staff triage: Chief of Staff asks you to decide/)).toBeInTheDocument();
  expect(screen.getByText(/Pricing needs your call\./)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Rules' }));
  await user.selectOptions(screen.getByLabelText('When'), 'agent.finding');
  expect(screen.getByLabelText('Only this topic (optional)')).toBeInTheDocument();
});

it('describes automatic starts and their outcomes in Auto mode', async () => {
  const status = { mode: 'auto', max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC',
    paused: false, agent_rules_enabled: true, version: 0, signal_types: types, profile_tool_ceiling: [], registered_tools: [] };
  const rules = [{ id: 'rule-3', name: 'Research after engineering', enabled: true, version: 1, current_version: 1, source: 'internal',
    signal_type: 'work_order.done', schedule: null, filters: { profile_name: 'Engineer' }, target_profile_id: 'qa', title: 'Research it',
    instructions: 'Summarize.', deliverable_kind: 'operator_review', acceptance_checks: [], autonomy: 'auto_start', max_per_day: 10,
    max_chain_depth: null, created_at: stamp, updated_at: stamp }];
  const intent = (id: string, kind: string, state: string, error: string | null) => ({ id, action_kind: kind, rule_id: 'rule-3',
    rule_name: 'Research after engineering', rule_version: 1, state, autonomy: kind === 'auto_start' ? null : 'auto_start',
    error_code: error, decision: null, work_order_id: 'order-9', work_order_title: 'Research it' });
  const signals = [{ id: 'signal-9', source: 'internal', signal_type: 'work_order.done', fields: { title: 'Ship', profile_name: 'Engineer' },
    origin_work_order_id: 'order-8', origin_run_id: null, chain_depth: 0, state: 'matched', reason: null, created_at: stamp,
    processed_at: stamp, intents: [intent('i-1', 'rule_work_order', 'done', null), intent('i-2', 'auto_start', 'blocked', 'auto_start_daily_limit')] }];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === base) return json(status);
    if (url === `${base}/rules`) return json(rules);
    if (url === '/api/projects/project-1/mail/accounts') return json([]);
    if (url === '/api/projects/project-1/connectors') return json([]);
    if (url === '/api/projects/project-1/documents/sources') return json([]);
    if (url === `${base}/signals?limit=50`) return json(signals);
    throw new Error(`Unexpected ${url}`);
  }));
  render(<AutomationView projectId="project-1" profiles={profiles} />);
  expect(await screen.findByText(/create “Research it” for QA and start it automatically/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Controls & limits' }));
  expect(screen.getByText(/20 automatic starts per project/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Recent events' }));
  expect(screen.getByText(/Automatic start: Not started: the project's daily auto-start limit was reached/)).toBeInTheDocument();
});
