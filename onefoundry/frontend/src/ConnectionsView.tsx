import { useState } from 'react';
import { ConnectedAccounts } from './ConnectedAccounts';
import { DocumentSources } from './DocumentSources';
import { MailInboxes } from './MailInboxes';
import { MailOutgoing } from './MailOutgoing';
import { ZohoMail } from './ZohoMail';
import type { Connector } from './connectorApi';

export function ConnectionsView({ projectId }: { projectId: string }) {
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [tab, setTab] = useState<'accounts' | 'incoming' | 'outgoing' | 'zoho' | 'files'>(
    new URLSearchParams(window.location.hash.split('?')[1] ?? '').has('zoho') ? 'zoho' : 'accounts',
  );
  return <section className="connections-workspace"><header className="view-heading"><div><p className="eyebrow">Connect once, use where you work</p><h1>Accounts &amp; files</h1><p>Manage your connected accounts, email, and watched folders in one place.</p></div></header>
    <nav className="workspace-tabs" aria-label="Connection types">{([['accounts', 'Accounts'], ['incoming', 'Incoming mail'], ['outgoing', 'Outgoing mail'], ['zoho', 'Zoho Mail'], ['files', 'Watched folders']] as const).map(([value, label]) => <button key={value} type="button" aria-current={tab === value ? 'page' : undefined} onClick={() => setTab(value)}>{label}</button>)}</nav>
    <div className="connections-content">
      <div hidden={tab !== 'accounts'}><ConnectedAccounts projectId={projectId} onChange={setConnectors} /></div>
      {tab === 'incoming' && <MailInboxes projectId={projectId} connectors={connectors} />}
      {tab === 'outgoing' && <MailOutgoing projectId={projectId} />}
      {tab === 'zoho' && <ZohoMail projectId={projectId} />}
      {tab === 'files' && <DocumentSources projectId={projectId} connectors={connectors} />}
    </div>
  </section>;
}
