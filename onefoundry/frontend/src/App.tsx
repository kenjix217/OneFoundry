import { useEffect, useState, type FormEvent } from 'react';
import { ChiefWorkspace } from './ChiefWorkspace';
import { LegacyWorkspace } from './LegacyWorkspace';
import { TeamView } from './TeamView';
import { ConnectionsView } from './ConnectionsView';
import { WorkspaceNavigation, WorkspaceTabs } from './WorkspaceNavigation';
import { WorkspaceCustomize } from './WorkspaceCustomize';
import { WorkspaceIcon } from './WorkspaceIcon';
import { sectionForView, viewLabel, type View } from './workspaceNavigation';
import { readWorkspacePreferences, saveWorkspacePreferences, type WorkspacePreferences } from './workspacePreferences';

import {
  ApiError, changeProjectStatus, createProfile, createProject, getProject, listProfiles, listProjects, updateProfile,
  type NewProfile, type Profile, type ProfileFields, type Project,
} from './api';
import { ProfileEditor } from './ProfileEditor';
import { RunTimelineView } from './RunTimelineView';
import { ApprovalInbox } from './ApprovalInbox';
import { MemoryView } from './MemoryView';
import { InfraView } from './InfraView';
import { ModelsView } from './ModelsView';
import { SetupView } from './SetupView';
import { IntegrationsView } from './IntegrationsView';
import { ExtensionHub } from './ExtensionHub';
import { CodingBackendsView } from './CodingBackendsView';
import { ChannelsView } from './ChannelsView';
import { SkillLibrary } from './SkillLibrary';
import { AutomationView } from './AutomationView';
import { listProfileSkills, type ProfileSkill } from './skillsApi';
import { InputFilePicker } from './InputFilePicker';
import { OneFoundryOrganizationsView } from './OneFoundryOrganizationsView';
import { OneFoundryOperatorInboxView } from './OneFoundryOperatorInboxView';
import { getBusinessMode, type BusinessMode } from './onefoundryOrganizationApi';
import { listInputFiles, uploadInputFiles, type PendingInputFile } from './inputFileApi';
import { clearLocalKey, readLocalKey } from './brandStorage';
import { readWorkspaceRoute, workspaceHash, type WorkspaceRoute } from './workNavigation';

const LAST_PROJECT_KEY = 'onefoundry:last-project-id';
function rememberedProjectId(): string | null {
  try { return readLocalKey(LAST_PROJECT_KEY); } catch { return null; }
}
function rememberProjectId(id: string | null) {
  try { if (id) window.localStorage.setItem(LAST_PROJECT_KEY, id); else clearLocalKey(LAST_PROJECT_KEY); } catch { /* Storage may be disabled. */ }
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : 'An unexpected error occurred. Check the saved state before retrying.';
}

