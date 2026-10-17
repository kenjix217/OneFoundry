import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DocumentActions } from '../src/DocumentActions';
import { DocumentSearch } from '../src/DocumentSearch';
import type { DocumentSource } from '../src/documentApi';

const base = '/api/projects/test/documents';
const source: DocumentSource = { id: 'folder', connector_account_id: 'account', provider: 'gdrive', label: 'Budget', folder_ref: 'F', folder_name: 'Budget', enabled: false, status: 'ready', last_error_code: null, last_poll_at: null, version: 1 };
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
afterEach(() => vi.unstubAllGlobals());

it('searches only the selected folder and reads an exact range', async () => {
  const posted: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    posted.push(JSON.parse(String(init?.body)));
    if (url === `${base}/search`) return json({ files: [{ file_id: 'File', name: 'Budget.xlsx', revision: 'v1' }], truncated: true });
    if (url === `${base}/range`) return json({ values: [[12, 'Ready']] });
    throw new Error('unexpected');
  }));
  const user = userEvent.setup();
  render(<DocumentSearch projectId="test" sources={[source]} />);
  await user.selectOptions(screen.getByLabelText('Search folder'), 'folder');
  await user.type(screen.getByLabelText('File name contains'), 'Budget');
  await user.click(screen.getByRole('button', { name: 'Search files' }));
  expect(await screen.findByText('Budget.xlsx')).toBeInTheDocument();
  expect(screen.getByText(/configured limit/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Read cells' }));
  await user.type(screen.getByLabelText('Worksheet name'), 'Summary');
  await user.clear(screen.getByLabelText('Cell range'));
  await user.type(screen.getByLabelText('Cell range'), 'A1:B1');
  await user.click(screen.getByRole('button', { name: 'Read exact range' }));
  expect(await screen.findByRole('table', { name: 'Cell values' })).toHaveTextContent('12Ready');
  expect(posted).toEqual([{ source_id: 'folder', query: 'Budget' }, { source_id: 'folder', file_id: 'File', sheet: 'Summary', address: 'A1:B1' }]);
});

it('dispatches only approved actions and removes the button after an uncertain result', async () => {
  let state = 'approved';
  let sends = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const row = { id: 'action', file_name: 'Budget.xlsx', file_id: 'File', operation: 'edit_cells', state, error_code: state === 'unknown' ? 'readback_failed' : null, readback: null };
    if (url === `${base}/actions/action/dispatch`) { sends++; state = 'unknown'; return json({ ...row, state }); }
    if (url === `${base}/actions`) return json([row]);
    throw new Error('unexpected');
  }));
  const user = userEvent.setup();
  render(<DocumentActions projectId="test" />);
  await user.click(await screen.findByRole('button', { name: 'Apply approved change once' }));
  expect(await screen.findByText(/Outcome unknown; inspect/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Apply approved change once' })).not.toBeInTheDocument();
  expect(sends).toBe(1);
});
