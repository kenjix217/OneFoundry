import { isView, type View } from './workspaceNavigation';

export type WorkspaceRoute = {
  projectId: string;
  view: View;
  orderId: string | null;
  runId: string | null;
};

export function readWorkspaceRoute(hash: string): WorkspaceRoute | null {
  const [path, query = ''] = hash.replace(/^#/, '').split('?');
  const parts = path.split('/').filter(Boolean);
  if (parts.length !== 3 || parts[0] !== 'project' || !parts[1]) return null;
  const view = parts[2] === 'work-orders' ? 'work' : parts[2] === 'task' ? 'chat' : parts[2] === 'tools' ? 'integrations' : isView(parts[2]) ? parts[2] : null;
  if (!view) return null;
  try {
    const params = new URLSearchParams(query);
    return { projectId: decodeURIComponent(parts[1]), view,
      orderId: params.get('order'), runId: view === 'activity' ? params.get('run') : null };
  } catch { return null; }
}

export function workspaceHash(route: WorkspaceRoute): string {
  const section = route.view === 'work' ? 'work-orders' : route.view === 'chat' ? 'task' : route.view;
  const params = new URLSearchParams();
  if (route.orderId) params.set('order', route.orderId);
  if (route.view === 'activity' && route.runId) params.set('run', route.runId);
  const query = params.toString();
  return `#project/${encodeURIComponent(route.projectId)}/${section}${query ? `?${query}` : ''}`;
}
