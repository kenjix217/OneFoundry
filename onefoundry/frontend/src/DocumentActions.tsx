import { useEffect, useState } from 'react';
import { ApiError } from './api';
import { dispatchDocumentAction, listDocumentActions, type DocumentAction } from './documentApi';

const names = { replace_file: 'New file version', edit_cells: 'Cell edit', share_file: 'Sharing permission' };

export function DocumentActions({ projectId }: { projectId: string }) {
  const [actions, setActions] = useState<DocumentAction[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void listDocumentActions(projectId, controller.signal).then(setActions).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(failure instanceof ApiError ? failure.message : 'Could not load document actions.');
    });
    return () => controller.abort();
  }, [projectId]);
  async function send(id: string) {
    setBusy(true); setError(null);
    try { await dispatchDocumentAction(projectId, id); setActions(await listDocumentActions(projectId)); }
    catch (failure) { setError(failure instanceof ApiError ? failure.message : 'Outcome unknown. Refresh to inspect the saved result.'); }
    finally { setBusy(false); }
  }
  return <section aria-label="Existing file changes">
    <h3>Existing file changes</h3>
    <p>Agents with Propose document action can propose a new version from an attached project file, an exact cell edit, or access for one recipient. Review the full change in the approval inbox, then dispatch it here. Uncertain results are saved without retrying.</p>
    {error && <p className="form-error" role="alert">{error}</p>}
    {actions.length === 0 ? <p>No existing file changes proposed.</p> : <ul className="automation-rules">
      {actions.map((action) => <li key={action.id}>
        <p><strong>{action.file_name}</strong> · {names[action.operation]} · {action.state === 'done' ? 'Verified' : action.state === 'pending' ? 'Waiting for approval' : action.state === 'approved' ? 'Approved, not sent' : action.state === 'unknown' ? 'Outcome unknown; inspect before further changes' : action.state === 'dispatching' ? 'Sending' : action.state === 'failed' ? 'Failed' : 'Rejected'}</p>
        {action.error_code && <details><summary>Details</summary><p>{action.error_code}</p></details>}
        {action.state === 'approved' && <button className="button button-outline" disabled={busy} onClick={() => { void send(action.id); }}>Apply approved change once</button>}
      </li>)}
    </ul>}
  </section>;
}
