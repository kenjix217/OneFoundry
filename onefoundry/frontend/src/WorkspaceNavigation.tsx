import { sections, sectionForView, type View } from './workspaceNavigation';
import { WorkspaceIcon } from './WorkspaceIcon';

type Props = { active: View; businessMode: string; onNavigate: (view: View) => void };
function available(view: View, mode: string) {
  return (view !== 'organizations' || mode !== 'off') && (view !== 'onefoundryInbox' || mode === 'inbox');
}
export function WorkspaceNavigation({ active, onNavigate }: Props) {
  const current = sectionForView(active);
  return <nav className="workspace-navigation" aria-label="Project sections">{sections.map((section) => <button key={section.label} type="button" title={section.label}
    aria-label={section.label} aria-current={current === section ? 'page' : undefined}
    onClick={() => onNavigate(section.views[0].view)}><WorkspaceIcon name={section.icon} /><span>{section.label}</span>{current === section && <i aria-hidden="true" />}</button>)}</nav>;
}
export function WorkspaceTabs({ active, businessMode, onNavigate }: Props) {
  const current = sectionForView(active);
  if (current.label !== 'Settings') return null;
  const views = current.views.filter((item) => available(item.view, businessMode));
  if (views.length <= 1) return null;
  return <nav className="settings-page-menu" aria-label="Settings pages"><label>
    <span>Settings page</span><select value={active} onChange={(event) => {
      const destination = views.find((item) => item.view === event.target.value);
      if (destination) onNavigate(destination.view);
    }}>{views.map((item) => <option key={item.view} value={item.view}>{item.label}</option>)}</select>
  </label></nav>;
}