function aborted(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

function initials(name: string): string {
  return name.split(/\s+/).slice(0, 2).map((part) => part[0] ?? '').join('').toUpperCase();
}

export function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [archivedProjects, setArchivedProjects] = useState<Project[]>([]);
  const [trashedProjects, setTrashedProjects] = useState<Project[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [showTrash, setShowTrash] = useState(false);
  const [projectMenuId, setProjectMenuId] = useState<string | null>(null);
  const [projectAction, setProjectAction] = useState<{ project: Project; action: 'archive' | 'trash' } | null>(null);
  const [trashConfirmation, setTrashConfirmation] = useState('');
  const [projectActionBusy, setProjectActionBusy] = useState(false);
  const [projectActionError, setProjectActionError] = useState<string | null>(null);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profileSkills, setProfileSkills] = useState<Record<string, ProfileSkill[]>>({});
  const [profilesLoading, setProfilesLoading] = useState(false);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [reloadProjects, setReloadProjects] = useState(0);
  const [reloadWorkspace, setReloadWorkspace] = useState(0);
  const [showProjectForm, setShowProjectForm] = useState(false);
  const [projectSwitcherOpen, setProjectSwitcherOpen] = useState(false);
  const [projectName, setProjectName] = useState('');
  const [projectSaving, setProjectSaving] = useState(false);
  const [projectFormError, setProjectFormError] = useState<string | null>(null);
  const [projectFiles, setProjectFiles] = useState<PendingInputFile[]>([]);
  const [createdProjectId, setCreatedProjectId] = useState<string | null>(null);
  const [editor, setEditor] = useState<Profile | 'create' | null>(null);
  const [profileSaving, setProfileSaving] = useState(false);
  const [profileFormError, setProfileFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [activeView, setCurrentView] = useState<View>('home');
  const [preferences, setPreferences] = useState(readWorkspacePreferences);
  const [customizing, setCustomizing] = useState(false);
  const [, setCreateWorkOnOpen] = useState(false);
  const [preferenceNotice, setPreferenceNotice] = useState<string | null>(null);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [openTaskOrderId, setOpenTaskOrderId] = useState<string | null>(null);
  const [activityOrderId, setActivityOrderId] = useState<string | null>(null);
  const [manageTaskOrderId, setManageTaskOrderId] = useState<string | null>(null);
  const [businessMode, setBusinessMode] = useState<BusinessMode>('off');
  const [focusOrganizationId, setFocusOrganizationId] = useState<string | null>(null);

  function showWorkspaceRoute(route: WorkspaceRoute) {
    setSelectedId(route.projectId);
    rememberProjectId(route.projectId);
    setCurrentView(route.view);
    setMobileMenuOpen(false);
    if (route.view === 'chat') setOpenTaskOrderId(route.orderId);
    if (route.view === 'work') setManageTaskOrderId(route.orderId);
    if (route.view === 'activity') {
      setActivityOrderId(route.orderId);
      setOpenRunId(route.runId);
    }
    setNotice(null);
  }

  function navigateWorkspace(view: WorkspaceRoute['view'], options: { orderId?: string | null; runId?: string | null } = {}) {
    if (!selectedId) return;
    setCreateWorkOnOpen(false);
    const route = { projectId: selectedId, view, orderId: options.orderId !== undefined ? options.orderId : view === 'chat' ? openTaskOrderId : null, runId: options.runId ?? null };
    window.history.pushState(null, '', workspaceHash(route));
    showWorkspaceRoute(route);
  }

  function setActiveView(view: View) {
    if (selectedId) navigateWorkspace(view); else setCurrentView(view);
  }
  function changePreferences(next: WorkspacePreferences) {
    setPreferences(next);
    setPreferenceNotice(saveWorkspacePreferences(next) ? null : 'This layout is active for this visit. Browser storage is unavailable.');
  }

  useEffect(() => {
    const route = readWorkspaceRoute(window.location.hash);
    if (route) showWorkspaceRoute(route);
    const onHistory = () => { const next = readWorkspaceRoute(window.location.hash); if (next) showWorkspaceRoute(next); else setCurrentView('home'); };
    window.addEventListener('popstate', onHistory);
    window.addEventListener('hashchange', onHistory);
    return () => { window.removeEventListener('popstate', onHistory); window.removeEventListener('hashchange', onHistory); };
  }, []);

  useEffect(() => { setMobileMenuOpen(false); }, [activeView, selectedId]);

  useEffect(() => {
    let active = true;
    void getBusinessMode().then((mode) => { if (active) setBusinessMode(mode); })
      .catch(() => { if (active) setBusinessMode('off'); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setProjectsLoading(true);
    setProjectsError(null);
    void listProjects(controller.signal).then((items) => {
      setProjects(items);
      setSelectedId((current) => {
        const preferred = current ?? readWorkspaceRoute(window.location.hash)?.projectId ?? rememberedProjectId();
        const next = preferred && items.some((item) => item.id === preferred) ? preferred : items[0]?.id ?? null;
        rememberProjectId(next);
        return next;
      });
      setProjectsLoading(false);
    }).catch((error: unknown) => {
      if (aborted(error)) return;
      setProjectsError(message(error));
      setProjectsLoading(false);
    });
    return () => controller.abort();
  }, [reloadProjects]);

  useEffect(() => {
    if (projectsLoading || projectsError) return;
    const route = readWorkspaceRoute(window.location.hash);
    if (!route || projects.some((item) => item.id === route.projectId)) return;
    const fallback = projects.find((item) => item.id === selectedId) ?? projects[0];
    setSelectedId(fallback?.id ?? null); rememberProjectId(fallback?.id ?? null);
    setCurrentView('home'); setOpenTaskOrderId(null); setOpenRunId(null); setActivityOrderId(null); setManageTaskOrderId(null);
    window.history.replaceState(null, '', fallback ? workspaceHash({ projectId: fallback.id, view: 'home', orderId: null, runId: null }) : window.location.pathname);
    setNotice('The linked project is not active. Choose a project, or restore it from Archived or Trash.');
  }, [projectsLoading, projectsError, projects, selectedId]);

  useEffect(() => {
    if (!showArchived && !showTrash) return;
    const controller = new AbortController();
    if (showArchived) void listProjects(controller.signal, 'archived').then(setArchivedProjects).catch((error: unknown) => {
      if (!aborted(error)) setProjectsError(message(error));
    });
    if (showTrash) void listProjects(controller.signal, 'trashed').then(setTrashedProjects).catch((error: unknown) => {
      if (!aborted(error)) setProjectsError(message(error));
    });
    return () => controller.abort();
  }, [showArchived, showTrash, reloadProjects]);

  async function changeStatus(item: Project, action: 'archive' | 'unarchive' | 'trash' | 'restore', confirmName?: string) {
    setProjectActionBusy(true);
    setProjectActionError(null);
    try {
      const changed = await changeProjectStatus(item, action, confirmName);
      const saved = await getProject(item.id);
      if (saved.status !== changed.status || saved.version !== changed.version || saved.version !== item.version + 1) {
        throw new ApiError('Project state could not be confirmed. Refresh before retrying.', null);
      }
      if (selectedId === item.id && saved.status !== 'active') {
        setSelectedId(null);
        setFocusOrganizationId(null);
        rememberProjectId(null);
        setEditor(null);
        setOpenRunId(null);
        setCurrentView('home');
        window.history.replaceState(null, '', window.location.pathname);
      }
      setProjectAction(null);
      setProjectMenuId(null);
      setTrashConfirmation('');
      setNotice(action === 'archive' ? 'Project archived.' : action === 'trash' ? 'Project moved to Trash.' : 'Project restored.');
      setReloadProjects((value) => value + 1);
    } catch (error) {
      setProjectActionError(message(error));
    } finally {
      setProjectActionBusy(false);
    }
  }

  useEffect(() => {
    if (!selectedId) {
      setProject(null);
      setProfiles([]);
      setProfileSkills({});
      setWorkspaceError(null);
      return;
    }
    const controller = new AbortController();
    setProfilesLoading(true);
    setWorkspaceError(null);
    setProject(null);
    setProfiles([]);
    setProfileSkills({});
    void Promise.all([getProject(selectedId, controller.signal), listProfiles(selectedId, controller.signal)])
      .then(([item, members]) => {
        setProject(item);
        setProfiles(members);
        setProfilesLoading(false);
        void Promise.all(members.map(async (member) => [member.id, await listProfileSkills(selectedId, member.id, controller.signal)] as const))
          .then((entries) => { if (!controller.signal.aborted) setProfileSkills(Object.fromEntries(entries)); })
          .catch(() => { if (!controller.signal.aborted) setProfileSkills({}); });
      }).catch((error: unknown) => {
        if (aborted(error)) return;
        setWorkspaceError(message(error));
        setProfilesLoading(false);
      });
    return () => controller.abort();
  }, [selectedId, reloadWorkspace]);

  async function submitProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = projectName.trim();
    if (!name) return;
    setProjectSaving(true);
    setProjectFormError(null);
    setNotice(null);
    let savedProjectId = createdProjectId;
    try {
      const created = createdProjectId ? await getProject(createdProjectId) : await createProject(name);
      savedProjectId = created.id;
      setCreatedProjectId(created.id);
      const items = await listProjects();
      if (!items.some((item) => item.id === created.id)) {
        throw new ApiError('Project creation was returned, but list readback did not find it. Check the saved state.', null);
      }
      setProjects(items);
      setSelectedId(created.id);
      setFocusOrganizationId(null);
      rememberProjectId(created.id);
      if (projectFiles.length) {
        await uploadInputFiles(created.id, projectFiles, 'project');
        const linked = await listInputFiles(created.id);
        if (!projectFiles.every((item) => linked.some((file) => file.id === item.id))) {
          throw new ApiError('One or more files were not linked to the saved project.', null);
        }
      }
      const route: WorkspaceRoute = { projectId: created.id, view: 'setup', orderId: null, runId: null };
      window.history.pushState(null, '', workspaceHash(route));
      showWorkspaceRoute(route);
      setOpenTaskOrderId(null); setOpenRunId(null); setActivityOrderId(null); setManageTaskOrderId(null);
      setShowProjectForm(false);
      setProjectSwitcherOpen(false);
      setProjectName('');
      setProjectFiles([]);
      setCreatedProjectId(null);
      setNotice(projectFiles.length ? 'Project and attached files saved. Files are processing.' : 'Project saved.');
    } catch (error) {
      setProjectFormError(savedProjectId ? `Project saved. Attachment upload needs retry: ${message(error)}` : message(error));
    } finally {
      setProjectSaving(false);
    }
  }

  async function saveProfile(fields: NewProfile | ProfileFields) {
    if (!selectedId || !editor) return;
    setProfileSaving(true);
    setProfileFormError(null);
    setNotice(null);
    try {
      let saved: Profile;
      if (editor === 'create') {
        saved = await createProfile(selectedId, fields as NewProfile);
      } else {
        saved = await updateProfile(selectedId, editor.id, fields);
      }
      const readback = await listProfiles(selectedId);
      setProfiles(readback);
      if ('reports_to_profile_id' in fields &&
          readback.find((item) => item.id === saved.id)?.reports_to_profile_id !== fields.reports_to_profile_id) {
        throw new ApiError('The reporting line was not confirmed by readback. Check the saved profile before retrying.', null);
      }
      if (editor === 'create') setProfileSkills((current) => Object.fromEntries(readback.map((item) => [item.id, current[item.id] ?? []])));
      setEditor(null);
      setNotice('Profile saved. Its capabilities are checked when work starts.');
    } catch (error) {
      setProfileFormError(message(error));
    } finally {
      setProfileSaving(false);
    }
  }


  const commandMode = !!project && !showProjectForm && ['home', 'work', 'chat'].includes(activeView);
  const layoutMode = !!project && !showProjectForm;
  const configuration = <>
        <header className="topbar">
          <button className="mobile-nav-toggle" type="button" aria-label={mobileMenuOpen ? 'Close menu' : 'Open menu'}
            hidden={layoutMode} aria-expanded={mobileMenuOpen} onClick={() => setMobileMenuOpen((value) => !value)}>
            <span aria-hidden="true">☰</span>
          </button>
          <div className="breadcrumb"><button type="button" onClick={() => setActiveView('home')}>{project?.name ?? 'OneFoundry'}</button><span aria-hidden="true">/</span><strong>{sectionForView(activeView).label}</strong>{sectionForView(activeView).label !== 'Home' && <><span aria-hidden="true">/</span><span>{viewLabel(activeView)}</span></>}</div>
          <div className="topbar-state"><span className="workspace-local">Local workspace</span><button type="button" className="button button-outline" aria-label="Customize" onClick={() => setCustomizing(true)}><WorkspaceIcon name="settings" /><span>Customize</span></button></div>
        </header>
        {project && !commandMode && <WorkspaceTabs active={activeView} businessMode={businessMode} onNavigate={setActiveView} />}
        <div className="content-wrap">
          {project && !profilesLoading && !workspaceError && activeView === 'legacyWork' && <LegacyWorkspace project={project} profiles={profiles} onNavigate={setActiveView}
            onChief={(orderId) => navigateWorkspace('chat', { orderId })} onRun={(runId) => navigateWorkspace('activity', { runId })} />}
          {project && !profilesLoading && !workspaceError && activeView === 'connections' && <ConnectionsView key={project.id} projectId={project.id} />}
          {!selectedId && !projectsLoading && !projectsError && (
            <section className="welcome-panel">
              <span className="welcome-symbol" aria-hidden="true">✳</span>
              <p className="eyebrow">Your workspace starts here</p>
              <h1>Create your first project.</h1>
              <p>Start with a project name. OneFoundry adds a Chief of Staff and five specialist profiles for assignments. Setup guides your brief and first work order; you can plan and record work manually without connecting AI or a repository.</p>
              <button className="button button-primary" type="button" onClick={() => setShowProjectForm(true)}>Create project <span aria-hidden="true">↗</span></button>
            </section>
          )}
          {selectedId && profilesLoading && <section className="loading-panel" role="status"><span className="loading-orb" />Loading project and profiles…</section>}
          {selectedId && workspaceError && !profilesLoading && (
            <section className="empty-panel"><p className="eyebrow">Workspace unavailable</p><h1>Could not load this project.</h1>
              <p role="alert">{workspaceError}</p><button className="button button-primary" type="button" onClick={() => setReloadWorkspace((value) => value + 1)}>Try again</button></section>
          )}
          {project && !profilesLoading && !workspaceError && activeView === 'setup' &&
            <SetupView key={project.id} project={project} onBriefSaved={(confirmed) => {
              setProject(confirmed);
              setProjects((items) => items.map((item) => item.id === confirmed.id ? confirmed : item));
            }} onNavigate={(view, runId) => navigateWorkspace(view, { runId })} />}
          {project && !profilesLoading && !workspaceError && activeView === 'team' && <TeamView profiles={profiles} skills={profileSkills} onEdit={(item) => { setEditor(item); setProfileFormError(null); }} onAdd={() => { setEditor('create'); setProfileFormError(null); }} />}
          {project && !profilesLoading && !workspaceError && activeView === 'organizations' && businessMode !== 'off' &&
            <OneFoundryOrganizationsView key={project.id} projectId={project.id} mode={businessMode} profiles={profiles} focusOrganizationId={focusOrganizationId} />}
          {project && !profilesLoading && !workspaceError && activeView === 'onefoundryInbox' && businessMode === 'inbox' &&
            <OneFoundryOperatorInboxView key={project.id} projectId={project.id}
              onOpenOrganization={(id) => { setFocusOrganizationId(id); setActiveView('organizations'); }}
              onNavigate={setActiveView} />}
          {project && !profilesLoading && !workspaceError && activeView === 'models' && <ModelsView projectId={project.id} />}
          {project && !profilesLoading && !workspaceError && activeView === 'integrations' && <IntegrationsView key={project.id} projectId={project.id} onOpenCoding={() => setActiveView('coding')} />}
          {project && !profilesLoading && !workspaceError && activeView === 'extensionHub' && <ExtensionHub key={project.id} projectId={project.id} onNavigate={setActiveView} />}
          {project && !profilesLoading && !workspaceError && activeView === 'coding' && <CodingBackendsView key={project.id} projectId={project.id} profiles={profiles}
            onNavigate={(view, runId) => navigateWorkspace(view, { runId })} />}
          {project && !profilesLoading && !workspaceError && activeView === 'channels' &&
            <ChannelsView key={project.id} projectId={project.id} profiles={profiles} />}
          {project && !profilesLoading && !workspaceError && activeView === 'skills' && <SkillLibrary projectId={project.id} />}
          {project && !profilesLoading && !workspaceError && activeView === 'automation' &&
            <AutomationView key={project.id} projectId={project.id} profiles={profiles} onOpenConnections={() => setActiveView('connections')} />}
          {project && !profilesLoading && !workspaceError && activeView === 'activity' &&
            <RunTimelineView projectId={project.id} profiles={profiles} initialRunId={openRunId}
              initialOrderId={activityOrderId} onOpenTask={(orderId) => navigateWorkspace('chat', { orderId })} />}
          {project && !profilesLoading && !workspaceError && activeView === 'approvals' &&
            <ApprovalInbox projectId={project.id} onOpenRun={(runId) => navigateWorkspace('activity', { runId })} />}
          {project && !profilesLoading && !workspaceError && activeView === 'memory' &&
            <MemoryView projectId={project.id} projects={[...projects, ...archivedProjects, ...trashedProjects]} />}
          {project && !profilesLoading && !workspaceError && activeView === 'infra' && <InfraView />}
        </div>
  </>;
  return (
    <div data-density={preferences.density} data-sidebar={preferences.sidebar} data-panel-size={preferences.panelSize} className={`app-shell ${layoutMode ? 'chief-shell' : ''} ${mobileMenuOpen ? 'mobile-menu-open' : ''}`}>
      <aside className="sidebar" hidden={layoutMode && !mobileMenuOpen}>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true"><span /><span /><span /><span /></span>
          <span><strong>OneFoundry</strong><small>Local workspace</small></span>
        </div>
        <details className="project-switcher" open={showProjectForm || !selectedId || projectSwitcherOpen}><summary aria-label="Switch project" onClick={(event) => { event.preventDefault(); if (selectedId && !showProjectForm) setProjectSwitcherOpen((open) => !open); }}><span className="project-monogram" aria-hidden="true">{initials(project?.name ?? 'Projects')}</span><span className="project-switcher-label">{project?.name ?? 'Choose a project'}</span><span aria-hidden="true">⌄</span></summary><div className="sidebar-section-heading">
          <span>Projects</span>
          <button className="sidebar-add" type="button" aria-label="Create project" onClick={() => {
            setShowProjectForm((value) => !value);
            setProjectFormError(null);
          }}>+</button>
        </div>
        {showProjectForm && (
          <form className="project-form" onSubmit={(event) => { void submitProject(event); }}>
            <label htmlFor="project-name">New project</label>
            <input id="project-name" autoFocus required maxLength={120} value={projectName}
              disabled={createdProjectId !== null} onChange={(event) => setProjectName(event.target.value)} placeholder="Project name" />
            <InputFilePicker files={projectFiles} onChange={setProjectFiles} limit={30}
              disabled={projectSaving || createdProjectId !== null} label="Project files" />
            {createdProjectId && <p role="status">Project created. Retry the selected files, or open Setup to add files later.</p>}
            {projectFormError && <p className="sidebar-error" role="alert">{projectFormError}</p>}
            <div className="project-form-actions">
              <button type="button" onClick={() => { setShowProjectForm(false); setCreatedProjectId(null);
                setProjectFiles([]); setProjectName(''); setProjectFormError(null); }} disabled={projectSaving}>Cancel</button>
              <button type="submit" disabled={projectSaving}>{projectSaving ? 'Saving…' : createdProjectId ? 'Retry files' : 'Create'}</button>
            </div>
          </form>
        )}
        <nav aria-label="Projects" className="project-nav">
          {projectsLoading && <p className="sidebar-hint">Loading projects…</p>}
          {projectsError && (
            <div className="sidebar-feedback"><p role="alert">{projectsError}</p>
              <button type="button" onClick={() => setReloadProjects((value) => value + 1)}>Try again</button></div>
          )}
          {!projectsLoading && !projectsError && projects.length === 0 &&
            <p className="sidebar-hint">No projects yet. Create one to set up your team.</p>}
          {!projectsLoading && projects.map((item) => (
            <div className="project-nav-entry" key={item.id}>
            <div className="project-row"><button type="button" className={`project-link ${selectedId === item.id ? 'active' : ''}`}
              aria-current={selectedId === item.id ? 'page' : undefined} onClick={() => {
                setProjectSwitcherOpen(false);
                setSelectedId(item.id);
                setFocusOrganizationId(null);
                rememberProjectId(item.id);
                setEditor(null);
                setNotice(null);
                const route: WorkspaceRoute = { projectId: item.id, view: 'home', orderId: null, runId: null };
                window.history.pushState(null, '', workspaceHash(route));
                showWorkspaceRoute(route);
                setOpenRunId(null);
                setOpenTaskOrderId(null);
              }}>
              <span className="project-monogram" aria-hidden="true">{initials(item.name)}</span>
              <span className="project-link-name">{item.name}</span>
              <span className="project-link-arrow" aria-hidden="true">›</span>
            </button><button type="button" className="project-menu-toggle" aria-label={`Project menu for ${item.name}`}
              aria-expanded={projectMenuId === item.id} onClick={() => { setProjectMenuId(projectMenuId === item.id ? null : item.id); setProjectActionError(null); }}>···</button></div>
            {projectMenuId === item.id && <div className="project-menu">
              <button type="button" onClick={() => { setProjectAction({ project: item, action: 'archive' }); setProjectActionError(null); }}>Archive</button>
              <button type="button" onClick={() => { setProjectAction({ project: item, action: 'trash' }); setTrashConfirmation(''); setProjectActionError(null); }}>Delete to Trash</button>
            </div>}
            </div>
          ))}
          <button type="button" className="project-section-toggle" aria-expanded={showArchived} onClick={() => setShowArchived(!showArchived)}>Archived <span>{showArchived ? '⌄' : '›'}</span></button>
          {showArchived && (archivedProjects.length ? archivedProjects.map((item) =>
            <div className="project-stored-row" key={item.id}><span title={item.name}>{item.name}</span>
              <button type="button" disabled={projectActionBusy} onClick={() => { void changeStatus(item, 'unarchive'); }}>Restore</button>
              <button type="button" disabled={projectActionBusy} onClick={() => { setProjectAction({ project: item, action: 'trash' }); setTrashConfirmation(''); }}>Delete</button></div>
          ) : <p className="sidebar-hint">No archived projects.</p>)}
          <button type="button" className="project-section-toggle" aria-expanded={showTrash} onClick={() => setShowTrash(!showTrash)}>Trash <span>{showTrash ? '⌄' : '›'}</span></button>
          {showTrash && (trashedProjects.length ? trashedProjects.map((item) =>
            <div className="project-stored-row" key={item.id}><span title={item.name}>{item.name}</span>
              <button type="button" disabled={projectActionBusy} onClick={() => { void changeStatus(item, 'restore'); }}>Restore</button></div>
          ) : <p className="sidebar-hint">Trash is empty.</p>)}
          {projectActionError && !projectAction && <p className="sidebar-error" role="alert">{projectActionError}</p>}
        </nav>
        </details>
        {selectedId && <WorkspaceNavigation active={activeView} businessMode={businessMode} onNavigate={setActiveView} />}
        <div className="sidebar-personalize"><button type="button" aria-label="Customize workspace" title="Customize workspace" onClick={() => setCustomizing(true)}><WorkspaceIcon name="settings" /><span>Customize workspace</span></button></div>
        <div className="sidebar-footer"><span className="local-dot" />On this device</div>
      </aside>

      <button className="mobile-nav-backdrop" type="button" aria-label="Close menu"
        onClick={() => setMobileMenuOpen(false)} />

      <main className={`main-content view-${activeView}`}>
        {preferenceNotice && <p className="notice" role="status">{preferenceNotice}</p>}
        {notice && <div className="notice" role="status"><span aria-hidden="true">✓</span>{notice}<button type="button" aria-label="Dismiss notice" onClick={() => setNotice(null)}>×</button></div>}
        {layoutMode && !profilesLoading && !workspaceError && project ? <div className="chief-host" aria-hidden={mobileMenuOpen || undefined} inert={mobileMenuOpen}><ChiefWorkspace key={project.id} project={project} profiles={profiles}
          initialOrderId={activeView === 'home' ? null : activeView === 'work' ? manageTaskOrderId : openTaskOrderId}
          onSelect={(orderId) => navigateWorkspace('chat', { orderId })} onMenu={() => setMobileMenuOpen(true)}
          onNavigate={setActiveView} settingsContent={commandMode ? undefined : configuration} integrationsOpen={activeView === 'extensionHub'} /></div> : configuration}
      </main>
      {customizing && <WorkspaceCustomize value={preferences} onChange={changePreferences} onClose={() => setCustomizing(false)} />}
      {projectAction && <div className="project-action-backdrop" role="presentation">
        <section className="project-action-dialog" role="dialog" aria-modal="true" aria-labelledby="project-action-title">
          <p className="eyebrow">Project menu</p>
          <h2 id="project-action-title">{projectAction.action === 'archive' ? 'Archive project' : 'Move project to Trash'}</h2>
          <p><strong>{projectAction.project.name}</strong> {projectAction.action === 'archive'
            ? 'will leave your active list. Its work and history stay saved. You can restore it from Archived.'
            : 'will move to recoverable Trash. Its work, artifacts, keys, and history stay saved. Restore it from Trash to use it again.'}</p>
          {projectAction.action === 'trash' && <label className="project-trash-label">Type the project name to confirm
            <input autoFocus value={trashConfirmation} onChange={(event) => setTrashConfirmation(event.target.value)} placeholder={projectAction.project.name} />
          </label>}
          {projectActionError && <p className="project-action-error" role="alert">{projectActionError}</p>}
          <div className="project-action-buttons">
            <button className="button button-quiet" type="button" disabled={projectActionBusy} onClick={() => { setProjectAction(null); setProjectActionError(null); }}>Cancel</button>
            <button className="button button-primary" type="button" disabled={projectActionBusy || (projectAction.action === 'trash' && trashConfirmation !== projectAction.project.name)}
              onClick={() => { void changeStatus(projectAction.project, projectAction.action, trashConfirmation); }}>
              {projectActionBusy ? 'Saving…' : projectAction.action === 'archive' ? 'Archive project' : 'Move to Trash'}</button>
          </div>
        </section>
      </div>}
      {editor && selectedId && <ProfileEditor key={editor === 'create' ? 'create' : editor.id} projectId={selectedId} profile={editor === 'create' ? null : editor} profiles={profiles}
        saving={profileSaving} error={profileFormError} onClose={() => setEditor(null)} onSave={saveProfile}
        onSkillsChanged={() => { if (editor !== 'create') void listProfileSkills(selectedId, editor.id).then((items) => setProfileSkills((current) => ({ ...current, [editor.id]: items }))); }} />}
    </div>
  );
}
