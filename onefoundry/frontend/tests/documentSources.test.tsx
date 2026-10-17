import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { DocumentSources } from '../src/DocumentSources';
import type { Connector } from '../src/connectorApi';

const path = '/api/projects/project-1/documents/sources';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('watches a Google Drive folder from a link and checks it before turning it on', async () => {
  const connectors: Connector[] = [{ id: 'g-1', provider: 'google', label: 'Workspace', client_id: 'x', tenant: 'common',
    features: ['files'], status: 'connected', last_error_code: null, granted_scopes: '', version: 1 }];
  let source: Record<string, unknown> | null = null;
  const posted: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === path && init?.method === 'POST') {
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      posted.push(body);
      source = { id: 'd-1', connector_account_id: 'g-1', provider: 'gdrive', label: body.label, folder_ref: body.folder_ref, folder_id: null,
        folder_name: null, enabled: false, status: 'unverified', last_error_code: null, last_probe_at: null, last_poll_at: null, version: 1,
        created_at: '2026-10-01T00:00:00Z' };
      return json(source, 201);
    }
    if (url === path) return json(source ? [source] : []);
    if (url === '/api/projects/project-1/documents/writes') return json([]);
    if (url === '/api/projects/project-1/documents/actions') return json([]);
    if (url === `${path}/d-1/probe`) { source = { ...source, status: 'ready', folder_name: 'Pipeline', version: 2 }; return json(source); }
    if (url === `${path}/d-1/enabled`) { source = { ...source, enabled: true, version: 3 }; return json(source); }
    throw new Error(`Unexpected ${url}`);
  }));
  const user = userEvent.setup();
  render(<DocumentSources projectId="project-1" connectors={connectors} />);
  expect(await screen.findByText('No folders watched.')).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('Drive'), 'gdrive');
  await user.selectOptions(screen.getByLabelText('Account'), 'g-1');
  await user.type(screen.getByLabelText('Name'), 'Pipeline');
  await user.type(screen.getByLabelText('Folder'), 'https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp');
  await user.click(screen.getByRole('button', { name: 'Save folder' }));
  expect(await screen.findByText(/Saved “Pipeline”/)).toBeInTheDocument();
  expect(posted[0]).toEqual({ label: 'Pipeline', provider: 'gdrive', connector_account_id: 'g-1',
    folder_ref: 'https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp' });
  expect(screen.getByRole('button', { name: 'Turn on' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Check folder' }));
  expect(await screen.findByText(/Folder “Pipeline” found/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Turn on' }));
  expect(await screen.findByText('Folder turned on.')).toBeInTheDocument();
});

it('sends an approved document write once and reports the verified result', async () => {
  let state = 'approved';
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === path) return json([]);
    if (url === '/api/projects/project-1/documents/actions') return json([]);
    const row = () => ({ id: 'w-1', source_id: 'd-1', work_order_id: 'o', run_id: 'r', approval_id: 'a', folder_id: 'F', file_name: 'brief.md',
      mime_type: 'text/markdown', content_sha256: 'a'.repeat(64), state, error_code: null, provider_file_id: state === 'done' ? 'f-1' : null,
      readback: state === 'done' ? { verified: true } : null, created_at: '2026-10-01T00:00:00Z', finished_at: null });
    if (url === '/api/projects/project-1/documents/writes') return json([row()]);
    if (url === '/api/projects/project-1/documents/writes/w-1/dispatch') { state = 'done'; return json(row()); }
    throw new Error(`Unexpected ${url}`);
  }));
  const user = userEvent.setup();
  render(<DocumentSources projectId="project-1" connectors={[]} />);
  await user.click(await screen.findByRole('button', { name: 'Write exact file once' }));
  expect(await screen.findByText('“brief.md” was written and read back.')).toBeInTheDocument();
  expect(screen.getByText(/Written and verified/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Write exact file once' })).not.toBeInTheDocument();
});
