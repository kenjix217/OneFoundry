import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';

import { ApiError } from './api';
import { cancelExtensionInvocation, createExtensionConnection, createExtensionDraft, createExtensionPackage, listExtensionCatalog,
  createExtensionWebhook, listExtensionWebhooks, rotateExtensionWebhookSecret, updateExtensionWebhook,
  exportExtensionPackage, importExtensionPackage,
  executeExtensionInvocation, getExtensionReadiness, getExtensionWorkerStatus, listExtensionConnections, listExtensionDrafts,
  listExtensionInvocations, listExtensionPackages, listPackageBindings, probeExtensionConnection, putPackageBinding,
  proposeDraftDecision, putExtensionCredential,
  proposePackageState, updateExtensionConnection, type ExtensionCatalog, type ExtensionConnection,
  type CompatibilityReport, type ExtensionDraft, type ExtensionInvocation, type ExtensionPackage, type ExtensionTool, type PackageBinding, type PackageFormat, type ExtensionWebhook, type ExtensionWorkerStatus } from './extensionHubApi';
import './extensionHub.css';

type Tab = 'connections' | 'catalog' | 'foundry';
type Snapshot = { catalog: ExtensionCatalog; packages: ExtensionPackage[]; connections: ExtensionConnection[]; drafts: ExtensionDraft[]; invocations: ExtensionInvocation[]; webhooks: ExtensionWebhook[]; worker: ExtensionWorkerStatus };
type Template = { id: string; label: string; kind: string; operations: string[]; operationIds: string[] };
const starterManifest = JSON.stringify({ schema_version: 1, id: 'my-extension', name: 'My Extension', version: '0.1.0', description: 'A focused OneFoundry extension',
  connections: [{ id: 'service', kind: 'http', name: 'Service API', operations: [{ id: 'lookup', method: 'GET', path: '/records/{id}' }] }], tools: [], skills: [] }, null, 2);
type PackagePreset = 'plugin' | 'python' | 'api' | 'mcp' | 'webhook';
function packagePreset(kind: PackagePreset): { manifest: Record<string, unknown>; files: Record<string, string> } {
  const manifest: Record<string, unknown> = { schema_version: 1, id: `my-${kind}`, name: `My ${kind.toUpperCase()} extension`, version: '0.1.0', description: '', connections: [], tools: [], skills: [] };
  const schema = { type: 'object', properties: {} };
  if (kind === 'python') {
    manifest.tools = [{ id: 'run', description: 'Return a supplied text value',
      parameters: { type: 'object', properties: { text: { type: 'string', maxLength: 2000 } }, required: ['text'], additionalProperties: false },
      returns: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'], additionalProperties: false },
      effect: 'pure', runtime: 'python', entrypoint: 'my_tool:run' }];
    return { manifest, files: { 'src/my_tool.py': 'async def run(context, text):\n    return {"text": text}\n' } };
  }
  if (kind === 'api' || kind === 'mcp') {
    manifest.connections = [{ id: 'service', name: 'Service connection', kind: kind === 'api' ? 'http' : 'mcp', operations: kind === 'api' ? [{ id: 'lookup', method: 'GET', path: '/records/{id}' }] : [{ id: 'lookup' }] }];
    manifest.tools = [{ id: 'lookup', description: 'Look up a record', parameters: schema, returns: schema, effect: 'read', runtime: kind === 'api' ? 'http' : 'mcp', connection: 'service', operation: 'lookup' }];
  }
  if (kind === 'webhook') {
    manifest.connections = [{ id: 'events', name: 'Outbound webhook endpoint', kind: 'webhook', operations: [{ id: 'send', method: 'POST', path: '/events' }] }];
    manifest.tools = [{ id: 'send', description: 'Send an event to the configured endpoint', parameters: schema, returns: schema,
      effect: 'write', runtime: 'http', connection: 'events', operation: 'send' }];
  }
  return { manifest, files: {} };
}

function templates(packages: ExtensionPackage[]): { packageItem: ExtensionPackage; template: Template }[] {
  return packages.flatMap((packageItem) => {
    const raw = packageItem.manifest.connections;
    if (!Array.isArray(raw)) return [];
    return raw.flatMap((value) => {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
      const item = value as Record<string, unknown>;
      return typeof item.id === 'string' ? [{ packageItem, template: {
        id: item.id, label: typeof item.name === 'string' ? item.name : item.id,
        kind: typeof item.kind === 'string' ? item.kind : 'connection',
        operations: Array.isArray(item.operations) ? item.operations.flatMap((operation) => {
          if (!operation || typeof operation !== 'object' || Array.isArray(operation)) return [];
          const entry = operation as Record<string, unknown>;
          return typeof entry.id === 'string' ? [`${typeof entry.method === 'string' ? `${entry.method} ` : ''}${entry.id}${typeof entry.path === 'string' ? ` · ${entry.path}` : ''}`] : [];
        }) : [],
        operationIds: Array.isArray(item.operations) ? item.operations.flatMap((operation) => {
          if (!operation || typeof operation !== 'object' || Array.isArray(operation)) return [];
          const entry = operation as Record<string, unknown>;
          return typeof entry.id === 'string' ? [entry.id] : [];
        }) : [],
      } }] : [];
    });
  });
}
function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Could not confirm the result. Refresh saved state before retrying.';
}
function uncertain(error: unknown): boolean { return !(error instanceof ApiError) || error.status === null || error.status >= 500; }
function readableName(id: string): string {
  const leaf = id.split(/[.:/]/).filter(Boolean).at(-1) ?? id;
  return leaf.replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}
function packageDisplayName(packageItem: ExtensionPackage): string {
  return typeof packageItem.manifest.name === 'string' && packageItem.manifest.name.trim()
    ? packageItem.manifest.name.trim() : readableName(packageItem.package_key);
}
function toolRecordLabel(tool: ExtensionTool, packages: ExtensionPackage[]): string {
  if (tool.source !== 'extension' || !tool.declared_id) return readableName(tool.id);
  const packageItem = packages.find((item) => item.id === tool.package_id);
  const packageName = packageItem ? packageDisplayName(packageItem) : '';
  return `${packageName ? `${packageName} · ` : ''}${readableName(tool.declared_id)}`;
}
function toolLabel(id: string, snapshot: Snapshot): string {
  const tool = snapshot.catalog.tools.find((item) => item.id === id && item.status === 'active')
    ?? snapshot.catalog.tools.find((item) => item.id === id);
  return tool ? toolRecordLabel(tool, snapshot.packages) : readableName(id);
}
async function read(projectId: string, signal?: AbortSignal): Promise<Snapshot> {
  const [catalog, packages, connections, drafts, invocations, webhooks, worker] = await Promise.all([
    listExtensionCatalog(projectId, signal), listExtensionPackages(projectId, signal), listExtensionConnections(projectId, signal),
    listExtensionDrafts(projectId, signal), listExtensionInvocations(projectId, signal), listExtensionWebhooks(projectId, signal), getExtensionWorkerStatus(projectId, signal),
  ]);
  return { catalog, packages, connections, drafts, invocations, webhooks, worker };
}

