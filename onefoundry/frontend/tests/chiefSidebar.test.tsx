import { expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChiefSidebar } from '../src/ChiefSidebar';
import type { ChiefConversation } from '../src/chiefWorkspaceApi';

const row = { id: 'order-1', title: 'Product brief', state: 'Completed', archived: false, trashed: false } as ChiefConversation;
function sidebar(rows = [row], disabled = false) {
  const actions = { onSelect: vi.fn(), onArchive: vi.fn(), onRestore: vi.fn(), onTrash: vi.fn() };
  render(<ChiefSidebar projectName="Studio" rows={rows} selectedId={null} disabled={disabled} settingsOpen={false} integrationsOpen={false}
    onProjects={vi.fn()} onSettings={vi.fn()} onIntegrations={vi.fn()} {...actions} />);
  return actions;
}

it('archives any conversation directly from its row without selecting it', async () => {
  const actions = sidebar();
  await userEvent.click(screen.getByRole('button', { name: 'Archive Product brief' }));
  expect(actions.onArchive).toHaveBeenCalledWith(row);
  expect(actions.onSelect).not.toHaveBeenCalled();
});

it('offers restore and recoverable deletion beside each archived chat', async () => {
  const archived = { ...row, archived: true };
  const actions = sidebar([archived]);
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: 'Archived' }));
  await user.click(screen.getByRole('button', { name: 'Restore Product brief' }));
  await user.click(screen.getByRole('button', { name: 'Move Product brief to Trash' }));
  expect(actions.onRestore).toHaveBeenCalledWith(archived);
  expect(actions.onTrash).toHaveBeenCalledWith(archived);
  expect(actions.onSelect).not.toHaveBeenCalled();
});

it('prevents archive controls while a submission is unresolved', () => {
  sidebar([row], true);
  expect(screen.getByRole('button', { name: 'Archive Product brief' })).toBeDisabled();
});

it('keeps Trash recovery available without opening a deleted conversation', async () => {
  const trashed = { ...row, archived: true, trashed: true };
  const actions = sidebar([trashed]);
  await userEvent.click(screen.getByRole('button', { name: 'Trash' }));
  await userEvent.click(screen.getByRole('button', { name: 'Restore Product brief from Trash' }));
  expect(actions.onRestore).toHaveBeenCalledWith(trashed);
  expect(actions.onSelect).not.toHaveBeenCalled();
});
