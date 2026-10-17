import { useEffect, useState } from 'react';
import { capabilityLabel } from './ProfileCapabilities';

import { readOneFoundryCapability, type OneFoundryCapability } from './onefoundryCapabilityApi';

export function OneFoundryCapabilityStatus({ projectId, profileId }: { projectId: string; profileId: string }) {
  const [readback, setReadback] = useState<OneFoundryCapability | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setReadback(null);
    setError(false);
    void readOneFoundryCapability(projectId, profileId, controller.signal)
      .then(setReadback)
      .catch((failure: unknown) => {
        if (!(failure instanceof DOMException && failure.name === 'AbortError')) setError(true);
      });
    return () => controller.abort();
  }, [projectId, profileId]);

  if (error) return <p className="profile-capability" role="status">Capability readback unavailable. Check the saved state before relying on it.</p>;
  if (!readback) return <p className="profile-capability" role="status">Loading saved capability…</p>;
  const run = readback.last_run;
  return (
    <div className="profile-capability" aria-label="Saved capability and run observation">
      <p>Configured model: {readback.configured_model_id ?? 'None'}</p>
      <p>Capabilities: {readback.configured_tool_ids.length ? readback.configured_tool_ids.map(capabilityLabel).join(', ') : 'None'}</p>
      <p>{run ? `Last saved run: ${run.status}` : 'No saved run for this profile.'}</p>
      {run && <p>{run.tools_active === null ? 'No live-text tool admission was recorded for that run.' :
        run.tools_active ? 'Live-text tool route was active for that run.' : 'Live-text tool route was inactive for that run.'}</p>}
      <p>Every action is checked against the current permissions.</p>
    </div>
  );
}
