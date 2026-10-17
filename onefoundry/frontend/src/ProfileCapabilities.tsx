import { useEffect, useState } from 'react';
import { getCoordination } from './coordinationApi';

export function capabilityLabel(value: string): string {
  const words = value.replace(/_v\d+$/, '').replaceAll('_', ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function ProfileCapabilities({ projectId, selected, disabled, onChange }: { projectId: string; selected: string[]; disabled: boolean; onChange: (tools: string[]) => void }) {
  const [registered, setRegistered] = useState<string[] | null>(null);
  const [registeredLabels, setRegisteredLabels] = useState<Record<string, string>>({});
  const [error, setError] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setError(false); setRegistered(null); setRegisteredLabels({});
    void getCoordination(projectId, controller.signal).then((value) => {
      if (!controller.signal.aborted) { setRegistered(value.registered_tools); setRegisteredLabels(value.registered_tool_labels ?? {}); }
    }).catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [projectId, refresh]);
  const choices = [...new Set([...(registered ?? []), ...selected])];
  return <fieldset className="profile-capabilities" disabled={disabled}><legend>Capabilities</legend>
    <p className="field-hint">Choose what this agent may request. Each action is still checked when work runs; outbound actions need approval.</p>
    {error && <p role="status">Capabilities could not be loaded. Your saved choices are retained. <button type="button" className="button button-quiet" onClick={() => setRefresh((value) => value + 1)}>Try loading capabilities again</button></p>}
    {!registered && !error && <p role="status">Loading capabilities…</p>}
    {registered?.length === 0 && selected.length === 0 && <p>No capabilities are available in this installation.</p>}
    <div className="capability-choices">{choices.map((tool) => <label key={tool}><input type="checkbox" value={tool} checked={selected.includes(tool)} onChange={(event) => onChange(event.target.checked ? [...selected, tool] : selected.filter((item) => item !== tool))} /><span>{registeredLabels[tool]?.trim() ? registeredLabels[tool] : capabilityLabel(tool)}{registered && !registered.includes(tool) && <small>Saved choice · currently unavailable</small>}</span></label>)}</div>
  </fieldset>;
}
