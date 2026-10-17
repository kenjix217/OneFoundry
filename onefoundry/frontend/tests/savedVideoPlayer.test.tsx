import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SavedVideoPlayer } from '../src/SavedVideoPlayer';
import { verifyVideoPlayback } from '../src/mediaCaptureApi';
vi.mock('../src/mediaCaptureApi', () => ({ savedVideoUrl: () => '/synthetic.webm', verifyVideoPlayback: vi.fn() }));
afterEach(() => vi.clearAllMocks());
const props = { projectId: 'p', runId: 'r', artifactId: 'a', sha256: 'b'.repeat(64), verified: false };
it('opens the same saved bytes for full-frame review without a playback decision', () => {
  const { container } = render(<SavedVideoPlayer {...props} />);
  const link = screen.getByRole('link', { name: 'Open video' });
  expect(link).toHaveAttribute('href', container.querySelector('video')!.getAttribute('src'));
  expect(link).toHaveAttribute('target', '_blank');
  expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  expect(verifyVideoPlayback).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: /I watched and verified/ })).toBeDisabled();
});
it('requires playback and an explicit hash-bound operator decision without accepting work', async () => {
  const { container } = render(<SavedVideoPlayer {...props} />);
  const button = screen.getByRole('button', { name: /I watched and verified/ });
  expect(button).toBeDisabled(); expect(verifyVideoPlayback).not.toHaveBeenCalled();
  fireEvent.ended(container.querySelector('video')!);
  await userEvent.setup().click(button);
  expect(verifyVideoPlayback).toHaveBeenCalledWith('p', 'r', 'a', props.sha256);
  expect(await screen.findByText(/Independent QA and work-order acceptance remain separate/)).toBeInTheDocument();
});
it('a decoder failure blocks playback attestation', () => {
  const { container } = render(<SavedVideoPlayer {...props} />);
  fireEvent.error(container.querySelector('video')!); fireEvent.ended(container.querySelector('video')!);
  expect(screen.getByRole('alert')).toHaveTextContent('cannot play');
  expect(screen.getByRole('button', { name: /I watched and verified/ })).toBeDisabled();
  expect(verifyVideoPlayback).not.toHaveBeenCalled();
});
it('a failed verification remains a failure and allows an explicit retry', async () => {
  vi.mocked(verifyVideoPlayback).mockRejectedValueOnce(new Error('Gateway unavailable'));
  const { container } = render(<SavedVideoPlayer {...props} />);
  fireEvent.ended(container.querySelector('video')!);
  await userEvent.setup().click(screen.getByRole('button', { name: /I watched and verified/ }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Gateway unavailable');
  expect(screen.getByRole('button', { name: /I watched and verified/ })).toBeEnabled();
});
