import { useEffect, useState, type FormEvent } from 'react';

import { captureConversation, listMemory, type MemoryItem, type MemoryScope } from './memoryApi';

type Props = { projectId: string; reloadToken: number; onChanged: () => void };

export function ConversationCapture({ projectId, reloadToken, onChanged }: Props) {
  const [sourceRef, setSourceRef] = useState('');
  const [excerpt, setExcerpt] = useState('');
  const [speaker, setSpeaker] = useState<'user' | 'assistant'>('user');
  const [sentAt, setSentAt] = useState('');
  const [summary, setSummary] = useState('');
  const [scope, setScope] = useState<MemoryScope>('project');
  const [authorized, setAuthorized] = useState(false);
  const [replacement, setReplacement] = useState('');
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [captureId, setCaptureId] = useState(() => crypto.randomUUID());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void listMemory(projectId, { signal: controller.signal }).then((items) => {
      setMemories(items.filter((item) => item.project_id === projectId));
    }).catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      setError(cause instanceof Error ? cause.message : 'Could not load correction targets.');
    });
    return () => controller.abort();
  }, [projectId, reloadToken]);

  function edited() { setCaptureId(crypto.randomUUID()); setNotice(null); }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!authorized) return;
    setSaving(true); setError(null); setNotice(null);
    try {
      await captureConversation(projectId, {
        client_capture_id: captureId, source_ref: sourceRef, authorized_company_content: true,
        messages: [{ speaker, text: excerpt, sent_at: new Date(sentAt).toISOString() }],
        summary, scope, replaces_memory_id: replacement || null,
      });
      setNotice('Conversation excerpt saved for review. Approve the candidate in Browse & review before agents use the fact.');
      setExcerpt(''); setSummary(''); setReplacement(''); setAuthorized(false);
      setCaptureId(crypto.randomUUID()); onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Capture failed. Retry unchanged content to check its saved receipt.');
    } finally { setSaving(false); }
  }

  return <section className="memory-intake panel" aria-labelledby="conversation-capture-heading">
    <div className="section-heading"><div><h2 id="conversation-capture-heading">Capture a company conversation</h2>
      <p>Provide a relevant visible excerpt and a fact to review. The source archive is kept separately from approved memory.</p></div></div>
    <form onSubmit={(event) => { void submit(event); }}>
      <fieldset disabled={saving} style={{ border: 0, padding: 0 }}>
        <label className="field">Source reference<input required maxLength={240} value={sourceRef} onChange={(e) => { setSourceRef(e.target.value); edited(); }} /></label>
        <label className="field">Source message time<input required type="datetime-local" value={sentAt} onChange={(e) => { setSentAt(e.target.value); edited(); }} /></label>
        <label className="field">Visible speaker<select value={speaker} onChange={(e) => { setSpeaker(e.target.value as typeof speaker); edited(); }}>
          <option value="user">User</option><option value="assistant">Visible assistant reply</option></select></label>
        <label className="field">Conversation excerpt<textarea required maxLength={4096} rows={5} value={excerpt} onChange={(e) => { setExcerpt(e.target.value); edited(); }} /></label>
        <label className="field">Fact for review<textarea required maxLength={4096} rows={3} value={summary} onChange={(e) => { setSummary(e.target.value); edited(); }} /></label>
        <label className="field">Correct an existing fact<select value={replacement} onChange={(e) => {
          setReplacement(e.target.value); const selected = memories.find((item) => item.id === e.target.value);
          if (selected) setScope(selected.scope); edited();
        }}><option value="">New fact</option>{memories.map((item) => <option key={item.id} value={item.id}>{item.text}</option>)}</select></label>
        <label className="field">Capture scope<select disabled={Boolean(replacement)} value={scope} onChange={(e) => { setScope(e.target.value as MemoryScope); edited(); }}>
          <option value="project">This project</option><option value="company">Company</option></select></label>
        <label className="field"><span><input type="checkbox" checked={authorized} onChange={(e) => { setAuthorized(e.target.checked); edited(); }} /> I authorize this company excerpt. It contains no secrets, private internal notes, or unrelated personal content.</span></label>
        <button className="button button-primary" type="submit" disabled={!authorized}>{saving ? 'Saving…' : 'Send excerpt for review'}</button>
      </fieldset>
    </form>
    {error && <p role="alert" className="form-error">{error}</p>}
    {notice && <p role="status" className="notice">{notice}</p>}
  </section>;
}
