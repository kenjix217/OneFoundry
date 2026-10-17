import { useEffect, useState, type FormEvent } from 'react';

import { ApiError } from './api';
import { approvalPresentation } from './approvalPresentation';
import { getSyntheticCode } from './codeApi';
import { getSalesDraft } from './salesApi';
import { listOutboundEmails } from './smtpApi';
import { zohoActionState } from './zohoMailApi';
import { dispatchGitHubComment, getGitHubCommentStatus, proposeGitHubComment, reconcileGitHubComment,
  type GitHubCommentStatus } from './githubCommentApi';
import { dispatchGitHubFixtureFetch, dispatchGitHubFixtureSnapshot, dispatchGitHubStage,
  getGitHubFixtureFetch, getGitHubFixtureSnapshot, getGitHubStageProposal,
  listProjectSources, type GitHubFixtureFetch, type GitHubFixtureSnapshot, type GitHubStageProposal,
  type ProjectSource } from './sourceApi';
import { decideApproval, listApprovals, listRuns, requestApproval,
  type ActionType, type Approval, type ApprovalStatus, type Run } from './workApi';

type Props = { projectId: string; onOpenRun?: (runId: string) => void };

const actions: { value: ActionType; label: string }[] = [
  { value: 'email', label: 'Email' }, { value: 'publish', label: 'Publish' },
  { value: 'merge', label: 'Merge' }, { value: 'deploy', label: 'Deploy' },
  { value: 'cloud_change', label: 'Cloud change' }, { value: 'other', label: 'Other' },
];

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Could not complete the request. Check the saved state before retrying.';
}

