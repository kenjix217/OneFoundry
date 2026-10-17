import { useEffect, useRef } from 'react';
import { ThemePicker } from './ThemePicker';
import { defaultWorkspacePreferences, type HomePanel, type WorkspacePreferences } from './workspacePreferences';

const LABELS: Record<HomePanel, string> = { attention: 'Needs your attention', work: 'Recent work', team: 'Your team' };
export function WorkspaceCustomize({ value, onChange, onClose }: { value: WorkspacePreferences; onChange: (value: WorkspacePreferences) => void; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) element.showModal();
    // Removing the dialog releases its native top layer. Calling close during
    // effect cleanup fires onClose and dismisses it during StrictMode's check.
  }, []);
  function move(panel: HomePanel, direction: number) {
    const order = [...value.homeOrder]; const index = order.indexOf(panel); const target = index + direction;
    if (target < 0 || target >= order.length) return;
    [order[index], order[target]] = [order[target], order[index]];
    onChange({ ...value, homeOrder: order });
  }
  return <dialog ref={dialog} className="workspace-customize" aria-labelledby="customize-title" onCancel={onClose} onClose={onClose}>
    <header><div><p className="eyebrow">Make it yours</p><h2 id="customize-title">Your workspace</h2></div><button type="button" className="button button-quiet" onClick={onClose} aria-label="Close customization">×</button></header>
    <p>Changes apply immediately. Saved on this browser.</p>
    <ThemePicker />
    <label className="field"><span className="field-label">Spacing</span><select value={value.density} onChange={(event) => onChange({ ...value, density: event.target.value as WorkspacePreferences['density'] })}><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select></label>
    <label className="field"><span className="field-label">Navigation</span><select value={value.sidebar} onChange={(event) => onChange({ ...value, sidebar: event.target.value as WorkspacePreferences['sidebar'] })}><option value="full">Labels and icons</option><option value="compact">Icons only</option></select></label>
    <label className="field"><span className="field-label">List panels</span><select value={value.panelSize} onChange={(event) => onChange({ ...value, panelSize: event.target.value as WorkspacePreferences['panelSize'] })}><option value="balanced">Balanced</option><option value="wide">More room for lists</option></select></label>
    <fieldset><legend>Home sections</legend>{value.homeOrder.map((panel, index) => <div className="customize-row" key={panel}>
      <label><input type="checkbox" checked={!value.hiddenHomePanels.includes(panel)} onChange={(event) => onChange({ ...value, hiddenHomePanels: event.target.checked ? value.hiddenHomePanels.filter((item) => item !== panel) : [...value.hiddenHomePanels, panel] })} />{LABELS[panel]}</label>
      <button type="button" aria-label={`Move ${LABELS[panel]} earlier`} disabled={index === 0} onClick={() => move(panel, -1)}>↑</button>
      <button type="button" aria-label={`Move ${LABELS[panel]} later`} disabled={index === value.homeOrder.length - 1} onClick={() => move(panel, 1)}>↓</button>
    </div>)}</fieldset>
    <footer><button type="button" className="button button-outline" onClick={() => onChange(defaultWorkspacePreferences())}>Reset layout</button><button type="button" className="button button-primary" onClick={onClose}>Done</button></footer>
  </dialog>;
}
