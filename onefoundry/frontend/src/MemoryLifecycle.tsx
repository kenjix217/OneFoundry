import { useState } from 'react';

import { transitionMemory, type MemoryItem } from './memoryApi';

export function MemoryLifecycle({ projectId, item, onChanged }: { projectId: string; item: MemoryItem; onChanged: () => void }) {
  const [reason, setReason] = useState('');
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (item.project_id !== projectId || item.state === 'superseded') return null;
  const action = item.state === 'withdrawn' ? 'restore' : 'withdraw';
  async function apply() {
    setSaving(true); setError(null);
    try { await transitionMemory(projectId, item.id, action, reason); setOpen(false); onChanged(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Memory state change failed.'); }
    finally { setSaving(false); }
  }
  return <div>
    <button className="button button-outline" type="button" onClick={() => setOpen(!open)}>{action === 'withdraw' ? 'Withdraw fact' : 'Restore fact'}</button>
    {open && <div><p>{action === 'withdraw' ? 'Agents will stop retrieving this fact. Its source and history remain, and you can restore it.' : 'Restore this reviewed fact to current retrieval.'}</p>
      <label className="field">Reason<input value={reason} maxLength={240} onChange={(e) => setReason(e.target.value)} /></label>
      <button type="button" className="button button-primary" disabled={!reason.trim() || saving} onClick={() => { void apply(); }}>{saving ? 'Saving…' : `Confirm ${action}`}</button></div>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
