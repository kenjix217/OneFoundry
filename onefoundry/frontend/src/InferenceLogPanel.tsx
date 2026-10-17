import type { LiveTextOutput, RunDetail } from './workApi';

function savedText(payload: Record<string, unknown> | undefined, key: string): string | null {
  const value = payload?.[key];
  return typeof value === 'string' && value.length <= 200 ? value : null;
}

function elapsed(startedAt: string, finishedAt: string | null): string {
  if (!finishedAt) return 'Awaiting result';
  const milliseconds = Date.parse(finishedAt) - Date.parse(startedAt);
  return Number.isFinite(milliseconds) && milliseconds >= 0
    ? `${(milliseconds / 1000).toFixed(2)} seconds` : 'Unavailable';
}

export function InferenceLogPanel({ run, output }: { run: RunDetail; output: LiveTextOutput | null }) {
  const started = run.events.find((event) => event.run_id === run.id && event.event_type === 'live_text_started');
  const finished = run.events.find((event) => event.run_id === run.id && event.event_type === 'live_text_finished');
  const diagnostic = savedText(finished?.payload, 'diagnostic_code');
  const provider = savedText(started?.payload, 'provider') ?? output?.provider ?? 'Unavailable';
  const model = savedText(started?.payload, 'model') ?? output?.model_id ?? run.profile_model ?? 'Unavailable';
  const effort = savedText(started?.payload, 'reasoning_effort') ?? 'Not set';
  const toolsActive = started?.payload.tools_active;

  return <section className="run-outcome inference-log" aria-label="Inference log">
    <h3>Inference log</h3>
    <p className="quiet-note">Saved outcome, timing, and usage for this model response.</p>
    <div className="run-outcome-grid">

      <div><span>Provider and model</span><strong>{provider} · {model}</strong></div>
      <div><span>Reasoning effort</span><strong>{effort}</strong></div>
      <div><span>OneFoundry role tools</span><strong>{toolsActive === true ? 'Available for this run' :
        toolsActive === false ? 'Inactive for this run' : 'Not recorded'}</strong></div>
      <div><span>Outcome</span><strong>{run.status}</strong></div>
      <div><span>Elapsed</span><strong>{elapsed(run.started_at, run.finished_at)}</strong></div>
      <div><span>Diagnostic</span><strong>{diagnostic?.replaceAll('_', ' ') ?? (run.status === 'done' ? 'None' : 'Not recorded for this attempt')}</strong></div>

      <div><span>Token usage</span><strong>{output ? `${output.input_tokens ?? 'Unknown'} input · ${output.output_tokens ?? 'Unknown'} output` : 'Unavailable'}</strong></div>
    </div>
    {run.status === 'unknown' && <p className="run-outcome-reason">The provider may have completed this call. This record does not confirm delivery, and OneFoundry will not retry it automatically.</p>}
    {toolsActive === false && <p className="run-outcome-reason">This run sent no OneFoundry role tools to the model. Any proposed actions in its text were not executed by this run.</p>}
  </section>;
}
