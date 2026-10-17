import { useEffect, useState, type FormEvent } from 'react';

import { ApiError } from './api';
import { dispatchBrowserJob, getBrowserEvidence, getBrowserFixture, getBrowserJob, getBrowserJobByKey, listBrowserJobs, proposeBrowserFixture,
  reconcileBrowserJob, type BrowserEvidence, type BrowserFixture, type BrowserJob } from './browserStaticApi';
import type { RunEvent } from './workApi';
import { clearSessionKey, readSessionKey } from './brandStorage';

type Props = { projectId: string; runId: string; events: RunEvent[] };
type FixtureFields = {
  approved_url: string; html: string; css: string; viewport_width: string; viewport_height: string;
  max_page_bytes: string; timeout_seconds: string; lifetime_seconds: string;
};
const emptyFields: FixtureFields = {
  approved_url: '', html: '', css: '', viewport_width: '', viewport_height: '',
  max_page_bytes: '', timeout_seconds: '', lifetime_seconds: '',
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Saved browser state is unavailable. Check Activity and Approvals before retrying.';
}

function fixtureIds(events: RunEvent[], runId: string): string[] {
  return [...new Set(events.filter((event) => event.run_id === runId && event.event_type === 'browser_static_fixture_proposed')
    .map((event) => event.payload.fixture_id).filter((id): id is string => typeof id === 'string'))];
}

function pendingKey(projectId: string, runId: string): string {
  return `onefoundry.browser-static-fixture.${projectId}.${runId}`;
}

function hasUnresolvedSubmission(projectId: string, runId: string): boolean {
  try { return readSessionKey(pendingKey(projectId, runId)) !== null; }
  catch { return true; }
}

function dispatchKeyName(projectId: string, runId: string, fixtureId: string): string {
  return `onefoundry.browser-static-job.${projectId}.${runId}.${fixtureId}`;
}

function savedDispatchKey(projectId: string, runId: string, fixtureId: string): string | null {
  try { return readSessionKey(dispatchKeyName(projectId, runId, fixtureId)); }
  catch { throw new ApiError('Browser session storage is unavailable; exact dispatch readback cannot be confirmed.', null); }
}

