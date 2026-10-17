import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MediaCapturePanel } from '../src/MediaCapturePanel';
import { editLocalVideo, startLocalRecording } from '../src/localVideoRecorder';
import { getVideoLimits, listMediaCaptures, saveRecordedVideo } from '../src/mediaCaptureApi';
vi.mock('../src/mediaCaptureApi', () => ({ getVideoLimits: vi.fn(), listMediaCaptures: vi.fn(), saveRecordedVideo: vi.fn() }));
vi.mock('../src/localVideoRecorder', () => ({ editLocalVideo: vi.fn(), startLocalRecording: vi.fn() }));
const request = { capture_id: 'capture', run_id: 'run', title: 'Record approved fixture', caption: 'Synthetic caption', status: 'waiting_for_operator' as const };
const descriptor = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices');
afterEach(() => {
  vi.clearAllMocks(); vi.unstubAllGlobals();
  if (descriptor) Object.defineProperty(navigator, 'mediaDevices', descriptor);
  else Reflect.deleteProperty(navigator, 'mediaDevices');
});
function fixture(surface = 'window') {
  vi.mocked(getVideoLimits).mockResolvedValue({ enabled: true, max_bytes: 1024, max_duration_seconds: 2, max_width: 640, max_height: 360, frame_rate: 24 });
  vi.mocked(listMediaCaptures).mockResolvedValue([request]);
  const track = { getSettings: () => ({ displaySurface: surface }), stop: vi.fn() };
  const stream = { getVideoTracks: () => [track], getTracks: () => [track] } as unknown as MediaStream;
  const getDisplayMedia = vi.fn(async () => stream);
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getDisplayMedia } });
  const clip = { blob: new Blob(['synthetic']), duration_seconds: 2 };
  vi.mocked(startLocalRecording).mockReturnValue({ finished: Promise.resolve(clip), stop: vi.fn() });
  vi.mocked(editLocalVideo).mockResolvedValue(clip);
  vi.stubGlobal('URL', class extends URL { static createObjectURL = vi.fn(() => 'blob:synthetic'); static revokeObjectURL = vi.fn(); });
  render(<MediaCapturePanel projectId="project" orderId="order" canRecord={true} reloadKey="fixture" />);
  return { getDisplayMedia, track, clip };
}
it('requires explicit selection, editing and a fresh review before saving local bytes', async () => {
  const { getDisplayMedia, clip } = fixture(); const user = userEvent.setup();
  const record = await screen.findByRole('button', { name: 'Record approved app window' });
  expect(record).toBeDisabled(); expect(getDisplayMedia).not.toHaveBeenCalled();
  await user.click(screen.getByRole('checkbox', { name: /I selected\/reviewed/ })); await user.click(record);
  expect(getDisplayMedia).toHaveBeenCalledOnce();
  const save = await screen.findByRole('button', { name: 'Save reviewed video locally' });
  expect(save).toBeDisabled(); expect(saveRecordedVideo).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: 'Trim and burn caption' }));
  await waitFor(() => expect(screen.queryByText(/Rendering the trimmed picture/)).not.toBeInTheDocument());
  expect(save).toBeDisabled();
  await user.click(screen.getByRole('checkbox', { name: /I selected\/reviewed/ })); await waitFor(() => expect(screen.getByRole('button', { name: 'Save reviewed video locally' })).toBeEnabled()); await user.click(screen.getByRole('button', { name: 'Save reviewed video locally' }));
  expect(saveRecordedVideo).toHaveBeenCalledWith('project', request, clip.blob);
});
it('rejects whole-screen selection and stops its capture tracks', async () => {
  const { track } = fixture('monitor'); const user = userEvent.setup();
  await screen.findByRole('button', { name: 'Record approved app window' });
  await user.click(screen.getByRole('checkbox', { name: /I selected\/reviewed/ }));
  await user.click(screen.getByRole('button', { name: 'Record approved app window' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Whole-screen recording is not admitted');
  expect(track.stop).toHaveBeenCalledOnce(); expect(startLocalRecording).not.toHaveBeenCalled();
  expect(saveRecordedVideo).not.toHaveBeenCalled();
});
