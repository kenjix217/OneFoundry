import { webcrypto } from 'node:crypto';
import { beforeEach, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { ApiError } from '../src/api';
import { getTeamStart, startTeamWork } from '../src/taskWorkflowApi';
import { useTaskStartWork } from '../src/useTaskStartWork';

vi.mock('../src/taskWorkflowApi', () => ({ getTeamStart: vi.fn(), startTeamWork: vi.fn() }));
const receipt = { session_id: 'session', message_id: 'message', attempt_id: 'attempt', sequence: 1, state: 'queued' as const };
beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto); sessionStorage.clear(); vi.clearAllMocks();
  vi.mocked(startTeamWork).mockResolvedValue(receipt);
  vi.mocked(getTeamStart).mockRejectedValue(new ApiError('No saved start', 404));
});

it('starts only on an explicit action and stores no message text', async () => {
  const confirmed = vi.fn();
  const { result } = renderHook(() => useTaskStartWork('project', 'order', confirmed));
  expect(startTeamWork).not.toHaveBeenCalled();
  await act(() => result.current.start(4, 'Use this saved brief.'));
  expect(startTeamWork).toHaveBeenCalledWith('project', 'order', expect.objectContaining({ expected_version: 4, body: 'Use this saved brief.' }));
  expect(confirmed).toHaveBeenCalledWith('order');
  expect(sessionStorage.length).toBe(0);
});

it('recovers a lost response through the exact saved receipt without another POST', async () => {
  vi.mocked(startTeamWork).mockRejectedValue(new Error('Connection lost'));
  vi.mocked(getTeamStart).mockResolvedValue(receipt);
  const confirmed = vi.fn();
  const { result } = renderHook(() => useTaskStartWork('project', 'order', confirmed));
  await act(() => result.current.start(4, 'Use this saved brief.'));
  expect(startTeamWork).toHaveBeenCalledTimes(1);
  expect(getTeamStart).toHaveBeenCalledWith('project', 'order', vi.mocked(startTeamWork).mock.calls[0][2].client_message_id);
  expect(confirmed).toHaveBeenCalledWith('order');
  expect(result.current.error).toBeNull();
});

it('retains the same identity after uncertainty and never retries automatically on reload', async () => {
  vi.mocked(startTeamWork).mockRejectedValue(new Error('Connection lost'));
  const confirmed = vi.fn();
  const first = renderHook(() => useTaskStartWork('project', 'order', confirmed));
  await act(() => first.result.current.start(4, 'Private business brief'));
  expect(confirmed).not.toHaveBeenCalled();
  expect(sessionStorage.getItem('onefoundry:task-start:project:order')).not.toContain('Private business brief');
  const identity = vi.mocked(startTeamWork).mock.calls[0][2];
  first.unmount();
  const second = renderHook(() => useTaskStartWork('project', 'order', confirmed));
  await waitFor(() => expect(second.result.current.error).toContain('unconfirmed'));
  expect(startTeamWork).toHaveBeenCalledTimes(1);
  await act(() => second.result.current.start(5, 'Changed brief'));
  expect(startTeamWork).toHaveBeenCalledTimes(1);
  vi.mocked(startTeamWork).mockResolvedValue(receipt);
  await act(() => second.result.current.start(5, 'Private business brief'));
  expect(vi.mocked(startTeamWork).mock.calls[1][2]).toEqual(identity);
  expect(confirmed).toHaveBeenCalledWith('order');
});

it('shows owner rejection and clears a definitively rejected request', async () => {
  vi.mocked(startTeamWork).mockRejectedValue(new ApiError('Project is paused', 409));
  const confirmed = vi.fn();
  const { result } = renderHook(() => useTaskStartWork('project', 'order', confirmed));
  await act(() => result.current.start(4, 'Use this brief'));
  expect(result.current.error).toContain('Project is paused');
  expect(confirmed).not.toHaveBeenCalled();
  expect(sessionStorage.length).toBe(0);
});
