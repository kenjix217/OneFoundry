export type HomePanel = 'attention' | 'work' | 'team';
export type WorkspacePreferences = {
  density: 'comfortable' | 'compact';
  sidebar: 'full' | 'compact';
  panelSize: 'balanced' | 'wide';
  homeOrder: HomePanel[];
  hiddenHomePanels: HomePanel[];
};
const KEY = 'onefoundry:workspace-layout:v1';
const PANELS: HomePanel[] = ['attention', 'work', 'team'];
export function defaultWorkspacePreferences(): WorkspacePreferences {
  return { density: 'comfortable', sidebar: 'full', panelSize: 'balanced', homeOrder: [...PANELS], hiddenHomePanels: [] };
}
function browserStorage(): Storage | null {
  try { return window.localStorage; } catch { return null; }
}
export function readWorkspacePreferences(storage: Pick<Storage, 'getItem'> | null = browserStorage()): WorkspacePreferences {
  const defaults = defaultWorkspacePreferences();
  try {
    const value: unknown = JSON.parse(storage?.getItem(KEY) ?? 'null');
    if (!value || typeof value !== 'object' || Array.isArray(value)) return defaults;
    const p = value as Record<string, unknown>;
    const homeOrder = Array.isArray(p.homeOrder) ? p.homeOrder.filter((item): item is HomePanel => PANELS.includes(item)) : [];
    return { density: p.density === 'compact' ? 'compact' : 'comfortable', sidebar: p.sidebar === 'compact' ? 'compact' : 'full',
      panelSize: p.panelSize === 'wide' ? 'wide' : 'balanced',
      homeOrder: [...new Set([...homeOrder, ...PANELS])],
      hiddenHomePanels: Array.isArray(p.hiddenHomePanels) ? [...new Set(p.hiddenHomePanels.filter((item): item is HomePanel => PANELS.includes(item)))] : [] };
  } catch { return defaults; }
}
export function saveWorkspacePreferences(value: WorkspacePreferences, storage: Pick<Storage, 'setItem'> | null = browserStorage()): boolean {
  try { if (!storage) return false; storage.setItem(KEY, JSON.stringify(value)); return true; } catch { return false; }
}
