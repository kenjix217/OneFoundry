import { useState } from 'react';
import type { Profile, Project } from './api';
import type { View } from './workspaceNavigation';
import { readWorkspacePreferences } from './workspacePreferences';
import { HomeView } from './HomeView';
import { WorkOrdersView } from './WorkOrdersView';
import { TaskChatView } from './TaskChatView';

/** Compatibility surface for historical records and capabilities, reachable through Settings. */
export function LegacyWorkspace({ project, profiles, onNavigate, onChief, onRun }: {
  project: Project; profiles: Profile[]; onNavigate: (view: View) => void;
  onChief: (orderId: string) => void; onRun: (runId: string) => void;
}) {
  const [task, setTask] = useState<string | null>(null);
  return <section aria-label="Legacy records"><p>Historical work, configuration and execution receipts. Direct ordinary work through Chief.</p>
    <button className="button button-outline" onClick={() => onNavigate('home')}>Return to Chief</button>
    {task ? <TaskChatView projectId={project.id} profiles={profiles} initialOrderId={task}
      onOpenWorkOrders={() => setTask(null)} onOpenTask={setTask} onManageOrder={() => setTask(null)}
      onOpenModels={() => onNavigate('models')} onOpenAutomation={() => onNavigate('automation')}
      onOpenActivity={() => onNavigate('activity')} onOpenRun={onRun} />
      : <WorkOrdersView projectId={project.id} profiles={profiles} onOpenTask={onChief} onOpenRun={onRun} />}
    <details><summary>Previous overview</summary><HomeView project={project} profiles={profiles}
      preferences={readWorkspacePreferences()} onCreateWork={() => onNavigate('home')} onCustomize={() => onNavigate('setup')}
      onNavigate={(view, orderId) => { if (orderId) setTask(orderId); else onNavigate(view); }} /></details>
  </section>;
}
