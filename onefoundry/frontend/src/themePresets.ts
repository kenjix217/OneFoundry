export const THEME_PRESETS = [
  { id: 'classic', label: 'OneFoundry Classic', mode: 'light' },
  { id: 'dark-gold', label: 'Dark Gold', mode: 'dark' },
  { id: 'dark-blue', label: 'Dark Blue', mode: 'dark' },
  { id: 'dark-green', label: 'Evergreen', mode: 'dark' },
  { id: 'dark-violet', label: 'Midnight Violet', mode: 'dark' },
  { id: 'light-gold', label: 'Light Gold', mode: 'light' },
  { id: 'light-blue', label: 'Light Blue', mode: 'light' },
  { id: 'black-grey', label: 'Black & Grey', mode: 'dark' },
] as const;

export type ThemePreset = (typeof THEME_PRESETS)[number]['id'];

const STORAGE_KEY = 'onefoundry-theme-preset';
const DEFAULT_PRESET: ThemePreset = 'classic';

export function readThemePreset(storage: Pick<Storage, 'getItem'> | null = browserStorage()): ThemePreset {
  try {
    const saved = storage?.getItem(STORAGE_KEY);
    return THEME_PRESETS.some((preset) => preset.id === saved) ? saved as ThemePreset : DEFAULT_PRESET;
  } catch {
    return DEFAULT_PRESET;
  }
}

export function applyThemePreset(preset: ThemePreset, storage: Pick<Storage, 'setItem'> | null = browserStorage()): void {
  const selected = THEME_PRESETS.find((item) => item.id === preset);
  if (!selected) return;
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.uiTheme = selected.id;
    document.documentElement.style.colorScheme = selected.mode;
  }
  try {
    storage?.setItem(STORAGE_KEY, selected.id);
  } catch {
    // The chosen theme remains active when device storage is unavailable.
  }
}

function browserStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try { return window.localStorage; } catch { return null; }
}
