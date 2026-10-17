import type { WorkTrigger } from './workApi';

const LABELS: Record<string, string> = {
  'email.received': 'New email', 'schedule.fired': 'Scheduled work',
  'work_order.done': 'Completed work', 'work_order.failed': 'Failed work',
  'document.created': 'New document', 'document.changed': 'Document changed',
  'agent.finding': 'Agent finding',
};

export function WorkOrderTrigger({ cause }: { cause: WorkTrigger | null | undefined }) {
  if (!cause) return null;
  return <p className="quiet-note">Triggered by {LABELS[cause.signal_type] ?? cause.signal_type.replaceAll('_', ' ').replaceAll('.', ' ')}
    {cause.rule_name ? ` · ${cause.rule_name} (version ${cause.rule_version})` : ' · Triage'}.</p>;
}
