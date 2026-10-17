import { useState, type FormEvent } from 'react';
import { ApiError } from './api';
import { readDocumentRange, searchDocuments, type DocumentMatch, type DocumentSource } from './documentApi';

export function DocumentSearch({ projectId, sources }: { projectId: string; sources: DocumentSource[] }) {
  const [source, setSource] = useState('');
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<DocumentMatch[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [selected, setSelected] = useState<DocumentMatch | null>(null);
  const [sheet, setSheet] = useState('');
  const [address, setAddress] = useState('A1:B2');
  const [values, setValues] = useState<unknown[][] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = sources.filter((item) => item.status === 'ready');
  async function read(event: FormEvent, range: boolean) {
    event.preventDefault(); setBusy(true); setError(null); setValues(null);
    try {
      if (range && selected) setValues(await readDocumentRange(projectId, source, selected.file_id, sheet, address));
      else { setSelected(null); const result = await searchDocuments(projectId, source, query); setMatches(result.files); setTruncated(result.truncated); }
    } catch (failure) { setError(failure instanceof ApiError ? failure.message : 'Could not read the selected folder.'); }
    finally { setBusy(false); }
  }
  if (ready.length === 0) return null;
  return <section aria-label="Find documents">
    <h3>Find documents</h3>
    <p>Search names directly inside one checked folder. Agents use the same folder boundary.</p>
    {error && <p className="form-error" role="alert">{error}</p>}
    <form className="skill-create" onSubmit={(event) => { void read(event, false); }}>
      <label className="field"><span className="field-label">Search folder</span><select required value={source} onChange={(event) => { setSource(event.target.value); setMatches([]); setSelected(null); setValues(null); }}><option value="">Choose a folder</option>{ready.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label className="field"><span className="field-label">File name contains</span><input required maxLength={100} value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      <button className="button button-outline" disabled={busy || !source}>Search files</button>
    </form>
    {truncated && <p>The search reached its configured limit. Narrow the file name to find more matches.</p>}
    <ul>{matches.map((item) => <li key={item.file_id}><strong>{item.name}</strong>{' '}<button className="button button-quiet" onClick={() => { setSelected(item); setValues(null); }}>Read cells</button><details><summary>File details</summary><p>{item.file_id} · {item.revision}</p></details></li>)}</ul>
    {selected && <form className="skill-create" onSubmit={(event) => { void read(event, true); }}>
      <p>Read cells from <strong>{selected.name}</strong> (Excel or Google Sheets).</p>
      <label className="field"><span className="field-label">Worksheet name</span><input required maxLength={100} value={sheet} onChange={(event) => setSheet(event.target.value)} /></label>
      <label className="field"><span className="field-label">Cell range</span><input required maxLength={30} value={address} onChange={(event) => setAddress(event.target.value)} /></label>
      <button className="button button-outline" disabled={busy}>Read exact range</button>
    </form>}
    {values && <div style={{ overflowX: 'auto' }}><table aria-label="Cell values"><tbody>{values.map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c}>{String(cell)}</td>)}</tr>)}</tbody></table></div>}
  </section>;
}