function time(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function ApprovalInbox({ projectId, onOpenRun }: Props) {
  const [status, setStatus] = useState<ApprovalStatus>('pending');
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [runId, setRunId] = useState('');
  const [actionType, setActionType] = useState<ActionType>('email');
  const [target, setTarget] = useState('');
  const [content, setContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [githubStages, setGithubStages] = useState<Record<string, GitHubStageProposal>>({});
  const [githubFetches, setGithubFetches] = useState<Record<string, GitHubFixtureFetch>>({});
  const [githubSnapshots, setGithubSnapshots] = useState<Record<string, GitHubFixtureSnapshot>>({});
  const [githubComments, setGithubComments] = useState<Record<string, GitHubCommentStatus>>({});
  const [commentFormOpen, setCommentFormOpen] = useState(false);
  const [commentSources, setCommentSources] = useState<ProjectSource[]>([]);
  const [commentRunId, setCommentRunId] = useState('');
  const [commentSourceId, setCommentSourceId] = useState('');
  const [prNumber, setPrNumber] = useState('');
  const [commentText, setCommentText] = useState('');
  const [commentKey, setCommentKey] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void Promise.all([listApprovals(projectId, status, controller.signal), listRuns(projectId, '', controller.signal)])
      .then(([items, runItems]) => { setApprovals(items); setRuns(runItems); setLoading(false); })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return;
        setError(message(cause));
        setLoading(false);
      });
    return () => controller.abort();
  }, [projectId, status, reload]);

  async function submitRequest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setActionError(null);
    setNotice(null);
    try {
      const created = await requestApproval(projectId, runId, {
        action_type: actionType, target: target.trim(), content: content.trim(),
      });
      const items = await listApprovals(projectId, 'pending');
      if (!items.some((item) => item.id === created.id && item.status === 'pending')) {
        throw new ApiError('The proposal returned, but inbox readback did not find it. Check the saved state before retrying.', null);
      }
      setStatus('pending');
      setApprovals(items);
      setTarget('');
      setContent('');
      setNotice('Proposal recorded for review. Nothing was sent or executed.');
    } catch (cause) {
      setActionError(message(cause));
    } finally {
      setSaving(false);
    }
  }

  async function decide(item: Approval, decision: 'approved' | 'rejected') {
    setSaving(true);
    setActionError(null);
    setNotice(null);
    try {
      await decideApproval(projectId, item.id, decision);
      const [current, decided] = await Promise.all([
        listApprovals(projectId, status), listApprovals(projectId, decision),
      ]);
      if (!decided.some((approval) => approval.id === item.id && approval.status === decision)) {
        throw new ApiError('The decision returned, but inbox readback did not confirm it. Check the saved state before retrying.', null);
      }
      let decisionNotice = `Proposal ${decision}. This records a decision only; no outbound action was performed.`;
      if (item.synthetic_merge) {
        const saved = await getSyntheticCode(projectId, item.run_id);
        const merge = saved.jobs.find((job) => job.stage === 'merge');
        if (decision === 'rejected') {
          if (merge) throw new ApiError('The rejection was saved, but a merge job exists. Inspect Activity before taking another action.', null);
          decisionNotice = 'Synthetic merge rejected. No local merge job was queued.';
        } else if (!merge) {
          throw new ApiError('The approval was saved, but merge job readback is missing. Inspect Activity; do not approve again.', null);
        } else {
          decisionNotice = merge.status === 'unknown' ?
            'Synthetic merge approved; dispatch outcome is unknown. Inspect Activity without retrying approval.' :
            'Synthetic merge approved; local fixture merge queued. Check Activity for its saved receipt.';
        }
      }
      if (item.synthetic_sales) {
        const saved = await getSalesDraft(projectId, item.run_id);
        if (saved.approval_id !== item.id) {
          throw new ApiError('Sales draft approval binding differs. Inspect Activity before another action.', null);
        }
        decisionNotice = decision === 'rejected'
          ? 'Synthetic Sales draft rejected. No email was sent.'
          : saved.delivery_status === 'delivered'
            ? 'Synthetic Sales draft delivered to local Mailpit and confirmed by readback.'
            : 'Sales approval saved; local Mailpit delivery outcome is unknown. Inspect Activity without retrying.';
      }
      if (item.outbound_email) {
        const saved = (await listOutboundEmails(projectId)).find((email) => email.approval_id === item.id);
        if (!saved) throw new ApiError('Email decision saved but message readback is missing. Inspect Activity before another action.', null);
        decisionNotice = decision === 'rejected' ? 'Email rejected. Nothing was sent.' :
          saved.state === 'accepted' ? 'SMTP server accepted the email. Recipient delivery is not confirmed.' :
          `Email approval saved; submission status is ${saved.state}. Inspect the outgoing mail record without retrying.`;
      }
      if (item.zoho_mail_operation) {
        const state = await zohoActionState(projectId, item.id);
        const draft = item.zoho_mail_operation.endsWith('draft');
        decisionNotice = decision === 'rejected' ? 'Zoho mail request rejected. Nothing was changed.' :
          state === 'accepted' ? (draft ? 'Zoho accepted the draft request. Nothing was sent.' :
            'Zoho accepted the email. Recipient delivery is not confirmed.') :
          `Zoho approval saved; status is ${state}. Inspect Activity and Zoho without retrying.`;
      }
      if (item.github_stage === true) {
        decisionNotice = decision === 'approved'
          ? 'Exact GitHub staging proposal approved. Check synthetic offline stage status before dispatch. No live GitHub fetch is available.'
          : 'Exact GitHub staging proposal rejected. Stage status: not launched.';
      }
      if (item.github_fetch === true) {
        decisionNotice = decision === 'approved'
          ? 'Synthetic HTTPS fixture verification approved. Check its exact status before dispatch. No real GitHub fetch or coding checkout.'
          : 'Synthetic HTTPS fixture verification rejected. No fetch was launched.';
      }
      if (item.github_snapshot === true) {
        decisionNotice = decision === 'approved'
          ? 'Synthetic HTTPS fixture snapshot approved. Check its exact status before dispatch. No real GitHub fetch or coding launch.'
          : 'Synthetic HTTPS fixture snapshot rejected. No source copy was made.';
      }
      if (item.github_comment === true) {
        decisionNotice = decision === 'approved'
          ? 'Exact GitHub PR comment approved. No comment was posted. Check saved status before the separate one-shot dispatch.'
          : 'GitHub PR comment rejected. Nothing was posted.';
      }
      setApprovals(current);
      setNotice(decisionNotice);
    } catch (cause) {
      setActionError(message(cause));
      setReload((value) => value + 1);
    } finally {
      setSaving(false);
    }
  }

  async function checkGithubStage(item: Approval) {
    setSaving(true);
    setActionError(null);
    try {
      const stage = await getGitHubStageProposal(projectId, item.id);
      setGithubStages((current) => ({ ...current, [item.id]: stage }));
      setNotice(`Synthetic local fixture stage: ${stage.stage_status.replace('_', ' ')}. No live GitHub fetch was performed.`);
    } catch (cause) {
      setActionError(message(cause));
    } finally {
      setSaving(false);
    }
  }

  async function dispatchGithubStage(item: Approval) {
    setSaving(true);
    setActionError(null);
    try {
      const stage = await dispatchGitHubStage(projectId, item.id);
      const saved = await getGitHubStageProposal(projectId, item.id);
      if (saved.attempt_id !== stage.attempt_id) {
        throw new ApiError('Synthetic stage dispatch readback differed. Check saved status before another action.', null);
      }
      setGithubStages((current) => ({ ...current, [item.id]: saved }));
      setNotice(`Synthetic local fixture stage: ${saved.stage_status}. No live GitHub fetch was performed.`);
    } catch (cause) {
      setActionError(message(cause));
    } finally {
      setSaving(false);
    }
  }

  async function checkGithubFetch(item: Approval) {
    setSaving(true); setActionError(null);
    try {
      const fetch = await getGitHubFixtureFetch(projectId, item.id);
      setGithubFetches((current) => ({ ...current, [item.id]: fetch }));
      setNotice(`Synthetic HTTPS fixture verification: ${fetch.fetch_status}. No real GitHub fetch or coding checkout.`);
    } catch (cause) { setActionError(message(cause)); }
    finally { setSaving(false); }
  }

  async function dispatchGithubFetch(item: Approval) {
    setSaving(true); setActionError(null);
    try {
      const launched = await dispatchGitHubFixtureFetch(projectId, item.id);
      const saved = await getGitHubFixtureFetch(projectId, item.id);
      if (saved.attempt_id !== launched.attempt_id) {
        throw new ApiError('Fixture fetch dispatch readback differed. Inspect Activity before another action.', null);
      }
      setGithubFetches((current) => ({ ...current, [item.id]: saved }));
      setNotice(`Synthetic HTTPS fixture verification: ${saved.fetch_status}. No real GitHub fetch or coding checkout.`);
    } catch (cause) { setActionError(message(cause)); }
    finally { setSaving(false); }
  }

  async function checkGithubSnapshot(item: Approval) {
    setSaving(true); setActionError(null);
    try {
      const snapshot = await getGitHubFixtureSnapshot(projectId, item.id);
      setGithubSnapshots((current) => ({ ...current, [item.id]: snapshot }));
      setNotice(`Synthetic HTTPS snapshot: ${snapshot.snapshot_status}. Commit-to-tree link is fixture asserted. No coding launch.`);
    } catch (cause) { setActionError(message(cause)); }
    finally { setSaving(false); }
  }

  async function dispatchGithubSnapshot(item: Approval) {
    setSaving(true); setActionError(null);
    try {
      const launched = await dispatchGitHubFixtureSnapshot(projectId, item.id);
      const saved = await getGitHubFixtureSnapshot(projectId, item.id);
      if (saved.attempt_id !== launched.attempt_id) {
        throw new ApiError('Fixture snapshot dispatch readback differed. Inspect Activity before another action.', null);
      }
      setGithubSnapshots((current) => ({ ...current, [item.id]: saved }));
      setNotice(`Synthetic HTTPS snapshot: ${saved.snapshot_status}. No coding launch.`);
    } catch (cause) { setActionError(message(cause)); }
    finally { setSaving(false); }
  }

  async function openCommentForm() {
    setCommentFormOpen(true); setActionError(null);
    try {
      const sources = await listProjectSources(projectId);
      setCommentSources(sources.filter((source) => source.kind === 'github'));
    } catch (cause) { setActionError(message(cause)); }
  }

  async function submitComment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setSaving(true); setActionError(null); setNotice(null);
    try {
      const number = Number(prNumber);
      if (!Number.isSafeInteger(number) || number < 1 || number > 999_999_999) throw new Error('Enter a valid PR number.');
      if (!commentText.trim() || new TextEncoder().encode(commentText).length > 16_000) throw new Error('Comment must contain text and be at most 16,000 UTF-8 bytes.');
      const key = commentKey || crypto.randomUUID();
      setCommentKey(key);
      const proposed = await proposeGitHubComment(projectId, {
        run_id: commentRunId, source_id: commentSourceId, number, comment: commentText, idempotency_key: key,
      });
      const items = await listApprovals(projectId, 'pending');
      if (!items.some((item) => item.id === proposed.approval_id && item.github_comment === true)) {
        throw new ApiError('The proposal returned, but inbox readback did not find it. Inspect saved state before retrying.', null);
      }
      setStatus('pending'); setApprovals(items); setCommentText(''); setPrNumber(''); setCommentKey('');
      setNotice('Exact PR comment proposal saved. No comment was posted.');
    } catch (cause) { setActionError(`${message(cause)} Check saved proposals before submitting again.`); }
    finally { setSaving(false); }
  }

  async function checkComment(item: Approval) {
    setSaving(true); setActionError(null);
    try {
      const saved = await getGitHubCommentStatus(projectId, item.id);
      setGithubComments((current) => ({ ...current, [item.id]: saved }));
      setNotice(`GitHub PR comment status: ${saved.status}${saved.reason_code ? ` · ${saved.reason_code}` : ''}.`);
    } catch (cause) { setActionError(message(cause)); }
    finally { setSaving(false); }
  }

  async function dispatchComment(item: Approval) {
    setSaving(true); setActionError(null);
    setGithubComments((current) => ({ ...current, [item.id]: { approval_id: item.id, status: 'unknown', retry_allowed: false } }));
    try {
      await dispatchGitHubComment(projectId, item.id);
      const saved = await getGitHubCommentStatus(projectId, item.id);
      setGithubComments((current) => ({ ...current, [item.id]: saved }));
      setNotice(`GitHub PR comment status: ${saved.status}${saved.reason_code ? ` · ${saved.reason_code}` : ''}.`);
    } catch (cause) { setActionError(`${message(cause)} Check saved status; do not dispatch again.`); }
    finally { setSaving(false); }
  }

  async function reconcileComment(item: Approval) {
    setSaving(true); setActionError(null);
    setGithubComments((current) => ({ ...current, [item.id]: { approval_id: item.id, status: 'unknown', retry_allowed: false } }));
    try {
      const saved = await reconcileGitHubComment(projectId, item.id);
      setGithubComments((current) => ({ ...current, [item.id]: saved }));
      setNotice(`GitHub PR comment readback: ${saved.status}${saved.reason_code ? ` · ${saved.reason_code}` : ''}.`);
    } catch (cause) { setActionError(`${message(cause)} Check saved status; do not retry the POST.`); }
    finally { setSaving(false); }
  }

  const activeRuns = runs.filter((run) => run.status === 'running' && run.mode === 'manual_planning');

  return (
    <section aria-labelledby="inbox-heading">
      <div className="view-heading"><div><p className="eyebrow">Operator decisions</p><h1 id="inbox-heading">Approval inbox</h1>
        <p>Review exact proposals and record operator decisions before execution.</p></div></div>
      <div className="inbox-boundary"><span aria-hidden="true">◇</span><p><strong>Operator controlled actions.</strong> Review the exact recipient, content, or file change before approving. Mail requests specify whether approval saves a draft or sends an email once. Document changes and GitHub PR comments require a separate dispatch after approval.</p></div>
      {actionError && <p className="form-error" role="alert">{actionError}</p>}
      <button className="button button-outline" type="button" onClick={() => { void openCommentForm(); }}>Prepare GitHub PR comment</button>
      {commentFormOpen && <form className="panel proposal-form" onSubmit={(event) => { void submitComment(event); }}>
        <h2>Propose exact GitHub PR comment</h2>
        <p className="quiet-note">This is an issue comment on the whole PR. Commits may change before posting. Creating the proposal reads the current PR head from GitHub; it does not post a comment.</p>
        <label className="field"><span className="field-label">Running role run</span><select required value={commentRunId} onChange={(event) => setCommentRunId(event.target.value)}>
          <option value="">Choose a running role run</option>{runs.filter((run) => run.status === 'running' && (run.mode === 'live_text' || run.mode === 'synthetic_text')).map((run) => <option key={run.id} value={run.id}>{run.work_order_title} · {time(run.started_at)}</option>)}
        </select></label>
        <label className="field"><span className="field-label">Saved GitHub source</span><select required value={commentSourceId} onChange={(event) => setCommentSourceId(event.target.value)}>
          <option value="">Choose a saved source</option>{commentSources.map((source) => <option key={source.id} value={source.id}>{source.owner}/{source.repo} · {source.branch}</option>)}
        </select></label>
        <label className="field"><span className="field-label">PR number</span><input required type="number" min="1" max="999999999" step="1" value={prNumber} onChange={(event) => setPrNumber(event.target.value)} /></label>
        <label className="field"><span className="field-label">Exact comment text</span><textarea required maxLength={16000} rows={5} value={commentText} onChange={(event) => setCommentText(event.target.value)} /></label>
        <p className="quiet-note">Exact preview:</p><p className="approval-content">{commentText || 'Enter the comment above.'}</p>
        <button className="button button-primary" disabled={saving || commentSources.length === 0} type="submit">Save PR comment proposal</button>
      </form>}
      <div className="inbox-layout">
        <form className="panel proposal-form" onSubmit={(event) => { void submitRequest(event); }}>
          <p className="eyebrow">New proposal</p><h2>Request a decision</h2>
          <label className="field"><span className="field-label">Manual run</span><select required value={runId} onChange={(event) => setRunId(event.target.value)}>
            <option value="">Choose a running work order</option>{activeRuns.map((run) => <option key={run.id} value={run.id}>{run.work_order_title}</option>)}
          </select></label>
          {activeRuns.length === 0 && !loading && <p className="quiet-note">Start a manual run in Work orders before requesting a decision.</p>}
          <label className="field"><span className="field-label">Action type</span><select value={actionType} onChange={(event) => setActionType(event.target.value as ActionType)}>
            {actions.map((action) => <option key={action.value} value={action.value}>{action.label}</option>)}
          </select></label>
          <label className="field"><span className="field-label">Target</span><input required maxLength={500} value={target} onChange={(event) => setTarget(event.target.value)} placeholder="Exact recipient, branch, URL, or target" /></label>
          <label className="field"><span className="field-label">Proposed content</span><textarea required maxLength={16000} rows={4} value={content} onChange={(event) => setContent(event.target.value)} placeholder="What would be sent or changed" /></label>
          <p className="quiet-note">This text is stored for review; it is not transmitted to the target.</p>
          <button className="button button-primary" type="submit" disabled={saving || activeRuns.length === 0}>{saving ? 'Saving…' : 'Request approval'}</button>
        </form>
        <div className="inbox-list-wrap">
          <div className="inbox-tabs" role="group" aria-label="Approval status">
            {(['pending', 'approved', 'rejected'] as ApprovalStatus[]).map((value) =>
              <button key={value} type="button" aria-pressed={status === value} className={status === value ? 'active' : ''}
                onClick={() => { setStatus(value); setNotice(null); setActionError(null); }}>{value}</button>)}
          </div>
          <div className="inbox-list">
          {notice && <div className="notice" role="status">{notice}</div>}
          {loading && <div className="loading-panel" role="status"><span className="loading-orb" />Loading inbox…</div>}
          {error && !loading && <div className="empty-inline"><p role="alert">{error}</p><button className="button button-outline" type="button" onClick={() => setReload((value) => value + 1)}>Try again</button></div>}
          {!loading && !error && approvals.length === 0 && <div className="empty-inline">No {status} proposals for this project.</div>}
          {!loading && !error && approvals.map((item) =>
            <article key={item.id} className="approval-card panel">
              <div className="detail-top"><span className="eyebrow">{actions.find((action) => action.value === item.action_type)?.label ?? item.action_type}</span>
                <span className={`status-chip status-${item.status}`}>{item.status}</span></div>
              <h3>{approvalPresentation(item).target}</h3><p className="approval-context">{runs.find((run) => run.id === item.run_id)?.work_order_title ?? 'Previous work order'}</p>
              {item.synthetic_merge && <p className="quiet-note">Bound synthetic fixture merge · approving queues a local merge job; rejecting leaves the fixture unmerged.</p>}
              {item.synthetic_sales && <p className="quiet-note">Bound synthetic Sales draft · approving sends the exact email to local Mailpit only. Recipient is fixed at sales-preview@onefoundry.invalid.</p>}
              {item.outbound_email && <p className="quiet-note">Bound outgoing email · approval triggers one SMTP submission attempt. Server acceptance does not confirm recipient delivery; an unknown result is never retried automatically.</p>}
              {item.zoho_mail_operation && <p className="quiet-note">{item.zoho_mail_operation.endsWith('draft')
                ? 'Approval saves this exact draft in Zoho. It does not send it.'
                : 'Approval sends this exact Zoho email once. Recipient delivery is not confirmed; an unknown result is never retried.'}</p>}
              {item.github_stage === true && <p className="quiet-note">Exact GitHub source staging proposal · approval authorizes a separate synthetic local fixture stage attempt. Live GitHub fetch is unavailable.</p>}
              {item.github_fetch === true && <p className="quiet-note">Version 2 synthetic HTTPS fixture verification only · exact source, action, expiry, and idempotency key bound. No real GitHub fetch or coding checkout.</p>}
              {item.github_fetch === true && item.status === 'approved' && <div className="approval-actions">
                <button className="button button-outline" disabled={saving} type="button" onClick={() => { void checkGithubFetch(item); }}>Check synthetic HTTPS verification</button>
                {githubFetches[item.id]?.fetch_status === 'not_launched' && githubFetches[item.id]?.source_current && !githubFetches[item.id]?.expired &&
                  <button className="button button-primary" disabled={saving} type="button" onClick={() => { void dispatchGithubFetch(item); }}>Start synthetic HTTPS verification</button>}
              </div>}
              {item.github_fetch === true && githubFetches[item.id] && <p className="quiet-note">Synthetic HTTPS fixture verification: {githubFetches[item.id].fetch_status}{githubFetches[item.id].reason_code ? ` · ${githubFetches[item.id].reason_code}` : ''}. No coding checkout.</p>}
              {item.github_snapshot === true && <p className="quiet-note">Version 3 synthetic HTTPS source snapshot · exact source, action, expiry, and idempotency key bound. The commit-to-tree link is fixture asserted; tree and blob hashes are checked locally. No real GitHub fetch or coding launch.</p>}
              {item.github_snapshot === true && item.status === 'approved' && <div className="approval-actions">
                <button className="button button-outline" disabled={saving} type="button" onClick={() => { void checkGithubSnapshot(item); }}>Check synthetic snapshot</button>
                {githubSnapshots[item.id]?.snapshot_status === 'not_launched' && githubSnapshots[item.id]?.source_current && !githubSnapshots[item.id]?.expired &&
                  <button className="button button-primary" disabled={saving} type="button" onClick={() => { void dispatchGithubSnapshot(item); }}>Create synthetic local snapshot</button>}
              </div>}
              {item.github_snapshot === true && githubSnapshots[item.id] && <p className="quiet-note">Synthetic snapshot: {githubSnapshots[item.id].snapshot_status}{githubSnapshots[item.id].reason_code ? ` · ${githubSnapshots[item.id].reason_code}` : ''}. No coding launch.</p>}
              {item.github_stage === true && item.status === 'approved' && <div className="approval-actions">
                <button className="button button-outline" disabled={saving} type="button" onClick={() => { void checkGithubStage(item); }}>Check synthetic offline stage status</button>
                {githubStages[item.id]?.stage_status === 'not_launched' && githubStages[item.id]?.source_current &&
                  <button className="button button-primary" disabled={saving} type="button" onClick={() => { void dispatchGithubStage(item); }}>Start synthetic offline stage</button>}
              </div>}
              {item.github_stage === true && githubStages[item.id] && <p className="quiet-note">Synthetic local fixture stage: {githubStages[item.id].stage_status.replace('_', ' ')}{githubStages[item.id].reason_code ? ` · ${githubStages[item.id].reason_code}` : ''}{githubStages[item.id].source_tree ? ' · Source files recorded' : ''}. No live GitHub fetch.</p>}
              {item.github_comment === true && <p className="quiet-note">Live GitHub issue comment on the whole PR. Commits may change before posting. Approval alone does not post it.</p>}
              {item.github_comment === true && item.status === 'approved' && <div className="approval-actions">
                <button className="button button-outline" disabled={saving} type="button" onClick={() => { void checkComment(item); }}>Check PR comment status</button>
                {githubComments[item.id]?.status === 'not_attempted' &&
                  <button className="button button-primary" disabled={saving} type="button" onClick={() => { void dispatchComment(item); }}>Post exact comment once</button>}
                {githubComments[item.id]?.reason_code === 'readback_pending' &&
                  <button className="button button-outline" disabled={saving} type="button" onClick={() => { void reconcileComment(item); }}>Read back acknowledged comment once</button>}
              </div>}
              {item.github_comment === true && githubComments[item.id] && <p className="quiet-note">Saved PR comment status: {githubComments[item.id].status}{githubComments[item.id].reason_code ? ` · ${githubComments[item.id].reason_code}` : ''}{githubComments[item.id].comment_id ? ' · Comment saved' : ''}. No automatic retry.</p>}
              <p className="approval-content">{approvalPresentation(item).content}</p>
              <div className="approval-meta"><span>Proposed {time(item.created_at)}</span><span>Saved proposal</span></div>
              {item.decided_at && <p className="quiet-note">Decided {time(item.decided_at)}</p>}
              {item.status === 'pending' && <div className="approval-actions"><button className="button button-outline" disabled={saving} type="button" onClick={() => { void decide(item, 'rejected'); }}>Reject</button>
                <button className="button button-primary" disabled={saving} type="button" onClick={() => { void decide(item, 'approved'); }}>{item.zoho_mail_operation ? (item.zoho_mail_operation.endsWith('draft') ? 'Approve and save Zoho draft' : 'Approve and send Zoho email once') : item.synthetic_merge ? 'Approve and queue local merge' : item.synthetic_sales ? 'Approve and send to local Mailpit' : item.outbound_email ? 'Approve and send email once' : item.github_stage === true ? 'Approve future exact stage' : item.github_fetch === true ? 'Approve synthetic HTTPS verification' : item.github_snapshot === true ? 'Approve synthetic local snapshot' : item.github_comment === true ? 'Approve exact PR comment only' : 'Approve decision'}</button></div>}
              {item.synthetic_merge && onOpenRun && <button className="button button-quiet" type="button" onClick={() => onOpenRun(item.run_id)}>View runner receipt</button>}
            </article>)}
          </div>
        </div>
      </div>
    </section>
  );
}
