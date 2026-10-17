import { useEffect, useRef, useState } from 'react';
import { ApiError } from './api';
import { checkGitHubSourceSnapshot, type GitHubSourceSnapshot, type ProjectSource } from './sourceApi';

/** Repository settings owns the one explicit, read-only remote revision check. */
export function GitHubSourceCheck({ projectId, source }: { projectId: string; source: ProjectSource }) {
  const [result, setResult] = useState<GitHubSourceSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const inFlight = useRef(false);
  useEffect(() => {
    generation.current += 1;
    inFlight.current = false;
    setResult(null); setError(null); setBusy(false);
    return () => { generation.current += 1; };
  }, [projectId, source.id, source.revision, source.branch, source.owner, source.repo]);
  async function check() {
    if (inFlight.current || source.kind !== 'github') return;
    const current = generation.current;
    inFlight.current = true; setBusy(true); setResult(null); setError(null);
    try {
      const saved = await checkGitHubSourceSnapshot(projectId, source.id);
      if (current !== generation.current) return;
      if (saved.saved_revision !== source.revision || saved.branch !== source.branch || saved.owner !== source.owner || saved.repo !== source.repo) {
        throw new ApiError('The response did not match the selected repository. Its saved version is unchanged.', null);
      }
      setResult(saved);
    } catch (cause) {
      if (current === generation.current) setError(cause instanceof ApiError ? cause.message : 'The current repository version could not be checked. Its saved version is unchanged.');
    } finally {
      if (current === generation.current) { inFlight.current = false; setBusy(false); }
    }
  }
  if (source.kind !== 'github') return null;
  return <div className="repository-check">
    <button type="button" className="button button-outline" disabled={busy} onClick={() => { void check(); }}>{busy ? 'Checking GitHub…' : 'Check current GitHub revision'}</button>
    <small>Contacts GitHub using this connection. Your saved repository version stays unchanged.</small>
    {result && <p role="status">{result.matches_saved_revision ? 'The saved version is current.' : 'This branch differs from the saved version.'} Checked {new Date(result.checked_at).toLocaleString()}.</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
  </div>;
}
