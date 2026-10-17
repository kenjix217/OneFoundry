import { useState } from 'react';

import { applyThemePreset, readThemePreset, THEME_PRESETS, type ThemePreset } from './themePresets';

export function ThemePicker() {
  const [selected, setSelected] = useState<ThemePreset>(readThemePreset);

  function selectTheme(preset: ThemePreset) {
    applyThemePreset(preset);
    setSelected(preset);
  }

  return (
    <label className="theme-choice">
      <span>Color theme</span>
      <select aria-label="Color theme" value={selected} onChange={(event) => selectTheme(event.target.value as ThemePreset)}>
        {THEME_PRESETS.map((preset) => <option key={preset.id} value={preset.id}>{preset.label}</option>)}
      </select>
    </label>
  );
}
