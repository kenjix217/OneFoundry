import { StrictMode, useState } from 'react';
import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { WorkspaceCustomize } from '../src/WorkspaceCustomize';
import { defaultWorkspacePreferences } from '../src/workspacePreferences';

it('keeps Customize open when StrictMode checks effect cleanup', () => {
  vi.spyOn(HTMLDialogElement.prototype, 'showModal').mockImplementation(function (this: HTMLDialogElement) { this.open = true; });
  vi.spyOn(HTMLDialogElement.prototype, 'close').mockImplementation(function (this: HTMLDialogElement) {
    this.open = false;
    this.dispatchEvent(new Event('close'));
  });
  function Host() {
    const [open, setOpen] = useState(true);
    return open ? <WorkspaceCustomize value={defaultWorkspacePreferences()} onChange={vi.fn()} onClose={() => setOpen(false)} /> : null;
  }
  render(<StrictMode><Host /></StrictMode>);
  expect(screen.getByRole('dialog', { name: 'Your workspace' })).toHaveAttribute('open');
  expect(screen.getByRole('combobox', { name: 'Color theme' })).toBeInTheDocument();
});
