import { webcrypto } from 'node:crypto';
import { afterEach, expect, it, vi } from 'vitest';

import { ApiError } from '../src/api';
import { checkInputIdsReady, chooseInputFiles, uploadInputFiles } from '../src/inputFileApi';

const projectId = 'project-1';
const fileId = '6ab21096-abfc-4e3b-9203-83b50adc6e42';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('reconciles a lost upload response against the exact saved file ID and hash', async () => {
  vi.stubGlobal('crypto', { subtle: webcrypto.subtle, randomUUID: () => fileId });
  const file = new File(['Launch plan'], 'plan.md', { type: 'text/markdown' });
  Object.defineProperty(file, 'arrayBuffer', { value: async () => new TextEncoder().encode('Launch plan').buffer });
  const selected = [{ id: fileId, file }];
  const digest = await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode('Launch plan'));
  const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
  const saved = { id: fileId, project_id: projectId, display_name: 'plan.md', extension: 'md', mime_type: 'text/markdown',
    sha256, byte_size: file.size, status: 'pending', reason_code: null, extractor_version: null };
  let installed = false;
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith(`/${fileId}`)) return installed ? json(saved) : json({ detail: 'not found' }, 404);
    if (init?.method === 'POST') {
      expect((init.headers as Record<string, string>)['X-Client-File-Id']).toBe(fileId);
      installed = true;
      throw new TypeError('response lost');
    }
    throw new Error(`Unexpected ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  expect((await uploadInputFiles(projectId, selected, 'project'))[0].sha256).toBe(sha256);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
});

it('rejects unsupported files and does not mistake processing for readiness', async () => {
  const bad = new File(['PK'], 'archive.zip');
  expect(() => chooseInputFiles({ 0: bad, length: 1 } as unknown as FileList, 8)).toThrow(ApiError);
  const pending = { id: fileId, project_id: projectId, display_name: 'plan.md', extension: 'md', mime_type: 'text/markdown',
    sha256: 'a'.repeat(64), byte_size: 2, status: 'pending', reason_code: null, extractor_version: null };
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(pending)));
  await expect(checkInputIdsReady(projectId, [fileId])).rejects.toThrow('still processing');
});
