export interface ObservedAction {
  source: 'runner_observed'; kind: 'command' | 'provider_invocation';
  command: string[]; cwd: '.'; started_at: string; finished_at: string;
  outcome: 'finished' | 'timed_out' | 'done' | 'unknown'; exit_code: number | null;
  stdout: string; stderr: string; output_truncated: boolean;
}

export interface ProviderProgress {
  source: 'cli_output' | 'runner_observed'; kind: 'agent_update' | 'tool_activity' | 'runner_update'; text: string;
  tool: 'shell' | 'file_change' | 'connected_tool' | 'web_search' | 'Read' | 'Edit' | 'Write' | 'Bash' | null;
  command: string | null;
}

export interface SavedActionTrace {
  coverage: 'partial'; provider_tool_calls_captured: false; omitted_actions: number;
  actions: ObservedAction[]; omitted_progress: number; progress: ProviderProgress[];
}

export const AGENT_UPDATE_TEXT = 'The coding agent shared an update. Review its final result for details.';
export const STAGE_BASELINE_FAILED = 'The starting check reported a failure. Next, the coding agent will repair the calculator.';
export const STAGE_CLI_FINISHED = 'The coding agent finished. Next, the runner will inspect the changed file and rerun the test.';
export const STAGE_TEST_PASSED = 'The repaired calculator passed its test. Next, the runner will save the evidence.';
const runnerUpdateTexts = [STAGE_BASELINE_FAILED, STAGE_CLI_FINISHED, STAGE_TEST_PASSED];

const toolText: Record<string, string> = {
  shell: 'Ran a shell command.', file_change: 'Changed project files.',
  connected_tool: 'Used a connected tool.', web_search: 'Searched the web.',
  Read: 'Read a project file.', Edit: 'Requested a project file edit.',
  Write: 'Requested a project file write.', Bash: 'Ran a shell command.',
};
const legacyToolText: Record<string, string> = {
  Edit: 'Edited a project file.', Write: 'Wrote a project file.',
};

export function parseProviderProgress(value: unknown): ProviderProgress {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('Invalid saved progress.');
  const item = value as Record<string, unknown>;
  const tools = ['shell', 'file_change', 'connected_tool', 'web_search', 'Read', 'Edit', 'Write', 'Bash'];
  const commands = ['pwd', 'git status --short', 'git diff --check',
    'python -m unittest discover -s tests -v', 'python3 -m unittest discover -s tests -v', 'pytest -q'];
  if (!['cli_output', 'runner_observed'].includes(String(item.source)) ||
      !['agent_update', 'tool_activity', 'runner_update'].includes(String(item.kind)) ||
      typeof item.text !== 'string' || !item.text.length || item.text.length > 240 ||
      !(item.tool === null || tools.includes(String(item.tool))) ||
      !(item.command === null || commands.includes(String(item.command))) ||
      (item.kind === 'runner_update' && (item.source !== 'runner_observed' ||
        item.tool !== null || item.command !== null || !runnerUpdateTexts.includes(item.text))) ||
      (item.kind === 'agent_update' && (item.source !== 'cli_output' ||
        item.tool !== null || item.command !== null || item.text !== AGENT_UPDATE_TEXT)) ||
      (item.kind === 'tool_activity' && item.tool === null) ||
      (item.kind === 'tool_activity' && item.source !== 'cli_output') ||
      (item.command !== null && item.tool !== 'shell') ||
      (item.kind === 'tool_activity' && item.text !== toolText[String(item.tool)] &&
        item.text !== legacyToolText[String(item.tool)])) {
    throw new Error('Invalid saved progress.');
  }
  if (item.kind === 'tool_activity' && item.text === legacyToolText[String(item.tool)]) {
    return { ...item, text: toolText[String(item.tool)] } as ProviderProgress;
  }
  return item as unknown as ProviderProgress;
}

export function parseSavedActionTrace(text: string): SavedActionTrace {
  const value: unknown = JSON.parse(text);
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('Invalid saved action trace.');
  const row = value as Record<string, unknown>;
  if (row.schema_version !== 1 || row.coverage !== 'partial' || row.provider_tool_calls_captured !== false ||
      !Number.isSafeInteger(row.omitted_actions) || (row.omitted_actions as number) < 0 ||
      !Array.isArray(row.actions) || row.actions.length > 64 ||
      (row.omitted_progress !== undefined && (!Number.isSafeInteger(row.omitted_progress) || (row.omitted_progress as number) < 0)) ||
      (row.progress !== undefined && (!Array.isArray(row.progress) || row.progress.length > 24))) {
    throw new Error('Invalid saved action trace.');
  }
  const actions = row.actions.map((value: unknown): ObservedAction => {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('Invalid saved action.');
    const item = value as Record<string, unknown>;
    if (item.source !== 'runner_observed' || !['command', 'provider_invocation'].includes(String(item.kind)) ||
        !Array.isArray(item.command) || !item.command.length || item.command.length > 32 ||
        item.command.some((part: unknown) => typeof part !== 'string' || part.length > 1000) ||
        item.cwd !== '.' || typeof item.started_at !== 'string' || typeof item.finished_at !== 'string' ||
        !['finished', 'timed_out', 'done', 'unknown'].includes(String(item.outcome)) ||
        !(item.exit_code === null || Number.isSafeInteger(item.exit_code)) ||
        typeof item.stdout !== 'string' || typeof item.stderr !== 'string' ||
        item.stdout.length > 2048 || item.stderr.length > 2048 || typeof item.output_truncated !== 'boolean') {
      throw new Error('Invalid saved action.');
    }
    return item as unknown as ObservedAction;
  });
  const progress = ((row.progress ?? []) as unknown[]).map(parseProviderProgress);
  return { coverage: 'partial', provider_tool_calls_captured: false,
    omitted_actions: row.omitted_actions as number, actions,
    omitted_progress: (row.omitted_progress ?? 0) as number, progress };
}
