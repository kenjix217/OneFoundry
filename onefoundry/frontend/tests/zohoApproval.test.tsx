import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApprovalInbox } from '../src/ApprovalInbox';

afterEach(() => vi.unstubAllGlobals());

it.each(['draft', 'send'] as const)('describes the %s side effect before approval and reads the receipt', async (operation) => {
  let approval = { id: 'approval-1', project_id: 'project-1', work_order_id: 'order-1', run_id: 'run-1',
    action_type: 'email', target: 'buyer@example.test', content: `Zoho Mail: ${operation}\nReviewed body`,
    proposal_digest: 'digest', status: 'pending', created_at: '2026-10-13T12:00:00Z', decided_at: null as string | null,
    synthetic_merge: false, zoho_mail_operation: operation };
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    calls.push(path);
    let value: unknown = [];
    if (path.endsWith('/decision') && init?.method === 'POST') {
      approval = { ...approval, status: 'approved', decided_at: '2026-10-13T12:01:00Z' }; value = approval;
    } else if (path.includes('/approvals')) value = [approval];
    else if (path.endsWith('/mail/zoho/actions')) value = [{ approval_id: approval.id, state: 'accepted' }];
    return new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
  }));
  render(<ApprovalInbox projectId="project-1" />);
  const label = operation === 'draft' ? 'Approve and save Zoho draft' : 'Approve and send Zoho email once';
  expect(await screen.findByRole('button', { name: label })).toBeInTheDocument();
  expect(screen.queryByText(/SMTP submission/)).not.toBeInTheDocument();
  await userEvent.setup().click(screen.getByRole('button', { name: label }));
  expect(await screen.findByText(operation === 'draft' ? 'Zoho accepted the draft request. Nothing was sent.' :
    'Zoho accepted the email. Recipient delivery is not confirmed.')).toBeInTheDocument();
  expect(calls.filter((path) => path.endsWith('/decision'))).toHaveLength(1);
  expect(calls).toContain('/api/projects/project-1/mail/zoho/actions');
});
