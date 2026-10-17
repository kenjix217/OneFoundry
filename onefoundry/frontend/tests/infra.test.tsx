import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { InfraView } from '../src/InfraView';

afterEach(() => vi.unstubAllGlobals());

it('shows stopped service and keeps monitoring read only', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    status: 'degraded', captured_at: '2026-09-26T12:00:00+00:00', reason: null,
    services: [
      { name: 'app', state: 'running', health: 'none' },
      { name: 'runner', state: 'stopped', health: 'none' },
    ],
  }), { status: 200 }));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<InfraView />);
  expect(await screen.findByText('Overall: degraded')).toBeInTheDocument();
  expect(screen.getByText('runner', { exact: false }).closest('li')).toHaveTextContent('stopped');
  await user.click(screen.getByRole('button', { name: 'Refresh view' }));
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(fetcher.mock.calls.every(([, init]) => init.method === undefined)).toBe(true);
  expect(screen.queryByRole('button', { name: /start|stop|restart/i })).not.toBeInTheDocument();
});
