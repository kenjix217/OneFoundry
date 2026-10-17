import { useState } from 'react';

import type { Project } from './api';
import { CandidateReview } from './CandidateReview';
import { ConversationCapture } from './ConversationCapture';
import { MemoryHistory } from './MemoryHistory';
import { MemorySearch } from './MemorySearch';
import { ResultIntake } from './ResultIntake';
import { SyntheticMemoryIntake } from './SyntheticMemoryIntake';

type Props = { projectId: string; projects: Pick<Project, 'id' | 'name'>[] };

export function MemoryView({ projectId, projects }: Props) {
  const [tab, setTab] = useState<'browse' | 'add' | 'import' | 'capture' | 'history'>('browse');
  const [reloadToken, setReloadToken] = useState(0);
  const changed = () => setReloadToken((value) => value + 1);

  return (
    <div className="memory-view">
      <div className="view-heading"><div><p className="eyebrow">Knowledge with a source</p><h1>Memory</h1>
        <p>Find what your team knows, review new knowledge, and keep useful results for future work.</p></div></div>
      <div className="inbox-boundary"><span aria-hidden="true">◇</span><p><strong>Reviewed and source based.</strong> Approve each fact before configured agents use it. External access follows the grants you approve.</p></div>
      <nav className="workspace-tabs" aria-label="Memory pages">{([['browse', 'Browse & review'], ['add', 'Add a result'], ['import', 'Import verified results'], ['capture', 'Capture conversation'], ['history', 'History']] as const).map(([value, label]) => <button type="button" key={value} aria-current={tab === value ? 'page' : undefined} onClick={() => setTab(value)}>{label}</button>)}</nav>
      <div className="memory-browse" hidden={tab !== 'browse'}>
        <MemorySearch projectId={projectId} projects={projects} reloadToken={reloadToken} />
        <CandidateReview projectId={projectId} reloadToken={reloadToken} onChanged={changed} />
      </div>
      <div className="memory-workflow" hidden={tab !== 'add'}>
        <ResultIntake projectId={projectId} reloadToken={reloadToken} onChanged={changed} />
      </div>
      <div className="memory-workflow" hidden={tab !== 'import'}>
        <SyntheticMemoryIntake projectId={projectId} reloadToken={reloadToken} onChanged={changed} />
      </div>
      {tab === 'capture' && <ConversationCapture key={projectId} projectId={projectId} reloadToken={reloadToken} onChanged={changed} />}
      {tab === 'history' && <MemoryHistory key={projectId} projectId={projectId} onChanged={changed} />}
    </div>
  );
}
