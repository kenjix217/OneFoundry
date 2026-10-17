import { useState } from 'react';
import { ApiError } from './api';
import { chooseInputFiles, type PendingInputFile } from './inputFileApi';

type Props = { files: PendingInputFile[]; onChange: (files: PendingInputFile[]) => void;
  limit: number; disabled?: boolean; label?: string; status?: string | null };

export function InputFilePicker({ files, onChange, limit, disabled = false, label = 'Attach files', status }: Props) {
  const [error, setError] = useState<string | null>(null);
  return <div className="input-file-picker">
    <label className="field"><span className="field-label">{label}</span>
      <input type="file" multiple disabled={disabled} accept=".txt,.md,.csv,.tsv,.json,.yaml,.yml,.xml,.html,.htm,.pdf,.docx,.xlsx,.pptx,.jpg,.jpeg,.png,.webp,.doc,.xls,.ppt,.rtf,.odt,.ods,.odp"
        onChange={(event) => { try { onChange(chooseInputFiles(event.target.files, limit)); setError(null); }
          catch (caught) { setError(caught instanceof ApiError ? caught.message : 'Could not select files.'); } }} /></label>
    <small>Up to {limit} files, 25 MiB each. Files are saved and processed locally before an agent can use them.</small>
    {files.length > 0 && <ul>{files.map(({ id, file }) => <li key={id}>{file.name} · {(file.size / 1024).toFixed(0)} KiB</li>)}</ul>}
    {status && <p role="status">{status}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
  </div>;
}
