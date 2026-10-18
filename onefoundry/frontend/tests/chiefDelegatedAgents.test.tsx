import { expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChiefDelegatedAgents } from '../src/ChiefDelegatedAgents';
import type { Profile } from '../src/api';
import type { TeamAssignment, TeamChild } from '../src/taskSessionApi';

const profiles = [{ id: 'researcher', name: 'Researcher' }, { id: 'engineer', name: 'Engineer' }] as Profile[];
function child(id: string, status: TeamChild['status'] = 'closed', profile_id = 'researcher'): TeamChild {
  return { id, status, profile_id, parent_session_id: 'chief' };
}
function assignment(id: string, status: TeamAssignment['status'], objective = `Objective ${id}`): TeamAssignment {
  return { id: `assignment-${id}`, child_session_id: id, status, objective, expected_result: '', acceptance_check: '', result_message_id: null };
}
function renderAgents(children: TeamChild[], assignments: TeamAssignment[]) {
  const onOpen = vi.fn();
  render(<ChiefDelegatedAgents children={children} assignments={assignments} profiles={profiles} onOpen={onOpen} />);
  return onOpen;
}

it('shows unfinished and working agents while keeping completed work behind a disclosure', async () => {
  const onOpen = renderAgents(
    [child('pending'), child('ready'), child('active'), child('blocked'), child('working', 'working', 'engineer'), child('done')],
    [assignment('pending', 'pending'), assignment('ready', 'ready'), assignment('active', 'active'),
      assignment('blocked', 'blocked'), assignment('working', 'satisfied', 'Repair the runner'), assignment('done', 'satisfied')],
  );
  const nav = screen.getByRole('navigation', { name: 'Delegated agents' });
  expect(within(nav).getByText('Past agent work (1)')).toBeInTheDocument();
  for (const [id, status] of [['pending', 'Waiting'], ['ready', 'Ready'], ['active', 'Assigned'], ['blocked', 'Blocked']]) {
    expect(within(nav).getByRole('button', { name: `Open Researcher: Objective ${id} (${status})` })).toBeVisible();
  }
  expect(within(nav).getByRole('button', { name: 'Open Engineer: Repair the runner (Working)' })).toBeVisible();
  expect(within(nav).getByRole('button', { name: 'Open Researcher: Objective done (Past)' })).not.toBeVisible();
  await userEvent.click(within(nav).getByRole('button', { name: 'Open Researcher: Objective blocked (Blocked)' }));
  expect(onOpen).toHaveBeenCalledWith('blocked');
  await userEvent.click(within(nav).getByText('Past agent work (1)'));
  await userEvent.click(within(nav).getByRole('button', { name: 'Open Researcher: Objective done (Past)' }));
  expect(onOpen).toHaveBeenLastCalledWith('done');
});

it('renders no navigation for an empty child list', () => {
  renderAgents([], []);
  expect(screen.queryByRole('navigation', { name: 'Delegated agents' })).not.toBeInTheDocument();
});

it('keeps all historical agents available with assignment context and no session IDs in labels', async () => {
  const onOpen = renderAgents([child('session-secret-1'), child('session-secret-2', 'closed', 'engineer')],
    [assignment('session-secret-1', 'satisfied', 'Read the market'), assignment('session-secret-2', 'canceled', 'Check   pricing\n evidence')]);
  const nav = screen.getByRole('navigation', { name: 'Delegated agents' });
  expect(within(nav).getByText('Past agent work (2)')).toBeInTheDocument();
  expect(within(nav).getByRole('button', { name: 'Open Researcher: Read the market (Past)' })).not.toBeVisible();
  await userEvent.click(within(nav).getByText('Past agent work (2)'));
  const engineer = within(nav).getByRole('button', { name: 'Open Engineer: Check pricing evidence (Past)' });
  expect(engineer).toHaveAttribute('title', 'Engineer: Check pricing evidence · Past');
  expect(nav.textContent).not.toContain('session-secret');
  await userEvent.click(engineer);
  expect(onOpen).toHaveBeenCalledWith('session-secret-2');
});

it('bounds long assignment labels, removes trace IDs, and opens the exact child', async () => {
  const id = 'child-47';
  const objective = `Check 91234567-89ab-cdef-0123-456789abcdef and ${'a'.repeat(64)} then ${'evidence '.repeat(60)}report findings`;
  const onOpen = renderAgents([child(id, 'working')], [assignment(id, 'active', objective)]);
  const button = screen.getByRole('button', { name: /^Open Researcher: Check and then evidence/ });
  expect(button.getAttribute('aria-label')?.length).toBeLessThan(140);
  expect(button.getAttribute('title')?.length).toBeLessThan(200);
  expect(button.getAttribute('aria-label')).not.toMatch(/91234567|aaaaaa/);
  expect(button.getAttribute('title')).not.toMatch(/91234567|aaaaaa/);
  await userEvent.click(button);
  expect(onOpen).toHaveBeenCalledWith(id);
});
