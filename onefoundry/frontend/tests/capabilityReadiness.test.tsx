import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { CapabilityReadiness } from '../src/CapabilityReadiness';
import { capabilityReport, type CapabilityReport } from '../src/capabilityReadiness';

const report: CapabilityReport = {
  project_id: 'project', profile_id: 'profile', observed_at: '2026-10-12T12:00:00Z',
  boundary: 'Preflight is not execution', omitted_sources: 1,
  checks: [{ operation: 'repository_read', title: 'Read selected repository', owner: 'source_store',
    source_id: null, revision: null, status: 'configuration_missing', next_action: 'configure',
    blockers: [{ kind: 'configuration_missing', code: 'source_unavailable', owner: 'source_store',
      message: 'Selected repository is unavailable.', next_step: 'Mount the exact repository read-only.' }] }],
};

describe('actionable capability readiness', () => {
  it('shows every concrete blocker without claiming feature work was started', () => {
    render(<CapabilityReadiness report={report} />);
    expect(screen.getByText('Capability readiness')).toBeTruthy();
    expect(screen.getByText('Read selected repository')).toBeTruthy();
    expect(screen.getByText('Mount the exact repository read-only.')).toBeTruthy();
    expect(screen.getByText(/source records were not inspected/)).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });
  it('validates scope and rejects malformed or contradictory observations', () => {
    expect(capabilityReport(report, 'project')).toEqual(report);
    for (const input of [null, { ...report, project_id: 'foreign' }, { ...report, observed_at: 'today' },
      { ...report, omitted_sources: -1 }, { ...report, checks: [{ ...report.checks[0], status: 'ready' }] },
      { ...report, checks: [{ ...report.checks[0], next_action: 'implement' }] },
      { ...report, checks: [{ ...report.checks[0], blockers: [null] }] }]) {
      expect(() => capabilityReport(input, 'project')).toThrow('Capability readiness could not be confirmed');
    }
  });
});
