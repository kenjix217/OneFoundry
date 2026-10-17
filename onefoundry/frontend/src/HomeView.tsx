import { useEffect, useState } from 'react';
import type { Profile, Project } from './api';
import { listApprovals, listWorkOrders, type Approval, type WorkOrder } from './workApi';
import { workOrderStatusLabel } from './workOrderActivity';
import type { View } from './workspaceNavigation';
import type { HomePanel, WorkspacePreferences } from './workspacePreferences';
import { approvalPresentation } from './approvalPresentation';
import { WorkspaceIcon } from './WorkspaceIcon';

type Props = { project: Project; profiles: Profile[]; preferences: WorkspacePreferences;
  onNavigate: (view: View, orderId?: string) => void; onCustomize: () => void; onCreateWork?: () => void };

export function HomeView({ project, profiles, preferences, onNavigate, onCustomize, onCreateWork }: Props) {
  const [orders, setOrders] = useState<WorkOrder[] | null>(null);
  const [approvals, setApprovals] = useState<Approval[] | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setOrders(null); setApprovals(null); setErrors([]);
    void Promise.allSettled([listWorkOrders(project.id, controller.signal), listApprovals(project.id, 'pending', controller.signal)])
      .then(([work, inbox]) => {
        if (controller.signal.aborted) return;
        const failures: string[] = [];
        if (work.status === 'fulfilled') setOrders(work.value); else failures.push('Work could not be loaded.');
        if (inbox.status === 'fulfilled') setApprovals(inbox.value); else failures.push('Approvals could not be loaded.');
        setErrors(failures);
      });
    return () => controller.abort();
  }, [project.id, refresh]);
  const attention = orders?.filter((item) => ['needs_review', 'blocked', 'unknown', 'failed'].includes(item.status));
  const active = orders?.filter((item) => item.status === 'running').length;
  const pending = approvals && attention ? approvals.length + attention.length : null;
  const visible = preferences.homeOrder.filter((item) => !preferences.hiddenHomePanels.includes(item));
  const headings: Record<HomePanel, string> = { attention: 'Needs your attention', work: 'Recent work', team: 'Your team' };
  return <section className="home-workspace" aria-labelledby="home-title">
    <header className="home-heading"><div><p className="eyebrow">Your workspace, in focus</p><h1 id="home-title">{project.name}</h1><p>{project.goal || 'A clear place to direct your team and move work forward.'}</p></div>
      <button type="button" className="button button-primary" onClick={() => onCreateWork ? onCreateWork() : onNavigate('work')}>Create work <span aria-hidden="true">+</span></button></header>
    {(!project.goal || !project.desired_outcome || profiles.every((profile) => !profile.model)) && <div className="home-setup"><span className="home-setup-icon"><WorkspaceIcon name="spark" /></span><div><strong>Give your team a good starting point</strong><p>{!project.goal || !project.desired_outcome ? 'Add a project brief, then connect an AI account when you are ready.' : 'Your brief is ready. Connect an AI account to work with your agents.'}</p></div><button className="button button-outline" type="button" onClick={() => onNavigate(!project.goal || !project.desired_outcome ? 'setup' : 'models')}>{!project.goal || !project.desired_outcome ? 'Continue setup' : 'Connect AI'} <span aria-hidden="true">→</span></button></div>}
    <div className="home-stats">
      <button type="button" onClick={() => onNavigate('work')}><span>Work in progress</span><strong>{active ?? '—'}</strong><small>Open your work queue <span aria-hidden="true">↗</span></small></button>
      <button type="button" onClick={() => onNavigate('approvals')}><span>Pending approvals</span><strong>{approvals?.length ?? '—'}</strong><small>Review requested actions <span aria-hidden="true">↗</span></small></button>
      <button type="button" onClick={() => onNavigate('team')}><span>Your agents</span><strong>{profiles.length}</strong><small>Roles and capabilities <span aria-hidden="true">↗</span></small></button>
    </div>
    {errors.length > 0 && <div className="notice" role="alert">{errors.join(' ')}<button className="button button-outline" type="button" onClick={() => setRefresh((value) => value + 1)}>Try again</button></div>}
    <div className="home-panels" style={{ gridTemplateColumns: `repeat(${Math.max(visible.length, 1)}, minmax(0, 1fr))` }}>
      {visible.map((panel) => <section className="home-panel" key={panel} aria-label={headings[panel]}>
        <header><h2>{headings[panel]}</h2><span className="home-panel-count">{panel === 'attention' ? pending ?? '—' : panel === 'work' ? orders?.length ?? '—' : profiles.length}</span></header>
        <div className="home-panel-scroll" tabIndex={0}>
          {panel === 'attention' && <>{!orders && !approvals && errors.length === 0 && <p role="status">Checking saved work…</p>}
            {pending === 0 && <div className="home-empty"><span aria-hidden="true">✓</span><h3>Nothing waiting on you</h3><p>Your approvals and work needing review will appear here.</p></div>}
            {approvals?.map((item) => <button className="home-item" key={item.id} onClick={() => onNavigate('approvals')}><span className="status-chip status-waiting">Approval needed</span><strong>{approvalPresentation(item).target}</strong><small>Review before this action proceeds <span aria-hidden="true">→</span></small></button>)}
            {attention?.map((item) => <button className="home-item" key={item.id} onClick={() => onNavigate('chat', item.id)}><span className={`status-chip status-${item.status}`}>{workOrderStatusLabel(item.status)}</span><strong>{item.title}</strong><small>{profiles.find((profile) => profile.id === item.profile_id)?.name ?? 'Assigned agent'} <span aria-hidden="true">→</span></small></button>)}</>}
          {panel === 'work' && <>{!orders && errors.length === 0 && <p role="status">Loading work…</p>}{orders?.length === 0 && <div className="home-empty"><WorkspaceIcon name="work" /><h3>Start with one clear outcome</h3><p>Create an assignment and choose the agent who will handle it.</p><button className="button button-primary" type="button" onClick={() => onNavigate('work')}>Create your first work order</button></div>}
            {orders?.map((item) => <button className="home-item" key={item.id} onClick={() => onNavigate('chat', item.id)}><span className={`status-chip status-${item.status}`}>{workOrderStatusLabel(item.status)}</span><strong>{item.title}</strong><small>{profiles.find((profile) => profile.id === item.profile_id)?.name ?? 'Assigned agent'} <span aria-hidden="true">→</span></small></button>)}</>}
          {panel === 'team' && <>{profiles.length === 0 && <p>No agents yet. Add an agent in Team.</p>}{profiles.map((profile) => <button className="home-agent" key={profile.id} onClick={() => onNavigate('team')}><span className="agent-monogram">{profile.name.split(' ').map((part) => part[0]).slice(0, 2).join('').toUpperCase()}</span><span><strong>{profile.name}</strong><small>{profile.model ? 'Model selected' : 'Choose a model'}</small></span><span aria-hidden="true">→</span></button>)}<p className="home-panel-note">Each agent has its own instructions, model, and approved capabilities.</p></>}
        </div><footer><button type="button" onClick={() => onNavigate(panel === 'attention' ? 'approvals' : panel === 'work' ? 'work' : 'team')}>{panel === 'attention' ? 'Open inbox' : panel === 'work' ? 'View all work' : 'Manage team'} <span aria-hidden="true">→</span></button></footer>
      </section>)}
      {visible.length === 0 && <div className="home-empty"><h2>A little room to think.</h2><p>Add the sections you want on your Home screen.</p><button type="button" className="button button-outline" onClick={onCustomize}>Customize Home</button></div>}
    </div>
  </section>;
}
