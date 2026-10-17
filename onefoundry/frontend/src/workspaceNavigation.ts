export type View = 'legacyWork' | 'home' | 'setup' | 'team' | 'models' | 'extensionHub' | 'integrations' | 'coding' | 'channels' | 'skills' | 'work' | 'chat' | 'activity' | 'approvals' | 'automation' | 'memory' | 'infra' | 'organizations' | 'onefoundryInbox' | 'connections';
export type WorkspaceSection = { label: string; icon: string; views: { view: View; label: string }[] };

export const sections: WorkspaceSection[] = [
  { label: 'Chief', icon: 'home', views: [{ view: 'home', label: 'Chief' }, { view: 'work', label: 'Conversations' }, { view: 'chat', label: 'Conversation' }] },
  { label: 'Settings', icon: 'settings', views: [
    { view: 'setup', label: 'Project setup' }, { view: 'team', label: 'Team' }, { view: 'models', label: 'AI connections' },
    { view: 'connections', label: 'Accounts & files' }, { view: 'integrations', label: 'Repositories' },
    { view: 'coding', label: 'Coding agents' }, { view: 'channels', label: 'Messaging' },
    { view: 'memory', label: 'Memory' }, { view: 'skills', label: 'Procedures' }, { view: 'approvals', label: 'Approvals' },
    { view: 'automation', label: 'Automation' }, { view: 'activity', label: 'Activity' },
    { view: 'organizations', label: 'Organizations' }, { view: 'onefoundryInbox', label: 'Follow-ups' },
    { view: 'infra', label: 'System health' }, { view: 'legacyWork', label: 'Legacy records' },
  ] },
  { label: 'Integrations', icon: 'integrations', views: [{ view: 'extensionHub', label: 'Integration Hub' }] },
];

export function sectionForView(view: View): WorkspaceSection {
  return sections.find((section) => section.views.some((item) => item.view === view))!;
}
export function viewLabel(view: View): string {
  return sectionForView(view).views.find((item) => item.view === view)!.label;
}
export function isView(value: string): value is View {
  return sections.some((section) => section.views.some((item) => item.view === value));
}
