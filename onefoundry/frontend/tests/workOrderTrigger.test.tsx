import { expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { WorkOrderTrigger } from '../src/WorkOrderTrigger';

it('shows a readable saved cause and rule version without starting work', () => {
  render(<WorkOrderTrigger cause={{ signal_id: 's', signal_type: 'email.received', source: 'email',
    observed_at: '2026-10-04T00:00:00Z', rule_name: 'Customer reply', rule_version: 3 }} />);
  expect(screen.getByText('Triggered by New email · Customer reply (version 3).')).toBeInTheDocument();
});

it('leaves manual orders without an invented trigger', () => {
  const { container } = render(<WorkOrderTrigger cause={null} />);
  expect(container).toBeEmptyDOMElement();
});
