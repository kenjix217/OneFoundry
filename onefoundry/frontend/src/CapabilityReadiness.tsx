import type { CapabilityReport, CapabilityStatus } from './capabilityReadiness';

const labels: Record<CapabilityStatus, string> = {
  ready: 'Preflight ready', disabled: 'Disabled', permission_required: 'Access needed',
  configuration_missing: 'Setup needed', dependency_unavailable: 'Connection unavailable',
  missing_implementation: 'Feature needed', unknown: 'Needs investigation',
};

export function CapabilityReadiness({ report }: { report: CapabilityReport }) {
  return <details><summary>Capability readiness</summary>
    <p>Checks observed on {new Date(report.observed_at).toLocaleString()}. Each capability is checked separately; these results do not start work or grant access.</p>
    <ul>{report.checks.map((check) => <li key={`${check.operation}-${check.source_id ?? 'policy'}`}>
      <strong>{check.title}</strong> · {labels[check.status]}
      {check.source_name && <p>{check.source_name}</p>}
      {check.blockers.length > 0 && <ul>{check.blockers.map((blocker) => <li key={blocker.code}>
        {blocker.message} <span>{blocker.next_step}</span>
      </li>)}</ul>}
    </li>)}</ul>
    {report.omitted_sources > 0 && <p>{report.omitted_sources} source records were not inspected in this bounded view. An omitted source is not a verified capability.</p>}
  </details>;
}