export function ExtensionHub({ projectId, onNavigate }: { projectId: string; onNavigate: (view: 'connections' | 'skills' | 'work' | 'approvals' | 'team') => void }) {
  const activeProject = useRef(projectId);
  activeProject.current = projectId;
  const [tab, setTab] = useState<Tab>('catalog');
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [blocked, setBlocked] = useState(false);
  const [selection, setSelection] = useState<string | null>(null);
  const [mobileDetail, setMobileDetail] = useState(false);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<'all' | 'tools' | 'skills' | 'packages'>('all');
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [editingConnection, setEditingConnection] = useState<string | null>(null);
  const [templateChoice, setTemplateChoice] = useState('');
  const [connectionLabel, setConnectionLabel] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [authMethod, setAuthMethod] = useState<'none' | 'bearer' | 'api_key'>('none');
  const [headerName, setHeaderName] = useState('');
  const [readiness, setReadiness] = useState<Record<string, { version: number; credential_present: boolean; configured: boolean; enabled: boolean; live_test_status: string; successful_use: boolean }>>({});
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [bindings, setBindings] = useState<Record<string, PackageBinding[]>>({});
  const [bindingChoices, setBindingChoices] = useState<Record<string, string>>({});
  const [bindingError, setBindingError] = useState<string | null>(null);
  const credentialInput = useRef<HTMLInputElement>(null);
  const webhookSecretInput = useRef<HTMLInputElement>(null);
  const webhookRotationInput = useRef<HTMLInputElement>(null);
  const [webhookName, setWebhookName] = useState('');
  const [webhookSecretVisible, setWebhookSecretVisible] = useState(false);
  const [selectedWebhook, setSelectedWebhook] = useState<string | null>(null);
  const connectionDialog = useRef<HTMLElement>(null);
  const connectionReturnFocus = useRef<HTMLElement | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [authorKind, setAuthorKind] = useState<'skill' | 'package'>('skill');
  const [skillName, setSkillName] = useState('');
  const [skillDescription, setSkillDescription] = useState('');
  const [skillContent, setSkillContent] = useState('');
  const [requiredTools, setRequiredTools] = useState<string[]>([]);
  const [manifestText, setManifestText] = useState(starterManifest);
  const [filesText, setFilesText] = useState('{}');
  const [preset, setPreset] = useState<PackagePreset>('plugin');
  const [packageFormat, setPackageFormat] = useState<PackageFormat>('onefoundry');
  const [compatibility, setCompatibility] = useState<CompatibilityReport | null>(null);
  const [compatibilityPackageId, setCompatibilityPackageId] = useState<string | null>(null);
  const importFile = useRef<HTMLInputElement>(null);
  const [confirmAction, setConfirmAction] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setBusy(false); setError(null); setSnapshot(null); setBlocked(false); setSelection(null); setMobileDetail(false); setConnectionOpen(false); setReadiness({}); setBindings({}); setBindingChoices({});
    if (credentialInput.current) credentialInput.current.value = '';
    if (webhookSecretInput.current) webhookSecretInput.current.value = '';
    if (webhookRotationInput.current) webhookRotationInput.current.value = '';
    void read(projectId, controller.signal).then((value) => {
      if (!controller.signal.aborted) { setSnapshot(value); setLoading(false); }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) { setError(errorText(failure)); setLoading(false); }
    });
    return () => controller.abort();
  }, [projectId, refreshKey]);
  useEffect(() => {
    setSkillName(''); setSkillDescription(''); setSkillContent(''); setRequiredTools([]);
    setManifestText(starterManifest); setFilesText('{}'); setCompatibility(null); setCompatibilityPackageId(null);
    setSearch(''); setFilter('all'); setConfirmAction(null);
    setWebhookName(''); setSelectedWebhook(null); setWebhookSecretVisible(false);
    if (importFile.current) importFile.current.value = '';
  }, [projectId]);
  useEffect(() => {
    if (connectionOpen) connectionDialog.current?.querySelector<HTMLElement>('button')?.focus();
  }, [connectionOpen]);

  async function perform(action: () => Promise<string>) {
    setBusy(true); setError(null); setNotice(null);
    try { const result = await action(); if (activeProject.current === projectId) setNotice(result); }
    catch (failure) { if (activeProject.current === projectId) { setError(errorText(failure)); if (uncertain(failure)) setBlocked(true); } }
    finally { if (activeProject.current === projectId) setBusy(false); }
  }
  function refresh() { setRefreshKey((value) => value + 1); setNotice(null); setConnectionError(null); }
  const templateOptions = useMemo(() => templates(snapshot?.packages ?? []), [snapshot?.packages]);
  const selectedTemplate = templateOptions.find(({ packageItem, template }) => `${packageItem.id}:${template.id}` === templateChoice);
  const cards = useMemo(() => {
    if (!snapshot) return [];
    const all = [
      ...snapshot.catalog.tools.map((item) => ({ key: `tool:${item.source}:${item.package_id ?? ''}:${item.id}`, type: 'Tool', title: toolRecordLabel(item, snapshot.packages), subtitle: item.description, status: item.status, packageId: item.package_id })),
      ...snapshot.catalog.skills.map((item) => ({ key: `skill-package:${item.package_id}:${item.id}`, type: 'Skill', title: item.name, subtitle: `Requires ${item.required_tools.length} tool${item.required_tools.length === 1 ? '' : 's'}`, status: item.status, packageId: item.package_id })),
      ...snapshot.drafts.map((item) => ({ key: `skill:${item.id}`, type: 'Skill draft', title: item.name, subtitle: item.description, status: item.status, packageId: null })),
      ...snapshot.packages.map((item) => ({ key: `package:${item.id}`, type: 'Package', title: typeof item.manifest.name === 'string' ? item.manifest.name : readableName(item.package_key), subtitle: String(item.manifest.description ?? ''), status: item.state, packageId: item.id })),
    ];
    return all.filter((item) => (filter === 'all' || item.type.toLowerCase().startsWith(filter.slice(0, -1))) &&
      `${item.title} ${item.subtitle}`.toLowerCase().includes(search.toLowerCase()));
  }, [snapshot, filter, search]);
  const active = cards.find((item) => item.key === selection) ?? cards[0];
  const activePackage = active?.packageId ? snapshot?.packages.find((item) => item.id === active.packageId) : undefined;
  const packageSlots = activePackage ? templateOptions.filter(({ packageItem }) => packageItem.id === activePackage.id) : [];
  const activeDraft = active?.key.startsWith('skill:') ? snapshot?.drafts.find((item) => `skill:${item.id}` === active.key) : undefined;
  const activeSkill = active?.key.startsWith('skill-package:') ? snapshot?.catalog.skills.find((item) => `skill-package:${item.package_id}:${item.id}` === active.key) : undefined;
  const activeConnection = snapshot?.connections.find((item) => `connection:${item.id}` === selection) ?? snapshot?.connections[0];
  const activeReadiness = activeConnection && readiness[activeConnection.id];
  const activeWebhook = snapshot?.webhooks.find((item) => item.id === selectedWebhook) ?? snapshot?.webhooks[0];
  const webhookReceivePath = activeWebhook ? `${window.location.origin}/api/extensions/webhooks/${activeWebhook.id}` : '';
  const activePackageId = activePackage?.id;

  useEffect(() => {
    if (!activePackageId) return;
    const controller = new AbortController();
    setBindingError(null);
    void listPackageBindings(projectId, activePackageId, controller.signal).then((records) => {
      if (!controller.signal.aborted) setBindings((current) => ({ ...current, [activePackageId]: records }));
    }).catch((failure: unknown) => { if (!controller.signal.aborted) setBindingError(errorText(failure)); });
    return () => controller.abort();
  }, [projectId, activePackageId]);

  useEffect(() => {
    if (!activeConnection) return;
    const controller = new AbortController();
    setReadinessError(null);
    void getExtensionReadiness(projectId, activeConnection, controller.signal).then((value) => {
      if (!controller.signal.aborted) setReadiness((current) => ({ ...current, [activeConnection.id]: value }));
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setReadinessError(errorText(failure));
    });
    return () => controller.abort();
  }, [projectId, activeConnection]);

  function openConnection(option = '', item?: ExtensionConnection) {
    connectionReturnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setEditingConnection(item?.id ?? null);
    setTemplateChoice(item ? `${item.package_id}:${item.template_id}` : option);
    setConnectionLabel(item?.label ?? ''); setEndpoint(typeof item?.config.endpoint === 'string' ? item.config.endpoint : '');
    setAuthMethod(item?.config.auth_method === 'bearer' || item?.config.auth_method === 'api_key' ? item.config.auth_method : 'none');
    setHeaderName(typeof item?.config.header_name === 'string' ? item.config.header_name : '');
    setConnectionError(null); setConnectionOpen(true);
  }
  function closeConnection() { setConnectionOpen(false); setEditingConnection(null); setEndpoint(''); setHeaderName(''); setConnectionError(null); connectionReturnFocus.current?.focus(); }
  function connectionDialogKey(event: KeyboardEvent) {
    if (event.key === 'Escape') { event.preventDefault(); closeConnection(); return; }
    if (event.key !== 'Tab' || !connectionDialog.current) return;
    const controls = [...connectionDialog.current.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)')];
    if (!controls.length) return;
    if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus(); }
    else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0].focus(); }
  }
  function saveConnection(event: FormEvent) {
    event.preventDefault();
    if (!selectedTemplate) { setConnectionError('Choose a package connection template.'); return; }
    let destination: URL;
    try { destination = new URL(endpoint.trim()); } catch { setConnectionError('Enter an HTTPS endpoint for this service.'); return; }
    if (destination.protocol !== 'https:' || destination.username || destination.password || destination.hash) {
      setConnectionError('Enter an HTTPS endpoint without a password or fragment.'); return;
    }
    const config: Record<string, unknown> = { transport: 'streamable_http', endpoint: endpoint.trim(), auth_method: authMethod };
    if (authMethod === 'api_key') config.header_name = headerName.trim();
    if (authMethod === 'api_key' && !/^[A-Za-z][A-Za-z0-9-]{0,79}$/.test(headerName.trim())) {
      setConnectionError('Enter a valid HTTP header name for the API key.'); return;
    }
    setConnectionError(null);
    void perform(async () => {
      const existing = snapshot?.connections.find((item) => item.id === editingConnection);
      const created = existing ? await updateExtensionConnection(projectId, existing, { config }) :
        await createExtensionConnection(projectId, { package_id: selectedTemplate.packageItem.id, template_id: selectedTemplate.template.id,
          label: connectionLabel.trim(), config });
      const connections = await listExtensionConnections(projectId);
      if (!connections.some((item) => item.id === created.id && item.version === created.version)) throw new ApiError('Connection save was not confirmed in saved state.', null);
      if (activeProject.current !== projectId) return 'Project changed; refresh saved state.';
      setSnapshot((state) => state && { ...state, connections }); closeConnection(); setTab('connections'); setSelection(`connection:${created.id}`); setMobileDetail(true);
      return existing ? 'Connection settings saved and confirmed.' : 'Connection draft saved and confirmed. Review its configuration before enabling.';
    });
  }
  function saveSkill(event: FormEvent) {
    event.preventDefault();
    const activeTools = new Set(snapshot?.catalog.tools.filter((tool) => tool.status === 'active').map((tool) => tool.id) ?? []);
    if (requiredTools.some((id) => !activeTools.has(id))) { setError('A requested tool is no longer active. Refresh the catalog and choose available tools.'); return; }
    void perform(async () => {
      const created = await createExtensionDraft(projectId, { name: skillName.trim(), description: skillDescription.trim(), content: skillContent, required_tools: requiredTools });
      const drafts = await listExtensionDrafts(projectId);
      if (!drafts.some((item) => item.id === created.id && item.sha256 === created.sha256)) throw new ApiError('Skill draft was not confirmed in saved state.', null);
      if (activeProject.current !== projectId) return 'Project changed; refresh saved state.';
      setSnapshot((state) => state && { ...state, drafts }); setSkillName(''); setSkillDescription(''); setSkillContent(''); setRequiredTools([]);
      setTab('catalog'); setSelection(`skill:${created.id}`); setMobileDetail(true);
      return 'Skill proposal saved. It is not available to agents until reviewed.';
    });
  }
  function savePackage(event: FormEvent) {
    event.preventDefault();
    let manifest: Record<string, unknown>; let files: Record<string, string>;
    try {
      const parsed: unknown = JSON.parse(manifestText); if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error(); manifest = parsed as Record<string, unknown>;
      const parsedFiles: unknown = JSON.parse(filesText); if (!parsedFiles || Array.isArray(parsedFiles) || typeof parsedFiles !== 'object' ||
        Object.values(parsedFiles).some((value) => typeof value !== 'string')) throw new Error(); files = parsedFiles as Record<string, string>;
    } catch { setError('Manifest and files must be valid JSON objects; each file value must be text.'); return; }
    void perform(async () => {
      const created = await createExtensionPackage(projectId, manifest, files);
      const packages = await listExtensionPackages(projectId);
      if (!packages.some((item) => item.id === created.id && item.sha256 === created.sha256)) throw new ApiError('Package was not confirmed in saved state.', null);
      if (activeProject.current !== projectId) return 'Project changed; refresh saved state.';
      setSnapshot((state) => state && { ...state, packages }); setTab('catalog'); setSelection(`package:${created.id}`); setMobileDetail(true);
      return 'Package saved as an inert draft. Its tools have no role access.';
    });
  }
  function choosePackagePreset(kind: PackagePreset) {
    const next = packagePreset(kind); setPreset(kind);
    setManifestText(JSON.stringify(next.manifest, null, 2)); setFilesText(JSON.stringify(next.files, null, 2));
  }
  function importPackage(event: FormEvent) {
    event.preventDefault();
    const file = importFile.current?.files?.[0];
    if (!file) { setError('Choose a package ZIP archive.'); return; }
    if (file.size > 4 * 1024 * 1024) { setError('The package ZIP must be 4 MiB or smaller.'); return; }
    void perform(async () => {
      const archive = await new Promise<ArrayBuffer>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => reader.result instanceof ArrayBuffer ? resolve(reader.result) : reject(new Error('ZIP read failed'));
        reader.onerror = () => reject(reader.error ?? new Error('ZIP read failed'));
        reader.readAsArrayBuffer(file);
      });
      const bytes = new Uint8Array(archive);
      let binary = '';
      for (let index = 0; index < bytes.length; index += 32768) binary += String.fromCharCode(...bytes.subarray(index, index + 32768));
      const result = await importExtensionPackage(projectId, packageFormat, btoa(binary));
      const packages = await listExtensionPackages(projectId);
      if (!packages.some((item) => item.id === result.package.id && item.sha256 === result.package.sha256)) throw new ApiError('Imported package was not confirmed in saved state.', null);
      if (activeProject.current !== projectId) return 'Project changed; refresh saved state.';
      setSnapshot((state) => state && { ...state, packages }); setCompatibility(result.report); setCompatibilityPackageId(result.package.id);
      if (importFile.current) importFile.current.value = '';
      setTab('catalog'); setSelection(`package:${result.package.id}`); setMobileDetail(true);
      return 'Package imported as an inert draft. Review compatibility and request activation separately.';
    });
  }
  function downloadPackage(item: ExtensionPackage) {
    void perform(async () => {
      const blob = await exportExtensionPackage(projectId, item.id, packageFormat);
      const url = URL.createObjectURL(blob);
      try {
        const link = document.createElement('a'); link.href = url;
        link.download = `${item.package_key}-${item.package_version}-${packageFormat}.zip`;
        document.body.append(link); link.click(); link.remove();
      } finally { URL.revokeObjectURL(url); }
      return 'Package ZIP prepared for download.';
    });
  }
  function generateWebhookSecret(target: HTMLInputElement | null) {
    if (!target) return;
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    target.value = btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    target.focus(); target.select();
  }
  function validWebhookSecret(value: string): boolean { return /^[!-~]{32,128}$/.test(value); }
  function saveWebhook(event: FormEvent) {
    event.preventDefault();
    const value = webhookSecretInput.current?.value ?? '';
    if (!validWebhookSecret(value)) { setError('Signing secret must be 32–128 printable characters without spaces.'); return; }
    void perform(async () => {
      const created = await createExtensionWebhook(projectId, webhookName.trim(), value);
      const webhooks = await listExtensionWebhooks(projectId);
      if (!webhooks.some((item) => item.id === created.id && item.version === created.version && item.credential_present)) throw new ApiError('Webhook save was not confirmed in saved state.', null);
      if (activeProject.current !== projectId) return 'Project changed; refresh saved state.';
      setSnapshot((state) => state && { ...state, webhooks }); setSelectedWebhook(created.id); setWebhookName('');
      if (webhookSecretInput.current) webhookSecretInput.current.value = '';
      return 'Webhook saved disabled with a protected signing secret. Configure the sender before enabling.';
    });
  }
  function rotateWebhook(item: ExtensionWebhook, event: FormEvent) {
    event.preventDefault();
    const value = webhookRotationInput.current?.value ?? '';
    if (!validWebhookSecret(value)) { setError('Signing secret must be 32–128 printable characters without spaces.'); return; }
    void perform(async () => {
      const changed = await rotateExtensionWebhookSecret(projectId, item, value);
      const webhooks = await listExtensionWebhooks(projectId);
      if (!webhooks.some((current) => current.id === changed.id && current.version === changed.version && current.credential_present)) throw new ApiError('Secret rotation was not confirmed in saved state.', null);
      if (activeProject.current !== projectId) return 'Project changed; refresh saved state.';
      setSnapshot((state) => state && { ...state, webhooks });
      if (webhookRotationInput.current) webhookRotationInput.current.value = '';
      return 'Signing secret rotated and confirmed. Update the sender with the new value.';
    });
  }

  return <section className="extension-hub" aria-labelledby="extension-hub-title">
    <header className="extension-hub-head"><div><p className="eyebrow">Project capability workspace</p><h1 id="extension-hub-title">Integrations</h1>
      <p>Connect services, review available capabilities, and prepare tools or skills for this project.</p></div>
      <button className="button button-outline" type="button" onClick={refresh} disabled={busy}>Refresh saved state</button></header>
    <nav className="extension-hub-tabs" aria-label="Integration Hub views">{([['connections', 'Connections'], ['catalog', 'Skills & Tools'], ['foundry', 'Tool Foundry']] as const).map(([id, label]) =>
      <button key={id} type="button" aria-current={tab === id ? 'page' : undefined} onClick={() => { setTab(id); setMobileDetail(false); setError(null); }}>{label}</button>)}</nav>
    {snapshot && <p className="extension-hub-worker" role="status"><strong>Isolated tool worker: {snapshot.worker.available ? 'Ready' : 'Unavailable'}.</strong> {snapshot.worker.available
      ? 'Python and local MCP tool calls can be picked up by the worker.'
      : snapshot.worker.reason === 'not_configured' ? 'Python and local MCP tool calls cannot run until the worker is configured.'
      : snapshot.worker.reason === 'stale' ? 'The worker has stopped reporting; pending Python and local MCP calls will wait.'
      : snapshot.worker.reason === 'policy_mismatch' ? 'The worker configuration differs from this gateway; pending Python and local MCP calls will wait.'
      : 'The worker is not responding; pending Python and local MCP calls will wait.'}</p>}
    {loading && <p className="extension-hub-state" role="status">Reading saved integration state…</p>}
    {error && <div className="extension-hub-alert" role="alert">{error}{blocked && <p>The outcome may be unknown. Inspect saved state before another write.</p>}</div>}
    {notice && <p className="extension-hub-notice" role="status">{notice}</p>}
    {!loading && !snapshot && <button className="button button-primary" type="button" onClick={refresh}>Try again</button>}
    {snapshot && tab === 'catalog' && <div className="extension-hub-body">
      <div className="extension-hub-toolbar"><label className="extension-hub-search"><span className="sr-only">Search skills and tools</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search skills and tools" /></label>
        <div className="extension-hub-filters" aria-label="Catalog filter">{(['all', 'tools', 'skills', 'packages'] as const).map((value) => <button type="button" key={value} aria-pressed={filter === value} onClick={() => { setFilter(value); setMobileDetail(false); }}>{value === 'all' ? 'All' : value[0].toUpperCase() + value.slice(1)}</button>)}</div></div>
      <div className={`extension-hub-columns ${mobileDetail ? 'show-detail' : ''}`}>
        <div className="extension-hub-list" aria-label="Capability catalog">{cards.length === 0 ? <p>No matching capabilities. Create a draft in Tool Foundry.</p> : cards.map((item) =>
          <button key={item.key} type="button" className="extension-hub-item" aria-pressed={active?.key === item.key} onClick={() => { setSelection(item.key); setMobileDetail(true); }}>
            <span className="extension-hub-glyph" aria-hidden="true">{item.type === 'Tool' ? '◇' : item.type === 'Package' ? '▣' : '✦'}</span><span><strong>{item.title}</strong><small>{item.type} · {item.status}</small><span>{item.subtitle}</span></span></button>)}</div>
        <section className="extension-hub-detail" aria-label="Capability details">{active ? <>
          <button className="extension-hub-back" type="button" onClick={() => setMobileDetail(false)}>← Back to list</button>
          <div className="extension-hub-detail-top"><div><p className="eyebrow">{active.type}</p><h2>{active.title}</h2></div><span className="extension-hub-pill">{active.status}</span></div>
          <p>{active.subtitle || 'No description supplied.'}</p>
          {activePackage && <><h3>Package state</h3><p>Version {activePackage.package_version}</p>
            {compatibility && compatibilityPackageId === activePackage.id && <details open><summary>Import compatibility</summary><p>Format: {compatibility.format}</p><ul>{compatibility.components.map((entry, index) => <li key={index}>{entry.component}: {entry.status}. {entry.reason}</li>)}</ul></details>}
            {Array.isArray(activePackage.manifest.tools) && <p>Tools: {activePackage.manifest.tools.length}</p>}
            {Array.isArray(activePackage.manifest.skills) && <p>Skills: {activePackage.manifest.skills.length}</p>}
            {Array.isArray(activePackage.manifest.connections) && <p>Connection templates: {activePackage.manifest.connections.length}</p>}
            {packageSlots.length > 0 && <div className="extension-hub-bindings"><h3>Connection bindings</h3><p>Choose a saved project connection for each declared requirement. The gateway checks package state, connection state, and operation access at use time.</p>
              {bindingError && <p role="alert" className="form-error">Could not read saved bindings: {bindingError}</p>}
              {packageSlots.map(({ template: slot }) => {
                const current = bindings[activePackage.id]?.find((record) => record.slot === slot.id);
                const choiceKey = `${activePackage.id}:${slot.id}`;
                const available = snapshot?.connections.filter((connection) => {
                  const source = snapshot.packages.find((item) => item.id === connection.package_id);
                  const sourceTemplate = templateOptions.find(({ packageItem, template }) => packageItem.id === connection.package_id && template.id === connection.template_id)?.template;
                  return source?.state === 'active' && sourceTemplate?.kind === slot.kind && slot.operationIds.every((id) => sourceTemplate.operationIds.includes(id));
                }) ?? [];
                return <div className="extension-hub-binding" key={slot.id}><label className="field"><span className="field-label">{slot.label} ({slot.kind.toUpperCase()})</span><select value={bindingChoices[choiceKey] ?? current?.connection_id ?? ''} onChange={(event) => setBindingChoices((state) => ({ ...state, [choiceKey]: event.target.value }))}><option value="">Choose a connection</option>{available.map((item) => <option key={item.id} value={item.id}>{item.label}{item.enabled ? '' : ' · disabled'}</option>)}</select></label>
                  <button type="button" className="button button-outline" disabled={busy || blocked || !bindings[activePackage.id] || !(bindingChoices[choiceKey] ?? current?.connection_id)} onClick={() => { const connectionId = bindingChoices[choiceKey] ?? current?.connection_id; if (!connectionId) return;
                    void perform(async () => { const saved = await putPackageBinding(projectId, activePackage.id, slot.id, connectionId, current?.version ?? 0);
                      const records = await listPackageBindings(projectId, activePackage.id);
                      if (!records.some((record) => record.slot === slot.id && record.connection_id === saved.connection_id && record.version === saved.version)) throw new ApiError('Connection binding was not confirmed in saved state.', null);
                      if (activeProject.current === projectId) { setBindings((state) => ({ ...state, [activePackage.id]: records })); setBindingChoices((state) => {
                        if (state[choiceKey] !== connectionId) return state;
                        const next = { ...state }; delete next[choiceKey]; return next;
                      }); }
                      return 'Connection binding saved and confirmed.'; }); }}>Save binding</button>
                  {current && <small>Saved binding · version {current.version}</small>}{available.length === 0 && <p className="extension-hub-footnote">No compatible active source package connection is available.</p>}</div>;
              })}</div>}
            <p>Saving a package does not grant its tools to roles. Activation and revocation require an exact approval.</p>
            <div className="extension-hub-actions"><label className="field"><span className="field-label">Export format</span><select value={packageFormat} onChange={(event) => setPackageFormat(event.target.value as PackageFormat)}><option value="onefoundry">OneFoundry</option><option value="codex-portable">Codex portable</option><option value="codex-legacy">Codex legacy</option><option value="claude">Claude</option></select></label><button type="button" className="button button-outline" disabled={busy} onClick={() => downloadPackage(activePackage)}>Download ZIP</button></div>
            {confirmAction === activePackage.id ? <div className="extension-hub-actions"><span>Request {activePackage.state === 'active' ? 'revocation' : 'activation'} for this exact version?</span><button type="button" className="button button-outline" onClick={() => setConfirmAction(null)}>Cancel</button><button type="button" className="button button-primary" disabled={busy || blocked} onClick={() => { void perform(async () => { await proposePackageState(projectId, activePackage, activePackage.state === 'active' ? 'revoke' : 'activate'); setConfirmAction(null); return 'Exact package proposal is pending in Approvals.'; }); }}>Request approval</button></div> :
              <button type="button" className="button button-outline" disabled={busy || blocked} onClick={() => setConfirmAction(activePackage.id)}>{activePackage.state === 'active' ? 'Request revocation' : 'Request activation'}</button>}</>}
          {activeDraft && <><h3>Skill proposal</h3><p>{activeDraft.required_tools.length ? `Requested tools: ${activeDraft.required_tools.map((id) => toolLabel(id, snapshot)).join(', ')}` : 'No tools requested.'}</p><details><summary>Review SKILL.md</summary><pre>{activeDraft.content}</pre></details>
            {activeDraft.status === 'pending' && <div className="extension-hub-actions">{(['accept', 'reject'] as const).map((decision) => <button key={decision} type="button" className="button button-outline" disabled={busy || blocked} onClick={() => { void perform(async () => { const result = await proposeDraftDecision(projectId, activeDraft, decision); const drafts = await listExtensionDrafts(projectId); if (activeProject.current === projectId) setSnapshot((state) => state && { ...state, drafts }); return result; }); }}>{decision === 'accept' ? 'Accept exact draft' : 'Reject exact draft'}</button>)}</div>}</>}
          {activeSkill && <><h3>Package skill</h3><p>{activeSkill.required_tools.length ? `Requires ${activeSkill.required_tools.map((id) => toolLabel(id, snapshot)).join(', ')}` : 'No tools requested.'}</p><p>Availability follows this package version and the role's granted tools.</p><details><summary>Skill instructions</summary><pre>{Array.isArray(activePackage?.manifest.skills) ? String((activePackage.manifest.skills as Record<string, unknown>[]).find((skill) => skill.id === activeSkill.id)?.content ?? 'No instructions included.') : 'No instructions included.'}</pre></details></>}
          {active.type === 'Tool' && <><h3>Access</h3><p>Tool access is decided by the project role grant and Tool Gateway. Skill instructions cannot grant it.</p><div className="extension-hub-actions"><button type="button" className="button button-outline" onClick={() => onNavigate('team')}>Manage role access</button>{activePackage && <button type="button" className="button button-outline" onClick={() => openConnection()}>Configure connection</button>}</div></>}
          <div className="extension-hub-linkrow"><button className="extension-hub-link" type="button" onClick={() => onNavigate('skills')}>Open existing Procedures</button><button className="extension-hub-link" type="button" onClick={() => onNavigate('approvals')}>Open Approvals</button></div>
        </> : <p>Select a capability to inspect its state.</p>}</section>
      </div></div>}
    {snapshot && tab === 'connections' && <div className="extension-hub-body"><div className="extension-hub-toolbar"><div><h2>Project connections</h2><p>Configuration is saved per project. Credentials use the protected account flow.</p></div><button type="button" className="button button-primary" disabled={blocked} onClick={() => openConnection()}>Add connection</button></div>
      <div className={`extension-hub-columns ${mobileDetail ? 'show-detail' : ''}`}><div className="extension-hub-list" aria-label="Saved extension connections">{snapshot.connections.length === 0 ? <p>No extension connections yet. Add one from a package template.</p> : snapshot.connections.map((item) => <button type="button" key={item.id} className="extension-hub-item" aria-pressed={activeConnection?.id === item.id} onClick={() => { setSelection(`connection:${item.id}`); setMobileDetail(true); }}><span className="extension-hub-glyph" aria-hidden="true">↗</span><span><strong>{item.label}</strong><small>{item.enabled ? 'Enabled' : 'Draft'} · version {item.version}</small><span>{item.template_id}</span></span></button>)}</div>
        <section className="extension-hub-detail" aria-label="Connection details">{activeConnection ? <>
          <button className="extension-hub-back" type="button" onClick={() => setMobileDetail(false)}>← Back to list</button>
          <div className="extension-hub-detail-top"><div><p className="eyebrow">Saved connection</p><h2>{activeConnection.label}</h2></div><span className="extension-hub-pill">{activeConnection.enabled ? 'Enabled' : 'Draft'}</span></div>
          <p>Version {activeConnection.version} · {templateOptions.find(({ packageItem, template }) => packageItem.id === activeConnection.package_id && template.id === activeConnection.template_id)?.template.label ?? readableName(activeConnection.template_id)}</p>
          <div className="extension-hub-readiness" aria-label="Connection readiness">
            <span>Configured <strong>{activeReadiness?.configured ? 'Yes' : 'Unconfirmed'}</strong></span>
            <span>Credential <strong>{activeReadiness?.credential_present ? 'Stored' : 'Missing or not needed'}</strong></span>
            <span>Live test <strong>{activeReadiness?.live_test_status ?? 'Not tested'}</strong></span>
            <span>Used successfully <strong>{activeReadiness?.successful_use ? 'Yes' : 'No'}</strong></span>
          </div>
          {readinessError && <p className="form-error" role="alert">Could not read connection readiness: {readinessError}</p>}
          <details><summary>Configuration</summary><pre>{JSON.stringify(activeConnection.config, null, 2)}</pre></details>
          {activeConnection.config.auth_method !== 'none' && <form className="extension-hub-credential" onSubmit={(event) => { event.preventDefault(); const value = credentialInput.current?.value ?? ''; if (!value) return;
            void perform(async () => { const saved = await putExtensionCredential(projectId, activeConnection, value);
              const connections = await listExtensionConnections(projectId);
              const confirmed = connections.find((item) => item.id === activeConnection.id && item.version === saved.version);
              if (!confirmed) throw new ApiError('Credential version was not confirmed in saved state.', null);
              const state = await getExtensionReadiness(projectId, confirmed);
              if (!state.credential_present || state.version !== saved.version) throw new ApiError('Credential presence was not confirmed in saved state.', null);
              if (activeProject.current !== projectId) return 'Project changed; refresh saved state.';
              setSnapshot((current) => current && { ...current, connections }); setReadiness((current) => ({ ...current, [activeConnection.id]: state }));
              if (credentialInput.current) credentialInput.current.value = '';
              return 'Credential saved in the protected store and confirmed. The value has been cleared from this form.';
            }); }}>
            <label className="field"><span className="field-label">Protected credential</span><input ref={credentialInput} required type="password" autoComplete="new-password" /></label>
            <p className="extension-hub-footnote">The value is sent only to the credential endpoint. It is never included in connection configuration or displayed after save.</p>
            <button className="button button-outline" type="submit" disabled={busy || blocked}>Save credential</button>
          </form>}
          <p className="extension-hub-footnote">The Tool Gateway checks project and role grants and approvals for each invocation.</p>
          <div className="extension-hub-actions">
            <button type="button" className="button button-outline" disabled={busy || blocked || activeConnection.config.transport === 'stdio'} onClick={() => openConnection('', activeConnection)}>Edit settings</button>
            <button type="button" className="button button-outline" disabled={busy || blocked} onClick={() => { void perform(async () => {
              const changed = await updateExtensionConnection(projectId, activeConnection, { enabled: !activeConnection.enabled });
              const connections = await listExtensionConnections(projectId);
              if (!connections.some((item) => item.id === changed.id && item.version === changed.version)) throw new ApiError('Connection change was not confirmed in saved state.', null);
              if (activeProject.current === projectId) setSnapshot((state) => state && { ...state, connections });
              return changed.enabled ? 'Connection enabled and confirmed.' : 'Connection disabled and confirmed.';
            }); }}>{activeConnection.enabled ? 'Disable connection' : 'Enable connection'}</button>
            <button type="button" className="button button-outline" disabled={busy || blocked || activeConnection.config.transport === 'stdio'} onClick={() => { const destination = String(activeConnection.config.endpoint ?? '');
              void perform(async () => { await probeExtensionConnection(projectId, activeConnection, destination); const state = await getExtensionReadiness(projectId, activeConnection); if (activeProject.current === projectId) setReadiness((current) => ({ ...current, [activeConnection.id]: state }));
                return `Connection handshake completed for ${destination}. This does not enable a tool or prove an end-to-end task.`; }); }}>Test connection at {String(activeConnection.config.endpoint ?? 'configured endpoint')}</button>
          </div>
        </> : <><h2>Choose a connection</h2><p>Saved connection details appear here.</p></>}</section></div>
      <section className="extension-hub-webhooks" aria-label="Incoming webhooks"><div className="extension-hub-toolbar"><div><h2>Incoming webhooks</h2><p>Signed event intake for this project. Create disabled, configure the sender, then enable.</p></div></div>
        <div className="extension-hub-columns"><div><form className="extension-hub-form" onSubmit={saveWebhook}><h3>Add a webhook</h3><label className="field"><span className="field-label">Name</span><input required maxLength={120} value={webhookName} onChange={(event) => setWebhookName(event.target.value)} placeholder="CRM events" /></label><label className="field"><span className="field-label">Signing secret</span><input ref={webhookSecretInput} type={webhookSecretVisible ? 'text' : 'password'} autoComplete="new-password" maxLength={128} /></label><div className="extension-hub-actions"><button type="button" className="button button-outline" onClick={() => generateWebhookSecret(webhookSecretInput.current)}>Generate secret</button><button type="button" className="button button-outline" onClick={() => setWebhookSecretVisible((current) => !current)}>{webhookSecretVisible ? 'Hide secret' : 'Show secret'}</button></div><p className="extension-hub-footnote">Copy the secret into the sending service before saving. OneFoundry will not display it again.</p><button className="button button-primary" type="submit" disabled={busy || blocked}>Save disabled webhook</button></form>
          {snapshot.webhooks.length > 0 && <div className="extension-hub-webhook-list" aria-label="Saved webhooks">{snapshot.webhooks.map((item) => <button key={item.id} type="button" className="extension-hub-item" aria-pressed={activeWebhook?.id === item.id} onClick={() => setSelectedWebhook(item.id)}><span className="extension-hub-glyph" aria-hidden="true">↙</span><span><strong>{item.name}</strong><small>{item.enabled ? 'Enabled' : 'Disabled'} · version {item.version}</small></span></button>)}</div>}</div>
          <section className="extension-hub-detail" aria-label="Webhook details">{activeWebhook ? <><div className="extension-hub-detail-top"><h3>{activeWebhook.name}</h3><span className="extension-hub-pill">{activeWebhook.enabled ? 'Enabled' : 'Disabled'}</span></div><p>Signing secret: {activeWebhook.credential_present ? 'Stored' : 'Missing'}</p><details><summary>Sender configuration</summary><p>Send signed POST requests to this URL:</p><code className="extension-hub-url">{webhookReceivePath}</code><p>Include timestamp, delivery ID, and SHA-256 HMAC signature headers.</p></details>
            <div className="extension-hub-actions"><button className="button button-outline" type="button" disabled={busy || blocked} onClick={() => { void perform(async () => { const changed = await updateExtensionWebhook(projectId, activeWebhook, !activeWebhook.enabled); const webhooks = await listExtensionWebhooks(projectId); if (!webhooks.some((item) => item.id === changed.id && item.version === changed.version && item.enabled === changed.enabled)) throw new ApiError('Webhook status was not confirmed in saved state.', null); if (activeProject.current === projectId) setSnapshot((state) => state && { ...state, webhooks }); return changed.enabled ? 'Webhook enabled and confirmed.' : 'Webhook disabled and confirmed.'; }); }}>{activeWebhook.enabled ? 'Disable webhook' : 'Enable webhook'}</button></div>
            <form className="extension-hub-credential" onSubmit={(event) => rotateWebhook(activeWebhook, event)}><label className="field"><span className="field-label">New signing secret</span><input ref={webhookRotationInput} type={webhookSecretVisible ? 'text' : 'password'} autoComplete="new-password" maxLength={128} /></label><button type="button" className="button button-outline" onClick={() => generateWebhookSecret(webhookRotationInput.current)}>Generate new secret</button><button className="button button-outline" type="submit" disabled={busy || blocked}>Rotate signing secret</button></form></> : <p>No incoming webhooks configured.</p>}</section></div>
      </section>
      <div className="extension-hub-invocations"><div className="extension-hub-toolbar"><div><h2>Recent tool calls</h2><p>Each external call has its own saved result and approval state.</p></div><button className="extension-hub-link" type="button" onClick={() => onNavigate('approvals')}>Open Approvals</button></div>
        {snapshot.invocations.length === 0 ? <p className="extension-hub-footnote">No extension tool calls have been recorded for this project.</p> : <ul>{snapshot.invocations.slice(0, 10).map((item) => <li key={item.invocation_id}><div><strong>{toolLabel(item.tool_id, snapshot)}</strong><small>{item.status}{item.reason ? ` · ${item.reason}` : ''}</small></div><div className="extension-hub-actions">{item.status === 'approval_required' && <button type="button" className="button button-outline" disabled={busy || blocked} onClick={() => { void perform(async () => { const result = await executeExtensionInvocation(projectId, item); const invocations = await listExtensionInvocations(projectId); if (activeProject.current === projectId) setSnapshot((state) => state && { ...state, invocations }); return result.status === 'approval_required' ? 'This exact call still awaits approval. Review it in Approvals.' : `Saved tool call status: ${result.status}.`; }); }}>Execute after approval</button>}{['approval_required', 'ready', 'running', 'waiting'].includes(item.status) && <button type="button" className="button button-outline" disabled={busy || blocked} onClick={() => { void perform(async () => { const result = await cancelExtensionInvocation(projectId, item); const invocations = await listExtensionInvocations(projectId); if (activeProject.current === projectId) setSnapshot((state) => state && { ...state, invocations }); return result.status === 'cancelled' ? 'Tool call cancelled and confirmed.' : 'Cancellation requested. Refresh to inspect the final receipt.'; }); }}>Cancel call</button>}</div></li>)}</ul>}
      </div>
      <div className="extension-hub-linkrow"><button className="extension-hub-link" type="button" onClick={() => onNavigate('connections')}>Open Accounts & files</button></div></div>}
    {snapshot && tab === 'foundry' && <div className="extension-hub-body"><div className="extension-hub-foundry-head"><div><p className="eyebrow">Create and review</p><h2>Tool Foundry</h2><p>Prepare a skill or inert package, then inspect its exact saved version before approval.</p></div><button type="button" className="button button-outline" onClick={() => onNavigate('work')}>Ask Chief to draft</button></div>
      <div className="extension-hub-create-choices"><button type="button" aria-pressed={authorKind === 'skill'} onClick={() => setAuthorKind('skill')}><strong>✦ Skill procedure</strong><span>Write instructions and select required tools.</span></button><button type="button" aria-pressed={authorKind === 'package'} onClick={() => setAuthorKind('package')}><strong>▣ Plugin package</strong><span>Import a declarative manifest and source files.</span></button></div>
      <div className="extension-hub-steps" aria-label="Authoring flow"><span><b>1</b> Describe</span><span><b>2</b> Configure</span><span><b>3</b> Review</span><span><b>4</b> Request enable</span></div>
      {authorKind === 'skill' ? <form className="extension-hub-form" onSubmit={saveSkill}><h3>Skill proposal</h3><label className="field"><span className="field-label">Name</span><input required maxLength={120} value={skillName} onChange={(event) => setSkillName(event.target.value)} placeholder="Customer account briefing" /></label><label className="field"><span className="field-label">When to use it</span><textarea required maxLength={500} rows={2} value={skillDescription} onChange={(event) => setSkillDescription(event.target.value)} /></label><label className="field"><span className="field-label">SKILL.md instructions</span><textarea required maxLength={16000} rows={9} value={skillContent} onChange={(event) => setSkillContent(event.target.value)} placeholder={'# Customer account briefing\n\nUse the approved lookup tool, then cite the returned source.'} /></label>
        <fieldset><legend>Requested tools</legend>{snapshot.catalog.tools.filter((tool) => tool.status === 'active').length === 0 ? <p>No active tools in the catalog.</p> : <div className="extension-hub-toolchoices">{snapshot.catalog.tools.filter((tool) => tool.status === 'active').map((tool) => <label key={tool.id}><input type="checkbox" value={tool.id} checked={requiredTools.includes(tool.id)} onChange={(event) => setRequiredTools((current) => event.target.checked ? [...current, tool.id] : current.filter((value) => value !== tool.id))} /><span>{toolLabel(tool.id, snapshot)}</span></label>)}</div>}</fieldset><p className="extension-hub-footnote">Selected tools use the shared project connections configured in Connections. This request does not change profile grants; use <button type="button" className="extension-hub-link" onClick={() => onNavigate('team')}>Team</button> to manage role access. Tool policy and any required approval still apply when a run starts.</p><div className="extension-hub-actions"><button type="button" className="button button-outline" onClick={() => openConnection()}>Configure connection</button><button type="submit" className="button button-primary" disabled={busy || blocked}>Save proposal</button></div></form> :
        <><form className="extension-hub-form" onSubmit={importPackage}><h3>Import a package ZIP</h3><p>Import an existing OneFoundry, Codex, or Claude package. Unsupported components remain inactive for review.</p><label className="field"><span className="field-label">Package format</span><select value={packageFormat} onChange={(event) => setPackageFormat(event.target.value as PackageFormat)}><option value="onefoundry">OneFoundry</option><option value="codex-portable">Codex portable</option><option value="codex-legacy">Codex legacy</option><option value="claude">Claude</option></select></label><label className="field"><span className="field-label">ZIP archive (up to 4 MiB)</span><input ref={importFile} type="file" accept=".zip,application/zip" /></label><button className="button button-primary" type="submit" disabled={busy || blocked}>Import inert draft</button></form>
        <form className="extension-hub-form" onSubmit={savePackage}><h3>Write a package</h3><p>Start from a template, then edit the manifest and source files. Saving validates an inert draft. The Webhook template sends events outward; signed incoming webhooks are configured in Connections.</p><div className="extension-hub-filters" aria-label="Package templates">{([['plugin', 'Plugin'], ['python', 'Python tool'], ['api', 'API tool'], ['mcp', 'MCP connection'], ['webhook', 'Outbound webhook']] as const).map(([kind, label]) => <button key={kind} type="button" aria-pressed={preset === kind} onClick={() => choosePackagePreset(kind)}>{label}</button>)}</div><label className="field"><span className="field-label">extension.json</span><textarea required rows={13} value={manifestText} onChange={(event) => setManifestText(event.target.value)} spellCheck={false} /></label><label className="field"><span className="field-label">Source files (JSON map)</span><textarea required rows={5} value={filesText} onChange={(event) => setFilesText(event.target.value)} spellCheck={false} placeholder={'{"skills/example/SKILL.md":"---\\nname: example\\n---\\n..."}'} /></label><div className="extension-hub-actions"><button type="button" className="button button-outline" onClick={() => openConnection()}>Configure connection</button><button className="button button-primary" type="submit" disabled={busy || blocked}>Save inert package</button></div></form></>}
      <p className="extension-hub-footnote">The existing <button type="button" className="extension-hub-link" onClick={() => onNavigate('skills')}>Procedures page</button> remains available for direct versioned procedures. Agent-created proposals start from a Chief work order.</p></div>}
    {connectionOpen && <div className="extension-hub-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) closeConnection(); }}>
      <section ref={connectionDialog} className="extension-hub-drawer" role="dialog" aria-modal="true" aria-labelledby="extension-connection-title" onKeyDown={connectionDialogKey}>
        <div className="extension-hub-detail-top"><div><p className="eyebrow">Shared project connection</p><h2 id="extension-connection-title">Configure connection</h2></div><button className="button button-outline" type="button" onClick={closeConnection}>Close</button></div>
        <p>Choose a package template or review an existing project connection. The credential is entered separately.</p>
        {snapshot && snapshot.connections.length > 0 && !editingConnection && <div className="extension-hub-existing"><h3>Existing connections</h3>{snapshot.connections.map((item) => <button type="button" key={item.id} className="extension-hub-link" onClick={() => { closeConnection(); setTab('connections'); setSelection(`connection:${item.id}`); setMobileDetail(true); }}>{item.label} · {item.enabled ? 'enabled' : 'draft'}</button>)}</div>}
        <form className="extension-hub-form" onSubmit={saveConnection}>
          <label className="field"><span className="field-label">Connection template</span><select required disabled={!!editingConnection} value={templateChoice} onChange={(event) => setTemplateChoice(event.target.value)}><option value="">Choose a template</option>{templateOptions.map(({ packageItem, template }) => <option key={`${packageItem.id}:${template.id}`} value={`${packageItem.id}:${template.id}`}>{packageDisplayName(packageItem)} · v{packageItem.package_version} · {readableName(packageItem.state)} · {template.label} ({template.kind.toUpperCase()})</option>)}</select></label>
          {selectedTemplate && <div className="extension-hub-template"><p className="extension-hub-footnote">{selectedTemplate.template.kind.toUpperCase()} · configure an HTTPS endpoint below</p><details><summary>Declared operations</summary>{selectedTemplate.template.operations.length ? <ul>{selectedTemplate.template.operations.map((operation) => <li key={operation}>{operation}</li>)}</ul> : <p>No operations declared.</p>}</details></div>}
          <label className="field"><span className="field-label">Label</span><input required disabled={!!editingConnection} maxLength={80} value={connectionLabel} onChange={(event) => setConnectionLabel(event.target.value)} placeholder="Company service" /></label>
          <label className="field"><span className="field-label">HTTPS endpoint</span><input required type="url" value={endpoint} onChange={(event) => setEndpoint(event.target.value)} placeholder="https://service.example/mcp" /></label>
          <label className="field"><span className="field-label">Authentication method</span><select value={authMethod} onChange={(event) => setAuthMethod(event.target.value as typeof authMethod)}><option value="none">None</option><option value="bearer">Bearer token</option><option value="api_key">API key header</option></select></label>
          {authMethod === 'api_key' && <label className="field"><span className="field-label">Header name</span><input required value={headerName} onChange={(event) => setHeaderName(event.target.value)} placeholder="X-API-Key" /></label>}
          <p className="extension-hub-footnote">This saves connection settings only. Credentials use a separate protected write-only step; never paste them into the endpoint or package.</p>
          {connectionError && <p className="form-error" role="alert">{connectionError}</p>}
          <div className="extension-hub-actions"><button className="button button-outline" type="button" onClick={() => { closeConnection(); onNavigate('connections'); }}>Accounts & files</button><button className="button button-primary" type="submit" disabled={busy || blocked || !selectedTemplate}>{editingConnection ? 'Save connection settings' : 'Save connection draft'}</button></div>
        </form>
      </section>
    </div>}
  </section>;
}
