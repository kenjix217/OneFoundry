import { useEffect, useState } from 'react';

import { getInfraStatus, type InfraSnapshot, type InfraService } from './infraApi';

function serviceLabel(service: InfraService): string {
  if (service.state !== 'running') return service.state;
  return service.health === 'none' ? 'running' : service.health;
}

export function InfraView() {
  const [snapshot, setSnapshot] = useState<InfraSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => setRefresh((value) => value + 1), 5000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void getInfraStatus(controller.signal).then((value) => {
      setSnapshot(value);
      setLoading(false);
    }).catch((failure: unknown) => {
      if (failure instanceof DOMException && failure.name === 'AbortError') return;
      setError(failure instanceof Error ? failure.message : 'Infra status is unavailable.');
      setLoading(false);
    });
    return () => controller.abort();
  }, [refresh]);

  return (
    <section aria-label="Infra monitoring" className="infra-panel">
      <div className="infra-heading">
        <div><p className="eyebrow">Infra</p><h2>Local stack</h2></div>
        <button className="button button-secondary" type="button" onClick={() => setRefresh((value) => value + 1)} disabled={loading}>
          {loading ? 'Checking…' : 'Refresh view'}
        </button>
      </div>
      <p className="infra-description">Read-only status from the latest host observation of this OneFoundry Compose stack.</p>
      {error && <p role="alert">{error}</p>}
      {snapshot && <>
        <p role="status" className={`infra-overall infra-${snapshot.status}`}>Overall: {snapshot.status}</p>
        {snapshot.captured_at && <p className="infra-observed">Observed: <time dateTime={snapshot.captured_at}>
          {new Date(snapshot.captured_at).toLocaleString()}</time></p>}
        {snapshot.reason && <p>{snapshot.reason}</p>}
        <ul aria-label="Compose services" className="infra-services">
          {snapshot.services.map((service) => <li key={service.name}>
            <strong>{service.name}</strong>: {serviceLabel(service)}
          </li>)}
        </ul>
      </>}
    </section>
  );
}
