import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { SnapshotCodeLaunch } from '../src/SnapshotCodeLaunch';
import { ExternalCodePanel } from '../src/ExternalCodePanel';
import { WorkOrdersView } from '../src/WorkOrdersView';
import { getSnapshotCodeEligibility } from '../src/snapshotCodeApi';
import { snapshotCodeStatus } from '../src/snapshotCodeStatus';
import type { ExternalCodeDetail } from '../src/externalCodeApi';

const projectId = 'project-1';
const orderId = 'work-1';
const snapshotId = 'snapshot-1';
const stamp = '2026-09-27T12:00:00Z';
const sha = 'a'.repeat(64);
const revision = 'b'.repeat(40);
const tree = 'c'.repeat(40);
const snapshot = { snapshot_attempt_id: snapshotId, approval_id: 'approval-1', source_id: 'source-1',
  account_id: 'account-1', owner: 'fixture', repo: 'example', branch: 'main', saved_revision: revision,
  source_tree: tree, snapshot_path: `snapshots/${sha}.json`, snapshot_sha256: sha,
  snapshot_byte_size: 123, receipt_sha256: 'd'.repeat(64) };
const eligibility = { eligible: true, reason: null, snapshots: [snapshot] };
const run = { id: 'run-1', project_id: projectId, work_order_id: orderId, work_order_title: 'Implement feature',
  work_order_instructions: 'Use a separate order', profile_id: 'engineer-1', profile_model: null,
  status: 'running', mode: 'external_code', tokens_used: null, cost_usd: null,
  outcome_reason: '', started_at: stamp, finished_at: null };

function detail(key: string) {
  return { run, attempts: [{ id: 'attempt-1', project_id: projectId, run_id: run.id,
    adapter_id: 'codex_cli', fixture_id: 'github_snapshot_codex_fixture_v1', target_id: 'github_snapshot_codex_fixture_v1',
    source_id: null, source_root_id: null, source_relative_path: null, source_branch: null, source_revision: null,
    snapshot_attempt_id: snapshotId, idempotency_key: key, intent_sha256: 'e'.repeat(64),
    snapshot_source_id: snapshot.source_id, snapshot_account_id: snapshot.account_id,
    snapshot_owner: snapshot.owner, snapshot_repo: snapshot.repo, snapshot_branch: snapshot.branch,
    snapshot_revision: revision, snapshot_tree: tree, snapshot_path: snapshot.snapshot_path,
    snapshot_sha256: sha, snapshot_byte_size: 123, snapshot_request_sha256: 'f'.repeat(64),
    snapshot_receipt_sha256: snapshot.receipt_sha256,
    request_sha256: '1'.repeat(64), status: 'queued', reason_code: null, provider_session_id: null,
    baseline_commit: null, branch: null, commit: null, diff_sha256: null, tokens_used: null, cost_usd: null,
    receipt_sha256: null, created_at: stamp, started_at: null, finished_at: null, tests: [], artifacts: [] }] };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); });

it('rejects malformed snapshot eligibility instead of offering a launch', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json({ ...eligibility, snapshots: [{ ...snapshot, snapshot_sha256: 'bad' }] })));
  await expect(getSnapshotCodeEligibility(projectId, orderId)).rejects.toThrow('Invalid saved snapshot hash');
});

it('does not offer snapshot coding for a ready custom profile', async () => {
  const custom = { id: 'custom-1', project_id: projectId, name: 'Engineer', kind: 'custom' as const,
    reports_to_profile_id: null, model: null, instructions: '', memory_scope: 'project' as const, allowed_tools: [],
    autonomy_level: 'manual' as const, configured: false, permissions_active: false };
  const order = { id: orderId, project_id: projectId, title: 'Implement feature', instructions: 'Do work',
    profile_id: custom.id, status: 'ready', version: 1, block_reason: null,
    blocked_from_status: null, created_at: stamp };
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith('/work-orders')) return json([order]);
    if (path.endsWith('/runs')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<WorkOrdersView projectId={projectId} profiles={[custom]} onOpenRun={vi.fn()} />);
  expect(await screen.findByRole('heading', { name: 'Implement feature' })).toBeInTheDocument();
  expect(screen.queryByText('Diagnostics · managed synthetic Codex CLI run')).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => String(path).includes('github-snapshot-codex'))).toBe(false);
});

