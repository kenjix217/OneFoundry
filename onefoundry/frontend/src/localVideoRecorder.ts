// Local browser recording and rendering. No frame, audio or byte is sent to a provider.
export type VideoLimits = { enabled: boolean; max_bytes: number; max_duration_seconds: number;
  max_width: number; max_height: number; frame_rate: number };
export type LocalClip = { blob: Blob; duration_seconds: number };
export type Recording = { finished: Promise<LocalClip>; stop: () => void };

function webmType(): string {
  if (typeof MediaRecorder === 'undefined') throw new Error('This browser cannot record local video.');
  for (const type of ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm']) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  throw new Error('This browser cannot encode a supported WebM video.');
}

export function startLocalRecording(stream: MediaStream, limits: VideoLimits, signal: AbortSignal): Recording {
  if (signal.aborted) { stream.getTracks().forEach((track) => track.stop()); throw new Error('Recording canceled.'); }
  let recorder: MediaRecorder;
  try { recorder = new MediaRecorder(stream, { mimeType: webmType() }); }
  catch (cause) { stream.getTracks().forEach((track) => track.stop()); throw cause; }
  const started = performance.now();
  const chunks: Blob[] = [];
  let bytes = 0;
  let failure: Error | null = null;
  let timer: number | undefined;
  const stop = () => { if (recorder.state !== 'inactive') recorder.stop(); };
  const abort = () => { failure = new Error('Recording canceled; nothing was saved.'); stop(); };
  const ended = () => { stop(); };
  const finished = new Promise<LocalClip>((resolve, reject) => {
    recorder.ondataavailable = (event) => {
      bytes += event.data.size;
      if (bytes > limits.max_bytes) { failure = new Error('Recording exceeded its byte limit; nothing was saved.'); stop(); }
      else if (event.data.size) chunks.push(event.data);
    };
    recorder.onerror = () => { failure = new Error('The browser video encoder failed.'); stop(); };
    recorder.onstop = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      signal.removeEventListener('abort', abort);
      stream.getTracks().forEach((track) => { track.removeEventListener('ended', ended); track.stop(); });
      const blob = new Blob(chunks, { type: 'video/webm' });
      if (failure) reject(failure);
      else if (!blob.size || blob.size > limits.max_bytes) reject(new Error('The browser returned an empty or oversized recording.'));
      else resolve({ blob, duration_seconds: Math.min(limits.max_duration_seconds, (performance.now() - started) / 1000) });
    };
    signal.addEventListener('abort', abort, { once: true });
    stream.getVideoTracks().forEach((track) => track.addEventListener('ended', ended, { once: true }));
    try { recorder.start(Math.ceil(1000 / limits.frame_rate)); }
    catch { signal.removeEventListener('abort', abort); stream.getTracks().forEach((track) => track.stop());
      reject(new Error('The browser could not start its video encoder.')); return; }
    timer = window.setTimeout(stop, limits.max_duration_seconds * 1000);
  });
  return { finished, stop };
}

function mediaReady(video: HTMLVideoElement, event: 'loadedmetadata' | 'seeked', signal: AbortSignal,
  timeoutSeconds: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const cleanup = () => { window.clearTimeout(timer); video.removeEventListener(event, ready);
      video.removeEventListener('error', failed); signal.removeEventListener('abort', aborted); };
    const ready = () => { cleanup(); resolve(); };
    const failed = () => { cleanup(); reject(new Error('The recorded video cannot be decoded by this browser.')); };
    const aborted = () => { cleanup(); reject(new Error('Video editing canceled.')); };
    const timer = window.setTimeout(failed, timeoutSeconds * 1000);
    video.addEventListener(event, ready, { once: true }); video.addEventListener('error', failed, { once: true });
    signal.addEventListener('abort', aborted, { once: true });
    if (signal.aborted) aborted();
  });
}

function captionFrame(context: CanvasRenderingContext2D, caption: string, width: number, height: number) {
  const font = Math.max(12, Math.round(width / 36));
  context.font = `${font}px sans-serif`;
  const lines: string[] = [];
  let line = '';
  for (const word of caption.split(/\s+/)) {
    const candidate = line ? `${line} ${word}` : word;
    if (line && context.measureText(candidate).width > width * 0.9) { lines.push(line); line = word; }
    else line = candidate;
  }
  if (line) lines.push(line);
  const lineHeight = font * 1.35;
  const boxHeight = lineHeight * lines.length + font;
  context.fillStyle = 'rgba(0,0,0,0.85)'; context.fillRect(0, height - boxHeight, width, boxHeight);
  context.fillStyle = 'white'; context.textAlign = 'center'; context.textBaseline = 'middle';
  lines.forEach((text, index) => context.fillText(text, width / 2, height - boxHeight + font + index * lineHeight, width * 0.9));
}

export async function editLocalVideo(clip: LocalClip, start: number, end: number, caption: string,
  limits: VideoLimits, signal: AbortSignal): Promise<LocalClip> {
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start ||
      end > clip.duration_seconds || end - start > limits.max_duration_seconds || !caption.trim() || caption.length > 240) {
    throw new Error('Choose a valid trim range and the requested bounded caption.');
  }
  const video = document.createElement('video'); video.playsInline = true;
  const url = URL.createObjectURL(clip.blob);
  const canvas = document.createElement('canvas');
  let frame = 0; let audio: AudioContext | null = null; let recording: Recording | null = null;
  try {
    // Start an audio context in this user gesture. It routes existing narration
    // into the edited clip, never to a cloud service or the speakers.
    audio = new AudioContext(); await audio.resume();
    const ready = mediaReady(video, 'loadedmetadata', signal, limits.max_duration_seconds);
    video.src = url; video.load(); await ready;
    if (!video.videoWidth || !video.videoHeight) throw new Error('The recording has no decodable picture.');
    const scale = Math.min(1, limits.max_width / video.videoWidth, limits.max_height / video.videoHeight);
    canvas.width = Math.floor(video.videoWidth * scale); canvas.height = Math.floor(video.videoHeight * scale);
    const context = canvas.getContext('2d');
    if (!context) throw new Error('The browser cannot render this video.');
    if (start > 0) { const seeked = mediaReady(video, 'seeked', signal, limits.max_duration_seconds);
      video.currentTime = start; await seeked; }
    const output = canvas.captureStream(limits.frame_rate);
    const destination = audio.createMediaStreamDestination();
    audio.createMediaElementSource(video).connect(destination);
    destination.stream.getAudioTracks().forEach((track) => output.addTrack(track));
    const draw = () => { context.drawImage(video, 0, 0, canvas.width, canvas.height);
      captionFrame(context, caption, canvas.width, canvas.height);
      if (video.ended || video.currentTime >= end) { recording?.stop(); return; }
      frame = requestAnimationFrame(draw); };
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    captionFrame(context, caption, canvas.width, canvas.height);
    recording = startLocalRecording(output, { ...limits, max_duration_seconds: end - start }, signal);
    video.onended = () => recording?.stop();
    await video.play(); draw();
    return await recording.finished;
  } finally {
    cancelAnimationFrame(frame); video.pause(); recording?.stop();
    if (recording) await recording.finished.catch(() => undefined);
    if (audio) await audio.close();
    video.removeAttribute('src'); video.load(); URL.revokeObjectURL(url);
  }
}
