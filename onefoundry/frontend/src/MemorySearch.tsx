import { useEffect, useState, type FormEvent } from 'react';

import type { Project } from './api';
import { MemoryLifecycle } from './MemoryLifecycle';
import { listMemory, type MemoryItem, type MemoryScope } from './memoryApi';

type Props = { projectId: string; projects: Pick<Project, 'id' | 'name'>[]; reloadToken: number };
type ScopeFilter = 'all' | MemoryScope;

function time(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function MemorySearch({ projectId, projects, reloadToken }: Props) {
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>('all');
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void listMemory(projectId, {
      query: search, scope: scopeFilter === 'all' ? undefined : scopeFilter, signal: controller.signal,
    }).then((results) => {
      setItems(results);
      setLoading(false);
    }).catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      setError(cause instanceof Error ? cause.message : 'Memory retrieval failed.');
      setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, search, scopeFilter, reload, reloadToken]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSearch(query.trim());
  }

  return (
    <section className="memory-search panel" aria-labelledby="memory-search-heading">
      <div className="section-heading"><div><p className="eyebrow">Published memory</p><h2 id="memory-search-heading">Browse and search</h2>
        <p>Search approved company knowledge.</p></div></div>
      <form className="memory-search-form" role="search" onSubmit={submit}>
        <label className="field"><span className="field-label">Search memory</span><input maxLength={120} value={query}
          onChange={(event) => setQuery(event.target.value)} placeholder="Find a phrase in approved memory" /></label>
        <button className="button button-primary" type="submit">Search</button>
        {search && <button className="button button-quiet" type="button" onClick={() => { setQuery(''); setSearch(''); }}>Clear</button>}
      </form>
      <div className="memory-scope-filter" role="group" aria-label="Memory scope">
        {(['all', 'project', 'company'] as ScopeFilter[]).map((scope) =>
          <button key={scope} type="button" aria-pressed={scopeFilter === scope} className={scopeFilter === scope ? 'active' : ''}
            onClick={() => { setLoading(true); setItems([]); setScopeFilter(scope); }}>{scope === 'all' ? 'All scopes' : scope === 'project' ? 'This project' : 'Company'}</button>)}
      </div>
      <p className="quiet-note">Project knowledge stays with this project. Company knowledge is shared across projects.</p>
      {loading && <div className="loading-panel" role="status"><span className="loading-orb" />Retrieving memory…</div>}
      {error && !loading && <div className="empty-inline"><p role="alert">{error}</p><button className="button button-outline" type="button" onClick={() => setReload((value) => value + 1)}>Try again</button></div>}
      {!loading && !error && items.length === 0 && <div className="empty-inline">{search ? 'No approved memory matched this search and scope.' : 'No approved memory is available in this scope.'}</div>}
      {!loading && !error && items.length > 0 && <div className="memory-list" aria-label="Approved memory results">{items.map((item) =>
        <article key={item.id} className="memory-item">
          <div className="detail-top"><span className={`status-chip ${item.scope === 'company' ? 'status-company' : 'status-soft'}`}>{item.scope === 'company' ? 'Company' : 'Project'}</span>
            <time dateTime={item.created_at}>{time(item.created_at)}</time></div>
          <p className="memory-text">{item.text}</p>
          <div className="memory-source"><span>Reviewed source result</span>

            <span>Source project <strong>{projects.find((project) => project.id === item.project_id)?.name ?? 'Previous project'}</strong></span>

            {search && <span>{item.similarity != null && item.score === 0 ? 'Matched by meaning' : <>Search matches <strong>{item.score}</strong></>}</span>}</div>
          <MemoryLifecycle projectId={projectId} item={item} onChanged={() => setReload((value) => value + 1)} />
        </article>)}</div>}
    </section>
  );
}
