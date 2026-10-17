import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ApprovalInbox } from '../src/ApprovalInbox';
import type { Approval, Run } from '../src/workApi';

const projectId = 'project-1';
const root = `/api/projects/${projectId}`;
const run: Run = { id: 'run-1', project_id: projectId, work_order_id: 'work-1',
  work_order_title: 'Review PR', work_order_instructions: 'Review', profile_id: 'role-1',
  profile_model: null, status: 'running', mode: 'synthetic_text', tokens_used: 0,
  cost_usd: 0, outcome_reason: '', started_at: '2026-09-28T12:00:00Z', finished_at: null };
const approval: Approval = { id: 'approval-1', project_id: projectId, work_order_id: run.work_order_id,
  run_id: run.id, action_type: 'other', target: 'Comment on GitHub PR #7 in team/repo (commits may change before posting)',
  content: JSON.stringify({ action: 'github_pr_comment_v1', comment: 'Please check the error path.' }),
  proposal_digest: 'a'.repeat(64), status: 'pending', created_at: run.started_at, decided_at: null,
  synthetic_merge: false, github_comment: true };

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('shows the exact whole-PR text and requires status check and separate one-shot dispatch', async () => {
  let saved = { ...approval };
  let commentStatus: Record<string, unknown> = { approval_id: approval.id, status: 'not_attempted', retry_allowed: false };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `${root}/runs`) return json([run]);
    if (path.startsWith(`${root}/approvals?status=`)) {
      const status = new URL(`http://localhost${path}`).searchParams.get('status');
      return json(saved.status === status ? [saved] : []);
    }
    if (path === `${root}/approvals/${approval.id}/decision`) {
      saved = { ...saved, status: 'approved', decided_at: run.started_at };
      return json(saved);
    }
    if (path === `${root}/github-pr-comments/${approval.id}`) return json(commentStatus);
    if (path === `${root}/github-pr-comments/${approval.id}/dispatch` && init?.method === 'POST') {
      commentStatus = { approval_id: approval.id, status: 'unknown', retry_allowed: false,
        reason_code: 'post_ambiguous', comment_id: null };
      return json(commentStatus);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ApprovalInbox projectId={projectId} />);
  expect(await screen.findByText('Please check the error path.')).toBeInTheDocument();
  expect(screen.getAllByText(/commits may change before posting/i).length).toBeGreaterThan(0);
  await user.click(screen.getByRole('button', { name: 'Approve exact PR comment only' }));
  expect(fetcher.mock.calls.filter(([path]) => String(path).endsWith('/dispatch'))).toHaveLength(0);
  await user.click(screen.getByRole('button', { name: 'approved' }));
  expect(await screen.findByRole('button', { name: 'Check PR comment status' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Post exact comment once' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Check PR comment status' }));
  await user.click(await screen.findByRole('button', { name: 'Post exact comment once' }));
  expect(await screen.findByText(/Saved PR comment status: unknown/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Post exact comment once' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path]) => String(path).endsWith('/dispatch'))).toHaveLength(1);
});

it('creates an exact proposal from a saved GitHub source and confirms it in the inbox', async () => {
  let saved: Approval | null = null;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `${root}/runs`) return json([run]);
    if (path.startsWith(`${root}/approvals?status=`)) return json(saved ? [saved] : []);
    if (path === `${root}/sources`) return json([{ id: 'source-1', project_id: projectId, kind: 'github',
      display_name: 'team/repo', root_id: null, relative_path: null, account_id: 'account-1',
      owner: 'team', repo: 'repo', branch: 'main', revision: 'a'.repeat(40), availability: 'available' }]);
    if (path === `${root}/github-pr-comments` && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      expect(body).toMatchObject({ run_id: run.id, source_id: 'source-1', number: 7,
        comment: 'Please check the error path.' });
      expect(typeof body.idempotency_key).toBe('string');
      saved = { ...approval };
      return json({ approval_id: approval.id, status: 'pending' }, 201);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); render(<ApprovalInbox projectId={projectId} />);
  await user.click(screen.getByRole('button', { name: 'Prepare GitHub PR comment' }));
  await user.selectOptions(await screen.findByLabelText('Running role run'), run.id);
  await user.selectOptions(screen.getByLabelText('Saved GitHub source'), 'source-1');
  await user.type(screen.getByLabelText('PR number'), '7');
  await user.type(screen.getByLabelText('Exact comment text'), 'Please check the error path.');
  await user.click(screen.getByRole('button', { name: 'Save PR comment proposal' }));
  expect(await screen.findByText('Exact PR comment proposal saved. No comment was posted.')).toBeInTheDocument();
  await waitFor(() => expect(fetcher.mock.calls.filter(([path]) => String(path) === `${root}/github-pr-comments`)).toHaveLength(1));
  expect(fetcher.mock.calls.filter(([path]) => String(path).endsWith('/dispatch'))).toHaveLength(0);
});

it('offers one readback for an unreconciled attempt without offering another POST', async () => {
  const saved: Approval = { ...approval, status: 'approved', decided_at: run.started_at };
  let commentStatus: Record<string, unknown> = { approval_id: approval.id, status: 'unknown',
    reason_code: 'readback_pending', retry_allowed: false };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `${root}/runs`) return json([run]);
    if (path.startsWith(`${root}/approvals?status=`)) return json(path.endsWith('approved') ? [saved] : []);
    if (path === `${root}/github-pr-comments/${approval.id}`) return json(commentStatus);
    if (path === `${root}/github-pr-comments/${approval.id}/reconcile` && init?.method === 'POST') {
      commentStatus = { approval_id: approval.id, status: 'done', reason_code: 'readback_matched',
        comment_id: 123, retry_allowed: false };
      return json(commentStatus);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); render(<ApprovalInbox projectId={projectId} />);
  await user.click(screen.getByRole('button', { name: 'approved' }));
  await user.click(await screen.findByRole('button', { name: 'Check PR comment status' }));
  expect(screen.queryByRole('button', { name: 'Post exact comment once' })).not.toBeInTheDocument();
  await user.click(await screen.findByRole('button', { name: 'Read back acknowledged comment once' }));
  expect(await screen.findByText(/Saved PR comment status: done/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Read back acknowledged comment once' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path]) => String(path).endsWith('/dispatch'))).toHaveLength(0);
  expect(fetcher.mock.calls.filter(([path]) => String(path).endsWith('/reconcile'))).toHaveLength(1);
});
