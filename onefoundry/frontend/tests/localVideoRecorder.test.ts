import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { editLocalVideo, startLocalRecording, type VideoLimits } from '../src/localVideoRecorder';

class Recorder {
  static latest: Recorder;
  static isTypeSupported = () => true;
  state = 'inactive';
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  constructor() { Recorder.latest = this; }
  start() { this.state = 'recording'; }
  stop() { this.state = 'inactive'; queueMicrotask(() => this.onstop?.()); }
  data(content: string) { this.ondataavailable?.({ data: new Blob([content]) }); }
}
const limits: VideoLimits = { enabled: true, max_bytes: 1024, max_duration_seconds: 2,
  max_width: 1280, max_height: 720, frame_rate: 24 };
function source() {
  const track = Object.assign(new EventTarget(), { stop: vi.fn() });
  const stream = { getTracks: () => [track], getVideoTracks: () => [track] } as unknown as MediaStream;
  return { stream, track };
}
beforeEach(() => { vi.stubGlobal('MediaRecorder', Recorder); vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it('stops every source track at the duration bound and returns exact encoded bytes', async () => {
  const { stream, track } = source();
  const recording = startLocalRecording(stream, limits, new AbortController().signal);
  Recorder.latest.data('synthetic encoded chunk');
  await vi.advanceTimersByTimeAsync(2000);
  expect((await recording.finished).blob.size).toBe(23);
  expect(track.stop).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
});
it('discards an oversized recording and stops its sources', async () => {
  const { stream, track } = source();
  const recording = startLocalRecording(stream, limits, new AbortController().signal);
  const rejected = expect(recording.finished).rejects.toThrow('byte limit');
  Recorder.latest.data('x'.repeat(1025));
  await rejected;
  expect(track.stop).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
});
it('cancels without returning bytes or leaving capture tracks active', async () => {
  const { stream, track } = source(); const controller = new AbortController();
  const recording = startLocalRecording(stream, limits, controller.signal);
  const rejected = expect(recording.finished).rejects.toThrow('canceled');
  Recorder.latest.data('clip'); controller.abort(); await rejected;
  expect(track.stop).toHaveBeenCalledOnce();
});
it('rejects an empty encoder result and unavailable encoding', async () => {
  const { stream, track } = source();
  const recording = startLocalRecording(stream, limits, new AbortController().signal);
  const rejected = expect(recording.finished).rejects.toThrow('empty'); recording.stop(); await rejected;
  expect(track.stop).toHaveBeenCalledOnce();
  vi.stubGlobal('MediaRecorder', undefined);
  expect(() => startLocalRecording(stream, limits, new AbortController().signal)).toThrow('cannot record');
});
it('rejects invalid trim input before opening an audio context or retaining an object URL', async () => {
  const clip = { blob: new Blob(['clip']), duration_seconds: 2 };
  for (const [start, end] of [[-1, 2], [0, 3], [1, 1], [NaN, 2]]) {
    await expect(editLocalVideo(clip, start, end, 'Caption', limits, new AbortController().signal)).rejects.toThrow('valid trim');
  }
});
