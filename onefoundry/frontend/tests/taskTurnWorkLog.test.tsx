import { expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { ConversationTurn } from '../src/conversationApi';
import { TaskTurnWorkLog } from '../src/TaskTurnWorkLog';

const saved = {
  status: 'done', source_kind: 'operator_ui', created_at: '2026-10-03T12:00:00Z',
  finished_at: '2026-10-03T12:00:04Z', assistant_text: 'Plan: research. Finding: claim needs proof. Next: review.',
  selected_skills: [{ skill_name: 'Claims review' }], used_sections: [{ source_label: 'Product brief' }],
  input_sections_omitted: 0, automation_decision: { status: 'saved', decision: 'assign_team',
    assignments: [{ id: 'child' }] },
  progress_updates: ['I will review the assignment.', 'The child task is saved.'],
} as ConversationTurn;

it('shows saved inputs and assignment without fabricating intermediate reasoning', async () => {
  const user = userEvent.setup();
  render(<TaskTurnWorkLog turn={saved} />);
  await user.click(screen.getByText('Work log · Saved reply'));
  expect(screen.getByText(/Request saved/)).toBeInTheDocument();
  expect(screen.getByText(/Procedures selected/)).toBeInTheDocument();
  expect(screen.getByText(/File inputs used/)).toBeInTheDocument();
  expect(screen.getByText(/1 agent assignment/)).toBeInTheDocument();
  expect(screen.getByText(/I will review the assignment/)).toBeInTheDocument();
  expect(screen.getByText(/Private reasoning is never shown/)).toBeInTheDocument();
});

it('shows an unknown reply without a completion claim', async () => {
  const user = userEvent.setup();
  render(<TaskTurnWorkLog turn={{ ...saved, status: 'unknown', assistant_text: null,
    finished_at: null, automation_decision: null }} />);
  await user.click(screen.getByText('Work log · Outcome unknown'));
  expect(screen.getByText(/No saved agent reply/)).toBeInTheDocument();
  expect(screen.queryByText(/Agent reply saved/)).not.toBeInTheDocument();
});
