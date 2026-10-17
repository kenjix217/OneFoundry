import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { WorkspaceTabs } from '../src/WorkspaceNavigation';
import { sections } from '../src/workspaceNavigation';

it('keeps Settings destinations in one accessible page selector without an overflowing tab strip', async () => {
  const navigate = vi.fn();
  const { rerender } = render(<WorkspaceTabs active="setup" businessMode="off" onNavigate={navigate} />);
  const picker = screen.getByRole('combobox', { name: 'Settings page' });
  expect(picker).toHaveValue('setup');
  expect(screen.queryByRole('button', { name: 'Team' })).not.toBeInTheDocument();
  expect(screen.getAllByRole('option').map((item) => item.textContent)).toEqual(sections[1].views.filter((item) => !['organizations', 'onefoundryInbox'].includes(item.view)).map((item) => item.label));
  await userEvent.setup().selectOptions(picker, 'team');
  expect(navigate).toHaveBeenCalledExactlyOnceWith('team');
  rerender(<WorkspaceTabs active="team" businessMode="off" onNavigate={navigate} />);
  expect(picker).toHaveValue('team');
  fireEvent.change(picker, { target: { value: 'missing-page' } });
  expect(navigate).toHaveBeenCalledTimes(1);
});

it('preserves the existing business-mode visibility rules', () => {
  render(<WorkspaceTabs active="setup" businessMode="inbox" onNavigate={vi.fn()} />);
  expect(screen.getAllByRole('option').map((item) => item.textContent)).toEqual(sections[1].views.map((item) => item.label));
});

it('does not add Settings navigation to the Chief conversation', () => {
  render(<WorkspaceTabs active="home" businessMode="off" onNavigate={vi.fn()} />);
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
});
