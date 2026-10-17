import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../src/App';

const project = { id: 'project-1', name: 'Studio', goal: '', desired_outcome: '', created_at: '2026-09-25T00:00:00Z' };
const json = (body: unknown) => new Response(JSON.stringify(body));
function setup() {
  const fetcher = vi.fn(async (path: string) => {
    if (path === '/api/projects') return json([project]);
    if (path === '/api/projects/project-1') return json(project);
    return json([]);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  return { user: userEvent.setup(), fetcher };
}
afterEach(() => { vi.unstubAllGlobals(); window.localStorage.clear(); window.history.replaceState(null, '', '/'); });

it('keeps the conversation sidebar and unsent message when entering and leaving Settings', async () => {
  const { user, fetcher } = setup();
  const composer = await screen.findByRole('textbox', { name: 'Message Chief' });
  await user.type(composer, 'A draft I have not sent.');
  const sidebar = screen.getByRole('complementary', { name: 'Conversation sidebar' });
  await user.click(within(sidebar).getByRole('button', { name: 'Settings' }));
  expect(screen.getByRole('complementary', { name: 'Conversation sidebar' })).toBe(sidebar);
  expect(within(sidebar).getByRole('button', { name: /New conversation/ })).toBeVisible();
  expect(within(sidebar).getByRole('button', { name: 'Archived' })).toBeVisible();
  expect(within(sidebar).getByRole('button', { name: 'Settings' })).toHaveAttribute('aria-current', 'page');
  expect(screen.queryByRole('textbox', { name: 'Message Chief' })).not.toBeInTheDocument();
  await user.selectOptions(screen.getByRole('combobox', { name: 'Settings page' }), 'team');
  expect(screen.getByRole('complementary', { name: 'Conversation sidebar' })).toBe(sidebar);
  await user.click(screen.getByRole('button', { name: 'Back to conversation' }));
  expect(await screen.findByRole('textbox', { name: 'Message Chief' })).toHaveValue('A draft I have not sent.');
  expect(fetcher.mock.calls.every(([path]) => !path.endsWith('/messages'))).toBe(true);
});

it('opens a direct Settings route with the same sidebar and returns to a new conversation', async () => {
  window.history.replaceState(null, '', '/#project/project-1/team');
  const { user } = setup();
  await screen.findByRole('complementary', { name: 'Conversation sidebar' });
  expect(await screen.findByRole('combobox', { name: 'Settings page' })).toHaveValue('team');
  await user.click(screen.getByRole('button', { name: 'Back to conversation' }));
  expect(await screen.findByRole('combobox', { name: 'Conversation agent' })).toHaveValue('');
});
