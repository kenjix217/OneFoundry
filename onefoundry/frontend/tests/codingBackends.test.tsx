import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { CodingBackendsView } from '../src/CodingBackendsView';

const projectId = 'project-1';
const engineer = { id: 'engineer-1', project_id: projectId, kind: 'builtin' as const, name: 'Engineer',
  reports_to_profile_id: null, model: null, model_account_id: null, instructions: 'Build carefully', memory_scope: 'company' as const,
  allowed_tools: [], autonomy_level: 'manual' as const, configured: false, permissions_active: false };
const order = { id: 'order-1', project_id: projectId, title: 'Repair parser', instructions: 'Fix edge case',
  profile_id: engineer.id, status: 'ready', version: 2, block_reason: null, blocked_from_status: null,
  created_at: '2026-09-26T00:00:00Z' };
const run = { id: 'run-1', project_id: projectId, work_order_id: order.id, work_order_title: order.title,
  work_order_instructions: order.instructions, profile_id: engineer.id, profile_model: null,
  status: 'unknown', mode: 'external_code', tokens_used: null, cost_usd: null,
  outcome_reason: 'Receipt missing', started_at: '2026-09-26T00:00:00Z', finished_at: null };
const source = { id: 'source-1', project_id: projectId, kind: 'local', display_name: 'parser',
  root_id: 'workspace', relative_path: 'parser', account_id: null, owner: null, repo: null,
  branch: 'main', revision: 'a'.repeat(40), availability: 'verified_at_selection' };
function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
afterEach(() => vi.unstubAllGlobals());

it('labels a recorded offline probe separately from live coding backends', async () => {
  const attempt = { id: 'attempt-1', project_id: projectId, run_id: run.id, adapter_id: 'offline_probe',
    fixture_id: 'local_source_probe_v1', target_id: 'local_source_probe_v1', source_id: source.id,
    source_root_id: 'workspace', source_relative_path: 'parser', source_branch: 'main', source_revision: source.revision,
    request_sha256: 'b'.repeat(64), status: 'unknown', reason_code: 'receipt_invalid', provider_session_id: null,
    baseline_commit: source.revision, branch: 'main', commit: null, diff_sha256: null, tokens_used: null,
    cost_usd: null, receipt_sha256: null, created_at: run.started_at, started_at: null, finished_at: null,
    tests: [], artifacts: [] };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/work-orders')) return json([order]);
    if (path.endsWith('/runs')) return json([run]);
    if (path.endsWith('/sources')) return json([source]);
    if (path.endsWith('/external-code/eligibility')) return json({ eligible: false, reason: 'No coding provider', adapters: [] });
    if (path.endsWith(`/runs/${run.id}/external-code`)) return json({ run, attempts: [attempt] });
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<CodingBackendsView projectId={projectId} profiles={[engineer]} onNavigate={vi.fn()} />);
  expect(await screen.findByText('Offline local source probe · no coding provider')).toBeInTheDocument();
});

it('does not imply backend readiness without a ready Engineer work order', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith('/work-orders') || path.endsWith('/runs') || path.endsWith('/sources')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const navigate = vi.fn(); const user = userEvent.setup();
  render(<CodingBackendsView projectId={projectId} profiles={[engineer]} onNavigate={navigate} />);
  expect(await screen.findByText('No ready Engineer work order is available to check. Prepare one in Work orders.')).toBeInTheDocument();
  expect(screen.getAllByText('Not checked')).toHaveLength(2);
  expect(fetcher.mock.calls.some(([path]) => String(path).includes('/external-code/eligibility'))).toBe(false);
  await user.click(screen.getByRole('button', { name: 'Open Work orders' }));
  expect(navigate).toHaveBeenCalledWith('work');
});

it('shows selected source name without hashes, eligibility reasons, and Activity receipt link', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith('/work-orders')) return json([order]);
    if (path.endsWith('/runs')) return json([run]);
    if (path.endsWith('/sources')) return json([source]);
    if (path.endsWith('/external-code/eligibility')) return json({ eligible: false, reason: 'No coding adapter is currently ready.', adapters: [
      { adapter_id: 'codex_cli', available: false, reason: 'isolation_not_proven' },
      { adapter_id: 'claude_code', available: false, reason: 'authentication unavailable' },
    ] });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const navigate = vi.fn(); const user = userEvent.setup();
  render(<CodingBackendsView projectId={projectId} profiles={[engineer]} onNavigate={navigate} />);
  expect(await screen.findByText('isolation_not_proven')).toBeInTheDocument();
  expect(screen.getByText('authentication unavailable')).toBeInTheDocument();
  expect(screen.getByText(/Local · main/)).toBeInTheDocument();
  expect(screen.queryByText(/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/)).not.toBeInTheDocument();
  expect(screen.getByText(/Offline source probes bind one saved local source and its exact revision/)).toBeInTheDocument();
  const receipts = screen.getByRole('heading', { name: 'Coding receipts' }).closest('section')!;
  expect(within(receipts).getByText('Unknown outcome; reconcile in Activity')).toBeInTheDocument();
  await user.click(within(receipts).getByRole('button', { name: 'Open run in Activity' }));
  expect(navigate).toHaveBeenCalledWith('activity', run.id);
  await user.click(screen.getByRole('button', { name: 'Open repositories' }));
  expect(navigate).toHaveBeenCalledWith('integrations');
  expect(fetcher.mock.calls.filter(([path]) => String(path).includes('/external-code/eligibility'))).toHaveLength(1);
});
