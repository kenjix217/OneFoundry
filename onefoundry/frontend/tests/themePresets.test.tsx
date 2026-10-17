import { afterEach, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ThemePicker } from '../src/ThemePicker';
import { applyThemePreset, readThemePreset, THEME_PRESETS } from '../src/themePresets';

afterEach(() => {
  window.localStorage.clear();
  delete document.documentElement.dataset.uiTheme;
  document.documentElement.style.colorScheme = '';
});

it('offers the six DistrictAI color presets and the new black and grey theme', () => {
  expect(THEME_PRESETS.map((preset) => preset.id)).toEqual([
    'classic', 'dark-gold', 'dark-blue', 'dark-green', 'dark-violet', 'light-gold', 'light-blue', 'black-grey',
  ]);
  expect(THEME_PRESETS.map((preset) => preset.label)).not.toContain('Black White');
  expect(THEME_PRESETS.map((preset) => preset.label)).not.toContain('Light Black');
});

it('restores a saved theme and switches the whole document from the picker', async () => {
  applyThemePreset('dark-blue');
  render(<ThemePicker />);
  expect(screen.getByRole('combobox', { name: 'Color theme' })).toHaveValue('dark-blue');
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'Color theme' }), 'black-grey');
  expect(readThemePreset()).toBe('black-grey');
  expect(document.documentElement.dataset.uiTheme).toBe('black-grey');
  expect(document.documentElement.style.colorScheme).toBe('dark');
});

it('uses Classic for an invalid or inaccessible saved value', () => {
  expect(readThemePreset({ getItem: () => 'black-white' })).toBe('classic');
  expect(readThemePreset({ getItem: () => { throw new Error('blocked'); } })).toBe('classic');
  applyThemePreset('light-blue', { setItem: () => { throw new Error('blocked'); } });
  expect(document.documentElement.dataset.uiTheme).toBe('light-blue');
  expect(document.documentElement.style.colorScheme).toBe('light');
});
