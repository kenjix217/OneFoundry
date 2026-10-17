import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { ProfileCapabilities } from '../src/ProfileCapabilities';
import { getCoordination } from '../src/coordinationApi';
vi.mock('../src/coordinationApi', () => ({ getCoordination: vi.fn() }));
afterEach(() => vi.resetAllMocks());

it('offers readable capabilities and preserves exact permission values when changed', async () => {
  vi.mocked(getCoordination).mockResolvedValue({ mode: 'off', paused: false, agent_rules_enabled: false, version: 1,
    max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', signal_types: {},
    profile_tool_ceiling: [], registered_tools: ['approved_memory_v1', 'report_signal_v1'] });
  const onChange = vi.fn();
  render(<ProfileCapabilities projectId="project-1" selected={['approved_memory_v1']} disabled={false} onChange={onChange} />);
  expect(await screen.findByRole('checkbox', { name: 'Approved memory' })).toBeChecked();
  await userEvent.click(screen.getByRole('checkbox', { name: 'Report signal' }));
  expect(onChange).toHaveBeenCalledWith(['approved_memory_v1', 'report_signal_v1']);
  expect(screen.queryByText('approved_memory_v1')).not.toBeInTheDocument();
});

it('shows registered human tool labels while granting the exact canonical ID', async () => {
  const canonicalId = `ext_${'a'.repeat(52)}_lookup_`;
  vi.mocked(getCoordination).mockResolvedValue({ mode: 'off', paused: false, agent_rules_enabled: false, version: 1,
    max_chain_depth: 10, max_orders_per_day: 50, max_auto_starts_per_day: 20, timezone: 'UTC', signal_types: {},
    profile_tool_ceiling: [], registered_tools: [canonicalId, 'approved_memory_v1'],
    registered_tool_labels: { [canonicalId]: 'Customer Records · Lookup Customer (customer-records/lookup_customer)' } });
  const onChange = vi.fn();
  render(<ProfileCapabilities projectId="project-1" selected={[]} disabled={false} onChange={onChange} />);
  const choice = await screen.findByRole('checkbox', { name: 'Customer Records · Lookup Customer (customer-records/lookup_customer)' }) as HTMLInputElement;
  expect(canonicalId).toHaveLength(64);
  expect(choice.value).toBe(canonicalId);
  expect(screen.getByRole('checkbox', { name: 'Approved memory' })).toBeInTheDocument();
  await userEvent.click(choice);
  expect(onChange).toHaveBeenCalledWith([canonicalId]);
  expect(screen.queryByText(canonicalId)).not.toBeInTheDocument();
});

it('retains saved permissions when the registry fails and allows retry', async () => {
  vi.mocked(getCoordination).mockRejectedValue(new Error('unavailable'));
  const onChange = vi.fn();
  render(<ProfileCapabilities projectId="project-1" selected={['approved_memory_v1']} disabled={false} onChange={onChange} />);
  expect(await screen.findByText(/Your saved choices are retained/)).toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: 'Approved memory' })).toBeChecked();
  expect(onChange).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: 'Try loading capabilities again' }));
  expect(getCoordination).toHaveBeenCalledTimes(2);
});
