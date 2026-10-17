import { useEffect, useRef, useState } from 'react';
import { editLocalVideo, startLocalRecording, type LocalClip, type Recording, type VideoLimits } from './localVideoRecorder';
import { getVideoLimits, listMediaCaptures, saveRecordedVideo, type CaptureRequest } from './mediaCaptureApi';

function LocalRecordingForm({ project, request, limits, onSaved }: {
  project: string; request: CaptureRequest; limits: VideoLimits; onSaved: () => void;
}) {
  const [clip, setClip] = useState<LocalClip | null>(null);
  const [edited, setEdited] = useState<LocalClip | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [stage, setStage] = useState<'idle' | 'recording' | 'editing' | 'saving'>('idle');
  const [error, setError] = useState<string | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [narrate, setNarrate] = useState(false);
  const [start, setStart] = useState(0); const [end, setEnd] = useState(0);
  const operation = useRef<AbortController | null>(null);
  const recording = useRef<Recording | null>(null);
  useEffect(() => () => operation.current?.abort(), []);
  useEffect(() => {
    const value = edited ?? clip;
    if (!value) return;
    const url = URL.createObjectURL(value.blob); setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [clip, edited]);

  async function record() {
    if (!reviewed || stage !== 'idle') return;
    setStage('recording'); setError(null); setClip(null); setEdited(null);
    const controller = new AbortController(); operation.current = controller;
    let display: MediaStream | null = null;
    try {
      if (!navigator.mediaDevices?.getDisplayMedia) throw new Error('Window recording is unavailable in this browser. Use a supported desktop browser.');
      display = await navigator.mediaDevices.getDisplayMedia({ video: {
        width: { max: limits.max_width }, height: { max: limits.max_height }, frameRate: { max: limits.frame_rate },
      }, audio: false });
      const surface = display.getVideoTracks()[0]?.getSettings().displaySurface;
      if (surface !== 'window' && surface !== 'browser') throw new Error('Select a single app window or browser tab. Whole-screen recording is not admitted.');
      if (narrate) {
        const microphone = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
        microphone.getAudioTracks().forEach((track) => display!.addTrack(track));
      }
      recording.current = startLocalRecording(display, limits, controller.signal);
      const saved = await recording.current.finished;
      setClip(saved); setStart(0); setEnd(saved.duration_seconds); setReviewed(false);
    } catch (cause) { display?.getTracks().forEach((track) => track.stop());
      setError(cause instanceof Error ? cause.message : 'Local recording failed.'); }
    finally { recording.current = null; operation.current = null; setStage('idle'); }
  }

  async function edit() {
    if (!clip || stage !== 'idle') return;
    setStage('editing'); setError(null); setEdited(null);
    const controller = new AbortController(); operation.current = controller;
    try { setEdited(await editLocalVideo(clip, start, end, request.caption, limits, controller.signal)); setReviewed(false); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Local editing failed.'); }
    finally { operation.current = null; setStage('idle'); }
  }

  async function save() {
    if (!edited || !reviewed || stage !== 'idle') return;
    setStage('saving'); setError(null);
    try { await saveRecordedVideo(project, request, edited.blob); onSaved(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Saving the local video failed.'); }
    finally { setStage('idle'); }
  }

  return <fieldset disabled={stage === 'saving'}>
    <legend>{request.title}</legend>
    <p>Select only the approved app window. Picture and optional microphone narration stay in this browser until you save to OneFoundry. No provider or paid voice service receives them.</p>
    <p>Limit: {limits.max_duration_seconds} seconds · {Math.floor(limits.max_bytes / 1024 / 1024)} MiB.</p>
    {error && <p className="form-error" role="alert">{error}</p>}
    <label><input type="checkbox" checked={narrate} disabled={stage !== 'idle'} onChange={(event) => setNarrate(event.target.checked)} />Record my microphone narration (separate browser permission)</label>
    <label><input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} />I selected/reviewed the approved app window and this clip contains no credentials or private unrelated data.</label>
    {!clip && stage === 'idle' && <button type="button" disabled={!reviewed} onClick={() => { void record(); }}>Record approved app window</button>}
    {stage === 'recording' && <button type="button" onClick={() => recording.current?.stop()}>Stop recording</button>}
    {(stage === 'recording' || stage === 'editing') && <button type="button" onClick={() => operation.current?.abort()}>Cancel and discard</button>}
    {preview && <video src={preview} controls playsInline style={{ width: '100%', maxHeight: '320px' }} aria-label={edited ? 'Local edited video preview' : 'Local recording preview'} />}
    {clip && stage === 'idle' && <>
      <label>Trim start (seconds)<input type="number" min={0} max={clip.duration_seconds} step="any" value={start}
        onChange={(event) => { setStart(Number(event.target.value)); setEdited(null); }} /></label>
      <label>Trim end (seconds)<input type="number" min={0} max={clip.duration_seconds} step="any" value={end}
        onChange={(event) => { setEnd(Number(event.target.value)); setEdited(null); }} /></label>
      <p>Caption to burn into the picture: {request.caption}</p>
      <button type="button" onClick={() => { void edit(); }}>Trim and burn caption</button>
      <button type="button" disabled={!edited || !reviewed} onClick={() => { void save(); }}>Save reviewed video locally</button>
    </>}
    {stage === 'editing' && <p role="status">Rendering the trimmed picture, caption and recorded audio locally…</p>}
    {stage === 'saving' && <p role="status">Saving the exact edited bytes to the local artifact owner…</p>}
  </fieldset>;
}

export function MediaCapturePanel({ projectId, orderId, canRecord, reloadKey }: { projectId: string; orderId: string; canRecord: boolean; reloadKey: string }) {
  const [requests, setRequests] = useState<CaptureRequest[]>([]);
  const [limits, setLimits] = useState<VideoLimits | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setRequests([]); setLimits(null); setError(null);
    void Promise.all([getVideoLimits(projectId, controller.signal), listMediaCaptures(projectId, orderId, controller.signal)])
      .then(([bounds, captures]) => { if (!controller.signal.aborted) { setLimits(bounds); setRequests(captures); } })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Video requests unavailable.'); });
    return () => controller.abort();
  }, [projectId, orderId, reload, reloadKey]);
  if (!error && requests.length === 0) return null;
  return <section aria-label="Local video requests"><h3>Local video requests</h3>
    {error && <p className="form-error" role="alert">{error}</p>}
    {requests.map((request) => <details key={request.capture_id} open={request.status === 'waiting_for_operator'}>
      <summary>{request.title} · {request.status.replaceAll('_', ' ')}</summary>
      {request.status === 'waiting_for_operator' && canRecord && limits?.enabled ?
        <LocalRecordingForm project={projectId} request={request} limits={limits} onSaved={() => setReload((value) => value + 1)} /> :
        <p>{request.status === 'ready' ? 'Video bytes are saved. Open Results to watch and verify the exact artifact before QA.' :
          request.status === 'pending' || request.status === 'unknown' ? 'The storage outcome is unconfirmed. Inspect its intent; do not re-upload or replay it.' : 'The task is closed or local recording is disabled.'}</p>}
    </details>)}
  </section>;
}
