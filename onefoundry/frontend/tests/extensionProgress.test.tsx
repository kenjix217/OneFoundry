import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { ExtensionProgress } from '../src/ExtensionProgress';
import { parseExtensionProgress } from '../src/extensionProgressApi';

const row = { extension_key: 'aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa', payload: { original_work_order_id: 'work-1' },
  stage: 'approval_pending', title: 'Activation needs your approval',
  message: 'Review the exact release and rollback plan in the inbox.',
  commit: 'b'.repeat(40), diff_sha256: 'c'.repeat(64) };
afterEach(() => { vi.unstubAllGlobals(); });
it('shows readable saved progress without technical identifiers or writes', async () => {
  const fetcher = vi.fn<(path: string, init?: RequestInit) => Promise<Response>>(async () => new Response(JSON.stringify([row]), { status: 200 }));
  vi.stubGlobal('fetch', fetcher);
  render(<ExtensionProgress projectId="project-1" orderId="work-1" />);
  await waitFor(() => expect(screen.getByText(row.title)).toBeTruthy());
  expect(screen.queryByText(row.commit)).toBeNull();
  expect(screen.queryByText(row.diff_sha256)).toBeNull();
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0]?.[1]?.method ?? 'GET').toBe('GET');
});
it('accepts empty progress and rejects foreign tasks, duplicate causes and unknown stages', () => {
  expect(parseExtensionProgress([], 'work-1')).toEqual([]);
  expect(() => parseExtensionProgress([row], 'work-2')).toThrow('invalid extension progress');
  expect(() => parseExtensionProgress([row, row], 'work-1')).toThrow('invalid extension progress');
  expect(() => parseExtensionProgress([{ ...row, stage: 'done' }], 'work-1')).toThrow();
});
it('reports readback failure without showing success', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('unavailable', { status: 503 })));
  render(<ExtensionProgress projectId="project-1" orderId="work-1" />);
  await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Could not confirm'));
  expect(screen.queryByText(row.title)).toBeNull();
});

it('requires an explicit discard decision and preserves its identity across an uncertain response', async () => {
  const { fireEvent } = await import('@testing-library/react');
  const candidate = { attempt_id: '11111111-1111-4111-8111-111111111111',
    run_id: '22222222-2222-4222-8222-222222222222', request_sha256: 'a'.repeat(64), receipt_sha256: 'b'.repeat(64) };
  const unknown = { ...row, stage: 'unknown', title: 'Outcome needs reconciliation', abandonment: candidate };
  const writes: Record<string, unknown>[] = [];
  const fetcher = vi.fn(async (_path: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      writes.push(body);
      if (writes.length === 1) return new Response('unavailable', { status: 503 });
      return new Response(JSON.stringify({ ...body, ...candidate, status: 'unknown' }), { status: 200 });
    }
    return new Response(JSON.stringify([writes.length > 1 ? { ...row, stage: 'coding_abandoned', title: 'Unverified build discarded' } : unknown]), { status: 200 });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<ExtensionProgress projectId="project-1" orderId="work-1" />);
  const button = await screen.findByRole('button', { name: 'Discard unverified build' });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Stopped worker; discarded disposable changes.' } });
  fireEvent.click(button);
  await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('same decision'));
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true);
  fireEvent.click(button);
  await screen.findByText('Unverified build discarded');
  expect(writes).toHaveLength(2);
  expect(writes[0]).toEqual(writes[1]);
  expect(writes[0]).toMatchObject({ decision: 'abandon_unverified', disposable_work_discarded: true });
});

it('rejects recovery controls for completed, foreign or malformed receipts', () => {
  expect(() => parseExtensionProgress([{ ...row, abandonment: {} }], 'work-1')).toThrow();
  expect(() => parseExtensionProgress([{ ...row, stage: 'unknown', abandonment: { attempt_id: '../foreign' } }], 'work-1')).toThrow();
});
