import { useEffect, useRef, useState } from 'react';
import { ApiError } from './api';
import { clearSessionKey, readSessionKey } from './brandStorage';
import { getTeamStart, startTeamWork } from './taskWorkflowApi';

type PendingStart = { clientId: string; version: number; bodyHash: string };
const key = (project: string, order: string) => `onefoundry:task-start:${project}:${order}`;

function read(project: string, order: string): PendingStart | null {
  try {
    const value = JSON.parse(readSessionKey(key(project, order)) ?? 'null') as PendingStart | null;
    return value && typeof value.clientId === 'string' && typeof value.bodyHash === 'string' &&
      Number.isSafeInteger(value.version) && value.version > 0 ? value : null;
  } catch { return null; }
}

async function digest(text: string): Promise<string> {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

export function useTaskStartWork(project: string, order: string | null, onConfirmed: (orderId: string) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);
  const confirmed = useRef(onConfirmed);
  useEffect(() => { confirmed.current = onConfirmed; }, [onConfirmed]);
  useEffect(() => {
    setError(null);
    if (!order) return;
    const pending = read(project, order);
    if (!pending) return;
    let canceled = false;
    void getTeamStart(project, order, pending.clientId).then(() => {
      if (canceled) return;
      clearSessionKey(key(project, order)); confirmed.current(order);
    }).catch(() => {
      if (!canceled) setError('A start request is unconfirmed. Start work again with the same message to check or retry the saved request.');
    });
    return () => { canceled = true; };
  }, [project, order]);

  async function start(version: number, body: string) {
    if (!order || inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null);
    let pending = read(project, order);
    try {
      const bodyHash = await digest(body);
      if (pending && pending.bodyHash !== bodyHash) {
        throw new Error('An earlier start is unconfirmed. Use the same message to reconcile it before starting different work.');
      }
      pending ??= { clientId: crypto.randomUUID(), version, bodyHash };
      window.sessionStorage.setItem(key(project, order), JSON.stringify(pending));
      try {
        await startTeamWork(project, order, {
          client_message_id: pending.clientId, expected_version: pending.version, body,
        });
      } catch (failure) {
        try { await getTeamStart(project, order, pending.clientId); }
        catch {
          if (failure instanceof ApiError && failure.status !== null && failure.status >= 400 && failure.status < 500) {
            clearSessionKey(key(project, order));
          }
          throw failure;
        }
      }
      clearSessionKey(key(project, order)); confirmed.current(order);
    } catch (failure) {
      setError(`${failure instanceof Error ? failure.message : 'Start could not be confirmed.'} No additional start was sent.`);
    } finally { inFlight.current = false; setBusy(false); }
  }
  return { start, busy, error };
}
