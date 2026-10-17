import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, type Profile } from './api';
import { createWorkOrder, listWorkOrders, type DeliverableKind } from './workApi';
import { InputFilePicker } from './InputFilePicker';
import { listInputFiles, uploadInputFiles, type PendingInputFile } from './inputFileApi';
import './workOrderCreate.css';

type Props = {
  projectId: string;
  profiles: Profile[];
  onConfirmDraft: (id: string) => Promise<void>;
  onSaved: (notice: string) => void;
  onClose: () => void;
};

export function WorkOrderCreateDialog({ projectId, profiles, onConfirmDraft, onSaved, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleInput = useRef<HTMLInputElement>(null);
  const [title, setTitle] = useState('');
  const [instructions, setInstructions] = useState('');
  const [profileId, setProfileId] = useState('');
  const [deliverableKind, setDeliverableKind] = useState<DeliverableKind>('operator_review');
  const [acceptanceChecks, setAcceptanceChecks] = useState('');
  const [files, setFiles] = useState<PendingInputFile[]>([]);
  const [createdId, setCreatedId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    titleInput.current?.focus();
    return () => element?.close();
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    if (!title.trim() || !instructions.trim() || !profileId) {
      setError('Add a title, instructions, and an assigned agent.');
      return;
    }
    setSaving(true); setError(null);
    try {
      const created = createdId
        ? (await listWorkOrders(projectId)).find((item) => item.id === createdId)
        : await createWorkOrder(projectId, {
          title: title.trim(), instructions: instructions.trim(), profile_id: profileId,
          deliverable_kind: deliverableKind,
          acceptance_checks: acceptanceChecks.split('\n').map((check) => check.trim()).filter(Boolean),
        });
      if (!created) throw new ApiError('Saved work order is unavailable. Refresh before retrying files.', null);
      setCreatedId(created.id);
      await onConfirmDraft(created.id);
      if (files.length) {
        await uploadInputFiles(projectId, files, 'work_order', created.id);
        const linked = await listInputFiles(projectId, created.id);
        if (!files.every((item) => linked.some((file) => file.id === item.id))) {
          throw new ApiError('One or more files were not linked to the saved work order.', null);
        }
      }
      onSaved(files.length ? 'Draft and files saved. Files are processing.' : 'Work order saved as a draft.');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not save the draft. Check saved work before retrying.');
    } finally { setSaving(false); }
  }

  const locked = saving || createdId !== null;
  return <dialog ref={dialog} className="work-create-dialog" aria-labelledby="create-work-title"
    onCancel={(event) => { event.preventDefault(); if (!saving) onClose(); }}>
    <form onSubmit={(event) => { void submit(event); }}>
      <header><div><p className="eyebrow">New assignment</p><h2 id="create-work-title">Create work order</h2></div>
        <button className="button button-quiet" type="button" aria-label="Close new work order" disabled={saving} onClick={onClose}>×</button></header>
      <div className="work-create-fields">
        <label className="field"><span className="field-label">Title</span>
          <input ref={titleInput} required maxLength={160} value={title} disabled={locked} onChange={(event) => setTitle(event.target.value)} placeholder="A clear outcome for this work" /></label>
        <label className="field"><span className="field-label">Instructions</span>
          <textarea required maxLength={16000} rows={4} value={instructions} disabled={locked} onChange={(event) => setInstructions(event.target.value)} placeholder="Describe the task and any boundaries" /></label>
        <div className="work-create-assignment">
          <label className="field"><span className="field-label">Assign to profile</span>
            <select required value={profileId} disabled={locked} onChange={(event) => setProfileId(event.target.value)}>
              <option value="">Choose a profile</option>{profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
            </select></label>
          <label className="field"><span className="field-label">Expected deliverable</span>
            <select value={deliverableKind} disabled={locked} onChange={(event) => setDeliverableKind(event.target.value as DeliverableKind)}>
              <option value="operator_review">Operator review (default)</option><option value="text_reply">Text reply</option>
              <option value="artifact">Saved artifact</option><option value="document">Saved document</option>
              <option value="code_change">Code change with QA</option><option value="external_action">Confirmed external action</option>
            </select></label>
        </div>
        <details className="work-create-checks"><summary>Acceptance checks <span>Optional</span></summary>
          <label className="field"><span className="field-label">Acceptance checks (one per line, optional)</span>
            <textarea value={acceptanceChecks} rows={3} disabled={locked} onChange={(event) => setAcceptanceChecks(event.target.value)} placeholder="What must be true before this work is accepted?" /></label>
        </details>
        <InputFilePicker files={files} onChange={setFiles} limit={8} disabled={locked} label="Work order files" />
        {profiles.length === 0 && <p role="status">Add an agent in Team before creating work.</p>}
      </div>
      <footer>
        {createdId && <p role="status">Draft saved. Retry the selected files or add them later from Manage.</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="work-create-actions"><p>Saved as a draft. Your agent starts when you’re ready.</p>
          <button className="button button-outline" type="button" disabled={saving} onClick={onClose}>Cancel</button>
          <button className="button button-primary" type="submit" disabled={saving || profiles.length === 0}>{saving ? 'Saving…' : createdId ? 'Retry files' : 'Create draft'}</button>
        </div>
      </footer>
    </form>
  </dialog>;
}