it('shows the exact filtered snapshot, launches once, and confirms the saved binding', async () => {
  let key = '';
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/github-snapshot-codex/eligibility')) return json(eligibility);
    if (path.endsWith('/github-snapshot-codex/runs') && init?.method === 'POST') {
      const body = JSON.parse(init.body as string) as { snapshot_attempt_id: string; idempotency_key: string };
      expect(body.snapshot_attempt_id).toBe(snapshotId);
      key = body.idempotency_key;
      return json(detail(key), 201);
    }
    if (path === `/api/projects/${projectId}/runs`) return json(key ? [run] : []);
    if (path.endsWith(`/runs/${run.id}/external-code`)) return json(detail(key));
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const openRun = vi.fn();
  const user = userEvent.setup();
  render(<SnapshotCodeLaunch projectId={projectId} orderId={orderId} onOpenRun={openRun} />);
  await user.click((await screen.findByText('Diagnostics · managed synthetic Codex CLI run')));
  await user.selectOptions(screen.getByRole('combobox', { name: 'Approved fake GitHub snapshot' }), snapshotId);
  expect(screen.getByText('Saved fixture revision selected.')).toBeInTheDocument();
  expect(screen.queryByText(revision)).not.toBeInTheDocument();
  expect(screen.queryByText(tree)).not.toBeInTheDocument();
  expect(screen.getByText(/The runner creates a separate local synthetic baseline commit/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Run managed synthetic Codex CLI' }));
  await waitFor(() => expect(openRun).toHaveBeenCalledWith(run.id));
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/github-snapshot-codex/runs'))).toHaveLength(1);
});

it('keeps a stored idempotency key after an unknown POST outcome and inspects saved state without resending', async () => {
  let readback = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/github-snapshot-codex/eligibility')) return json(eligibility);
    if (path.endsWith('/github-snapshot-codex/runs') && init?.method === 'POST') throw new Error('network lost');
    if (path === `/api/projects/${projectId}/runs`) return json(readback ? [run] : []);
    if (path.endsWith(`/runs/${run.id}/external-code`)) {
      const key = sessionStorage.getItem(`onefoundry.synthetic-snapshot-code.${projectId}.${orderId}.${snapshotId}`);
      return json(detail(key ?? ''));
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const openRun = vi.fn();
  const user = userEvent.setup();
  render(<SnapshotCodeLaunch projectId={projectId} orderId={orderId} onOpenRun={openRun} />);
  await user.click(await screen.findByText('Diagnostics · managed synthetic Codex CLI run'));
  await user.selectOptions(screen.getByRole('combobox', { name: 'Approved fake GitHub snapshot' }), snapshotId);
  await user.click(screen.getByRole('button', { name: 'Run managed synthetic Codex CLI' }));
  expect(await screen.findByText(/Launch outcome may be unknown/)).toBeInTheDocument();
  readback = true;
  await user.click(screen.getByRole('button', { name: 'Inspect saved launch' }));
  await waitFor(() => expect(openRun).toHaveBeenCalledWith(run.id));
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/github-snapshot-codex/runs'))).toHaveLength(1);
});

it.each([
  ['source', 'snapshot_source_id', 'other-source'],
  ['account', 'snapshot_account_id', 'other-account'],
  ['owner', 'snapshot_owner', 'other-owner'],
  ['repository', 'snapshot_repo', 'other-repo'],
  ['branch', 'snapshot_branch', 'other-branch'],
  ['size', 'snapshot_byte_size', 124],
  ['fetch receipt digest', 'snapshot_receipt_sha256', '9'.repeat(64)],
])('refuses a saved launch whose %s differs from the displayed snapshot', async (_label, field, value) => {
  let key = '';
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/github-snapshot-codex/eligibility')) return json(eligibility);
    if (path.endsWith('/github-snapshot-codex/runs') && init?.method === 'POST') {
      key = (JSON.parse(init.body as string) as { idempotency_key: string }).idempotency_key;
      const mismatch = detail(key) as unknown as { run: Record<string, unknown>; attempts: Record<string, unknown>[] };
      mismatch.attempts[0][field] = value;
      return json(mismatch, 201);
    }
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path.endsWith(`/runs/${run.id}/external-code`)) {
      const mismatch = detail(key) as unknown as { run: Record<string, unknown>; attempts: Record<string, unknown>[] };
      mismatch.attempts[0][field] = value;
      return json(mismatch);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const openRun = vi.fn();
  const user = userEvent.setup();
  render(<SnapshotCodeLaunch projectId={projectId} orderId={orderId} onOpenRun={openRun} />);
  await user.click(await screen.findByText('Diagnostics · managed synthetic Codex CLI run'));
  await user.selectOptions(screen.getByRole('combobox', { name: 'Approved fake GitHub snapshot' }), snapshotId);
  await user.click(screen.getByRole('button', { name: 'Run managed synthetic Codex CLI' }));
  expect(await screen.findByText(/launch response did not match the selected saved snapshot/i)).toBeInTheDocument();
  expect(openRun).not.toHaveBeenCalled();
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/github-snapshot-codex/runs'))).toHaveLength(1);
});

it('refuses navigation when the GET readback changes the displayed snapshot identity', async () => {
  let key = '';
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/github-snapshot-codex/eligibility')) return json(eligibility);
    if (path.endsWith('/github-snapshot-codex/runs') && init?.method === 'POST') {
      key = (JSON.parse(init.body as string) as { idempotency_key: string }).idempotency_key;
      return json(detail(key), 201);
    }
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path.endsWith(`/runs/${run.id}/external-code`)) {
      const mismatch = detail(key) as unknown as { run: Record<string, unknown>; attempts: Record<string, unknown>[] };
      mismatch.attempts[0].snapshot_owner = 'unexpected-owner';
      return json(mismatch);
    }
    throw new Error(`Unexpected path ${path}`);
  }));
  const openRun = vi.fn();
  const user = userEvent.setup();
  render(<SnapshotCodeLaunch projectId={projectId} orderId={orderId} onOpenRun={openRun} />);
  await user.click(await screen.findByText('Diagnostics · managed synthetic Codex CLI run'));
  await user.selectOptions(screen.getByRole('combobox', { name: 'Approved fake GitHub snapshot' }), snapshotId);
  await user.click(screen.getByRole('button', { name: 'Run managed synthetic Codex CLI' }));
  expect(await screen.findByText(/Saved coding attempt does not match every selected snapshot identity field/)).toBeInTheDocument();
  expect(openRun).not.toHaveBeenCalled();
});