export function BrowserStaticPanel({ projectId, runId, events }: Props) {
  const [open, setOpen] = useState(false);
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [fields, setFields] = useState<FixtureFields>(emptyFields);
  const [pendingUnknown, setPendingUnknown] = useState(() => hasUnresolvedSubmission(projectId, runId));
  const [createdFixtureId, setCreatedFixtureId] = useState<string | null>(null);
  const [fixtures, setFixtures] = useState<BrowserFixture[]>([]);
  const [jobs, setJobs] = useState<BrowserJob[]>([]);
  const [evidence, setEvidence] = useState<Record<string, BrowserEvidence>>({});
  const [pendingDispatches, setPendingDispatches] = useState<Record<string, string>>({});
  const [dispatchError, setDispatchError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true); setError(null);
    const ids = fixtureIds(events, runId);
    if (createdFixtureId && !ids.includes(createdFixtureId)) ids.push(createdFixtureId);
    void Promise.all([
      Promise.all(ids.map((id) => getBrowserFixture(projectId, runId, id, controller.signal))),
      listBrowserJobs(projectId, runId, controller.signal),
    ]).then(([savedFixtures, savedJobs]) => {
      if (controller.signal.aborted) return;
      const pending = Object.fromEntries(savedFixtures.flatMap((item) => {
        const key = savedDispatchKey(projectId, runId, item.id);
        return key ? [[item.id, key]] : [];
      }));
      setPendingDispatches(pending);
      setFixtures(savedFixtures); setJobs(savedJobs); setLoading(false);
    }).catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      setFixtures([]); setJobs([]);
      setError(cause instanceof ApiError && cause.status === 404 && ids.length === 0
        ? 'Static browser is not configured for this local service.' : message(cause));
      setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, runId, events, open, reload, createdFixtureId]);

  function setField<K extends keyof FixtureFields>(key: K, value: FixtureFields[K]) {
    setFields((current) => ({ ...current, [key]: value }));
  }

  async function propose(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || loading || error || pendingUnknown) return;
    setBusy(true); setError(null); setNotice(null);
    const proposal = {
      idempotency_key: crypto.randomUUID(), approved_url: fields.approved_url.trim(),
      html: fields.html, css: fields.css, viewport_width: Number(fields.viewport_width),
      viewport_height: Number(fields.viewport_height), max_page_bytes: Number(fields.max_page_bytes),
      timeout_seconds: Number(fields.timeout_seconds), lifetime_seconds: Number(fields.lifetime_seconds),
    };
    let sent = false;
    try {
      try { sessionStorage.setItem(pendingKey(projectId, runId), proposal.idempotency_key); }
      catch { throw new ApiError('Browser session storage is unavailable; the fixture proposal was not sent.', null); }
      setPendingUnknown(true);
      setFields(emptyFields);
      sent = true;
      const created = await proposeBrowserFixture(projectId, runId, proposal);
      const saved = await getBrowserFixture(projectId, runId, created.id);
      if (saved.id !== created.id || saved.approval_id !== created.approval_id ||
        saved.approved_url !== proposal.approved_url || saved.source_sha256 !== created.source_sha256 ||
        saved.proposal_digest !== created.proposal_digest) {
        throw new ApiError('Fixture write returned, but exact owner readback did not confirm it.', null);
      }
      setCreatedFixtureId(saved.id);
      setFixtures((current) => [...current.filter((item) => item.id !== saved.id), saved]);
      setNotice('Browser check proposed. Review it in Inbox before starting.');
      try { clearSessionKey(pendingKey(projectId, runId)); setPendingUnknown(false); }
      catch { /* The stored key keeps a future proposal blocked. */ }
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 422) {
        try { clearSessionKey(pendingKey(projectId, runId)); setPendingUnknown(false); }
        catch { /* The stored key keeps a future proposal blocked. */ }
      }
      setError(sent
        ? `${message(cause)} Submission outcome may be unknown. Inspect Activity and Approvals before another proposal.`
        : message(cause));
    } finally {
      setBusy(false);
    }
  }

  async function checkJob(current: BrowserJob) {
    setBusy(true); setError(null); setNotice(null);
    try {
      try { await reconcileBrowserJob(projectId, runId, current.id); }
      catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 409) throw cause;
        setNotice(`${message(cause)} Job is pending or unresolved; no new launch was requested.`);
      }
      const saved = await getBrowserJob(projectId, runId, current.id);
      if (saved.fixture_id !== current.fixture_id || saved.request_sha256 !== current.request_sha256 ||
        saved.binding_digest !== current.binding_digest || saved.source_sha256 !== current.source_sha256) {
        throw new ApiError('Browser job readback did not match its saved intent.', null);
      }
      setJobs((items) => items.map((item) => item.id === saved.id ? saved : item));
    } catch (cause) {
      setError(`${message(cause)} Inspect saved job state before checking again.`);
    } finally {
      setBusy(false);
    }
  }

  function confirmDispatch(saved: BrowserJob, item: BrowserFixture, expected: BrowserJob | null): void {
    if (saved.fixture_id !== item.id || saved.source_sha256 !== item.source_sha256 ||
      (expected && (saved.id !== expected.id || saved.request_sha256 !== expected.request_sha256 ||
        saved.binding_digest !== expected.binding_digest || saved.actor_id !== expected.actor_id))) {
      throw new ApiError('Saved browser job does not match the operator dispatch intent.', null);
    }
    setJobs((current) => [...current.filter((job) => job.id !== saved.id), saved]);
    try {
      clearSessionKey(dispatchKeyName(projectId, runId, item.id));
      setPendingDispatches((current) => { const next = { ...current }; delete next[item.id]; return next; });
    } catch { /* Keep the saved key for readback after reopening. */ }
    setNotice('Browser check dispatched. Check its saved result; no second launch was sent.');
  }

  async function dispatch(item: BrowserFixture) {
    if (busy || item.status !== 'approved' || pendingDispatches[item.id] || jobs.some((job) => job.fixture_id === item.id)) return;
    setBusy(true); setDispatchError(null); setNotice(null);
    try {
      const key = crypto.randomUUID();
      try { sessionStorage.setItem(dispatchKeyName(projectId, runId, item.id), key); }
      catch { throw new ApiError('Browser session storage is unavailable; the job was not dispatched.', null); }
      setPendingDispatches((current) => ({ ...current, [item.id]: key }));
      const created = await dispatchBrowserJob(projectId, runId, item.id, key);
      const saved = await getBrowserJobByKey(projectId, runId, key);
      if (!saved) throw new ApiError('Dispatch returned, but the job was not found by its exact key.', null);
      confirmDispatch(saved, item, created);
    } catch (cause) {
      setDispatchError(`${message(cause)} Dispatch outcome may be unknown. Check the saved key before any new action.`);
    } finally {
      setBusy(false);
    }
  }

  async function checkDispatch(item: BrowserFixture) {
    const key = pendingDispatches[item.id];
    if (!key || busy) return;
    setBusy(true); setDispatchError(null); setNotice(null);
    try {
      const saved = await getBrowserJobByKey(projectId, runId, key);
      if (!saved) {
        setNotice('No job was found for the saved key. The dispatch outcome remains unresolved; no new job was sent.');
      } else {
        confirmDispatch(saved, item, null);
      }
    } catch (cause) {
      setDispatchError(`${message(cause)} The saved key remains pending; no new job was sent.`);
    } finally {
      setBusy(false);
    }
  }

  async function readEvidence(current: BrowserJob) {
    setBusy(true); setError(null);
    try {
      const saved = await getBrowserEvidence(projectId, runId, current.id);
      if (current.status !== 'done' || saved.receipt_sha256 !== current.receipt_sha256) {
        throw new ApiError('Browser evidence does not match the saved terminal receipt.', null);
      }
      setEvidence((items) => ({ ...items, [current.id]: saved }));
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }

  return <section className="timeline" style={{ overflowWrap: 'anywhere' }} aria-label="Browser fixture and evidence">
    <button className="button button-outline" type="button" onClick={() => setOpen((value) => !value)}>
      Browser fixture and evidence
    </button>
    {open && <div>
      <p className="quiet-note">Propose a private static page for this run. Approvals records the operator decision. Only built-in Researcher and QA profiles can receive browser_static_inspect_v1 in Tools &amp; Plugins, and use requires saved and current permission. This form does not launch a job.</p>
      <button className="button button-quiet" type="button" disabled={loading || busy} onClick={() => setReload((value) => value + 1)}>Refresh browser state</button>
      {loading && <p role="status">Loading browser state…</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {dispatchError && <p className="form-error" role="alert">{dispatchError}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}
      {!loading && !error && <>
        <section aria-label="Browser fixture proposals">
          <h3>Fixture proposals</h3>
          {fixtures.length === 0 && <p>No browser fixtures recorded for this run.</p>}
          {fixtures.map((item) => <article key={item.id} className="timeline-step-body">
            <strong>Browser check</strong><p>{item.status}</p>
            <p>Exact target: {item.approved_url}</p>
            {pendingDispatches[item.id] && <button className="button button-outline" type="button" disabled={busy} onClick={() => { void checkDispatch(item); }}>Check saved dispatch</button>}
            {item.status === 'approved' && !pendingDispatches[item.id] && !jobs.some((job) => job.fixture_id === item.id) &&
              <button className="button button-outline" type="button" disabled={busy} onClick={() => { void dispatch(item); }}>Dispatch for assigned Researcher or QA</button>}
            {item.status === 'approved' && <p className="quiet-note">The operator initiates dispatch for this run's assigned built-in Researcher or QA profile. The server checks the active run and saved/current tool permission. No role identity is supplied by this form.</p>}
          </article>)}
        </section>
        <section aria-label="Browser jobs">
          <h3>Browser jobs</h3>
          {jobs.length === 0 && <p>No browser jobs recorded for this run.</p>}
          {jobs.map((item) => <article key={item.id} className="timeline-step-body">
            <strong>Browser check result</strong><p>{item.status} · {item.reason_code ?? 'No reason recorded'}</p>

            <p>Saved result: {item.receipt_sha256 ? 'Available' : 'Not available'}</p>
            {item.status === 'queued' && <button className="button button-outline" type="button" disabled={busy} onClick={() => { void checkJob(item); }}>Check job receipt</button>}
            {item.status === 'done' && <button className="button button-outline" type="button" disabled={busy} onClick={() => { void readEvidence(item); }}>Read saved evidence</button>}
            {evidence[item.id] && <div><h4>{evidence[item.id].title}</h4><p>{evidence[item.id].text}</p>
              <p>Blocked requests: {evidence[item.id].blocked_request_count}</p></div>}
          </article>)}
        </section>
        {pendingUnknown && <p className="quiet-note">A previous fixture submission has an unknown outcome. Inspect Activity and Approvals before another proposal; this panel will not create a new key.</p>}
        {!pendingUnknown && <form onSubmit={(event) => { void propose(event); }}>
          <h3>Propose static browser fixture</h3>
          <label className="field"><span className="field-label">Approved loopback URL</span><input required value={fields.approved_url} onChange={(event) => setField('approved_url', event.target.value)} /></label>
          <label className="field"><span className="field-label">HTML source</span><textarea required rows={4} value={fields.html} onChange={(event) => setField('html', event.target.value)} /></label>
          <label className="field"><span className="field-label">CSS source</span><textarea rows={2} value={fields.css} onChange={(event) => setField('css', event.target.value)} /></label>
          <label className="field"><span className="field-label">Viewport width</span><input type="number" required min="1" value={fields.viewport_width} onChange={(event) => setField('viewport_width', event.target.value)} /></label>
          <label className="field"><span className="field-label">Viewport height</span><input type="number" required min="1" value={fields.viewport_height} onChange={(event) => setField('viewport_height', event.target.value)} /></label>
          <label className="field"><span className="field-label">Maximum page bytes</span><input type="number" required min="1" value={fields.max_page_bytes} onChange={(event) => setField('max_page_bytes', event.target.value)} /></label>
          <label className="field"><span className="field-label">Timeout seconds</span><input type="number" required min="1" value={fields.timeout_seconds} onChange={(event) => setField('timeout_seconds', event.target.value)} /></label>
          <label className="field"><span className="field-label">Lifetime seconds</span><input type="number" required min="1" value={fields.lifetime_seconds} onChange={(event) => setField('lifetime_seconds', event.target.value)} /></label>
          <button className="button button-outline" type="submit" disabled={busy}>Propose browser fixture</button>
        </form>}
      </>}
    </div>}
  </section>;
}