it.each([
  ['queued', 'running', /execution or final outcome is not yet confirmed/i],
  ['unknown', 'unknown', /Codex CLI result is not confirmed/i],
])('does not claim Codex CLI ran for a %s synthetic attempt', async (attemptStatus, runStatus, expected) => {
  const saved = detail('key-1') as unknown as { run: Record<string, unknown>; attempts: Record<string, unknown>[] };
  saved.run.status = runStatus;
  saved.attempts[0].status = attemptStatus;
  vi.stubGlobal('fetch', vi.fn(async () => json(saved)));
  render(<ExternalCodePanel projectId={projectId} runId={run.id} onTimelineRefresh={vi.fn()} onDetailChange={vi.fn()} />);
  expect(await screen.findByText(expected)).toBeInTheDocument();
  expect(screen.queryByText(/receipt confirms the pinned Codex CLI executed/i)).not.toBeInTheDocument();
  expect(snapshotCodeStatus(saved as unknown as ExternalCodeDetail)).toMatch(expected);
});

it('shows the managed synthetic receipt with distinct GitHub revision, local commits, tests, and diff', async () => {
  const saved = detail('key-1') as unknown as { run: Record<string, unknown>; attempts: Record<string, unknown>[] };
  saved.run.status = 'done';
  saved.attempts[0].status = 'done';
  saved.attempts[0].provider_session_id = 'local-fake-session';
  saved.attempts[0].baseline_commit = '2'.repeat(40);
  saved.attempts[0].commit = '3'.repeat(40);
  saved.attempts[0].branch = 'fixture/snapshot-attempt-1';
  saved.attempts[0].tokens_used = 28;
  saved.attempts[0].tests = [
    { name: 'file_text_before', status: 'failed', exit_code: 1 },
    { name: 'file_text_after', status: 'passed', exit_code: 0 },
  ];
  saved.attempts[0].diff_sha256 = '4'.repeat(64);
  saved.attempts[0].artifacts = [{ id: 'artifact-1', kind: 'diff', path: 'artifacts/attempt-1/change.diff',
    sha256: '4'.repeat(64), byte_size: 12 }];
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith(`/runs/${run.id}/external-code`)) return json(saved);
    if (path.endsWith('/external-code/artifacts/artifact-1')) return json({ kind: 'diff',
      sha256: '4'.repeat(64), byte_size: 12, text: '+fixture edit\n' });
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<ExternalCodePanel projectId={projectId} runId={run.id} onTimelineRefresh={vi.fn()} onDetailChange={vi.fn()} />);
  expect(await screen.findByRole('heading', { name: 'Managed synthetic Codex CLI run' })).toBeInTheDocument();
  expect(screen.getByText('Saved fake GitHub snapshot')).toBeInTheDocument();
  expect(screen.queryByText('2'.repeat(40))).not.toBeInTheDocument();
  expect(screen.getByText(/Changes recorded/)).toBeInTheDocument();
  expect(screen.getByText('file_text_before: failed · exit 1')).toBeInTheDocument();
  expect(screen.getByText('file_text_after: passed · exit 0')).toBeInTheDocument();
  expect(screen.getByText('Synthetic usage: 28 fixture tokens · no live billing evidence')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Preview diff' }));
  expect(await screen.findByLabelText('Verified synthetic snapshot diff')).toHaveTextContent('+fixture edit');
});
