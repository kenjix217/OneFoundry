import type { ReactNode } from 'react';

type Props = { source: string; reference?: (label: string, token: string, key: string) => ReactNode | null };

// Saved text artifacts are capped at 64 KiB. Larger chat messages remain visible as plain text.
const MAX_FORMATTED_LENGTH = 131072;
const MAX_TABLE_COLUMNS = 12;
// eslint-disable-next-line no-useless-escape -- Explicit bracket escapes keep this bounded label class readable.
const INLINE = /(`[^`\n]{0,500}`|\*\*[^*\n]{1,500}\*\*|\[[^\]\[\n]{1,120}\]\(artifact:(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\))/g;
// eslint-disable-next-line no-useless-escape -- Match the same bounded label syntax as INLINE.
const ARTIFACT = /^\[([^\]\[\n]{1,120})\]\(artifact:([0-9a-fA-F-]+)\)$/;

function inline(source: string, key: string, reference?: Props['reference'], depth = 0): ReactNode[] {
  const parts: ReactNode[] = [];
  let offset = 0;
  INLINE.lastIndex = 0;
  // Match boundaries are bounded, so malformed punctuation cannot make parsing quadratic.
  for (const match of source.matchAll(INLINE)) {
    const index = match.index;
    const raw = match[0];
    if (index > offset) parts.push(source.slice(offset, index));
    if (raw.startsWith('`')) parts.push(<code key={`${key}:${index}`}>{raw.slice(1, -1)}</code>);
    else if (raw.startsWith('**')) parts.push(<strong key={`${key}:${index}`}>{depth < 2
      ? inline(raw.slice(2, -2), `${key}:${index}`, reference, depth + 1) : raw.slice(2, -2)}</strong>);
    else {
      const artifact = ARTIFACT.exec(raw);
      const linked = artifact && source[index - 1] !== '['
        ? reference?.(artifact[1]!, artifact[2]!.toLowerCase(), `${key}:${index}`) : null;
      parts.push(linked ?? raw);
    }
    offset = index + raw.length;
  }
  if (offset < source.length) parts.push(source.slice(offset));
  return parts;
}

function cells(line: string): string[] {
  const trimmed = line.trim();
  const inner = trimmed.startsWith('|') ? trimmed.slice(1) : trimmed;
  return (inner.endsWith('|') ? inner.slice(0, -1) : inner).split('|').map((cell) => cell.trim());
}

function tableAt(lines: string[], index: number): { rows: string[][]; end: number } | null {
  if (index + 1 >= lines.length || !lines[index]!.includes('|')) return null;
  const head = cells(lines[index]!);
  const divider = cells(lines[index + 1]!);
  if (head.length < 2 || head.length > MAX_TABLE_COLUMNS || divider.length !== head.length ||
    !divider.every((cell) => /^:?-{3,}:?$/.test(cell))) return null;
  const rows = [head];
  let end = index + 2;
  while (end < lines.length && lines[end]!.includes('|') && lines[end]!.trim()) {
    const row = cells(lines[end]!);
    if (row.length !== head.length) break;
    rows.push(row);
    end++;
  }
  return { rows, end };
}

function blockStart(lines: string[], index: number): boolean {
  const line = lines[index] ?? '';
  return /^\s*$|^ {0,3}#{1,4} |^ {0,3}```|^ {0,3}[-*] |^ {0,3}\d{1,3}\. /.test(line) || !!tableAt(lines, index);
}

export function ReadableMarkdown({ source, reference }: Props) {
  if (source.length > MAX_FORMATTED_LENGTH) return <div className="readable-markdown readable-markdown-plain">{source}</div>;
  const lines = source.replace(/\r\n/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index]!;
    if (!line.trim()) { index++; continue; }
    const key = String(index);
    const fence = /^ {0,3}```([^`]*)$/.exec(line);
    if (fence) {
      const start = ++index;
      while (index < lines.length && !/^ {0,3}```\s*$/.test(lines[index]!)) index++;
      blocks.push(<pre key={key}><code>{lines.slice(start, index).join('\n')}</code></pre>);
      if (index < lines.length) index++;
      continue;
    }
    const heading = /^ {0,3}(#{1,4})\s+(.+)$/.exec(line);
    if (heading) {
      const content = inline(heading[2]!, key, reference);
      blocks.push(heading[1]!.length === 1 ? <h2 key={key}>{content}</h2> :
        heading[1]!.length === 2 ? <h3 key={key}>{content}</h3> : <h4 key={key}>{content}</h4>);
      index++;
      continue;
    }
    const table = tableAt(lines, index);
    if (table) {
      blocks.push(<div className="readable-table-scroll" key={key}><table><thead><tr>{table.rows[0]!.map((cell, column) =>
        <th scope="col" key={column}>{inline(cell, `${key}:h${column}`, reference)}</th>)}</tr></thead><tbody>
        {table.rows.slice(1).map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, column) =>
          <td key={column}>{inline(cell, `${key}:${rowIndex}:${column}`, reference)}</td>)}</tr>)}</tbody></table></div>);
      index = table.end;
      continue;
    }
    const item = /^ {0,3}([-*]|\d{1,3}\.)\s+(.+)$/.exec(line);
    if (item) {
      const ordered = item[1]!.endsWith('.');
      const items: ReactNode[] = [];
      while (index < lines.length) {
        const next = /^ {0,3}([-*]|\d{1,3}\.)\s+(.+)$/.exec(lines[index]!);
        if (!next || next[1]!.endsWith('.') !== ordered) break;
        items.push(<li key={index}>{inline(next[2]!, String(index), reference)}</li>);
        index++;
      }
      blocks.push(ordered ? <ol key={key}>{items}</ol> : <ul key={key}>{items}</ul>);
      continue;
    }
    const paragraph: string[] = [line.trim()];
    index++;
    while (index < lines.length && !blockStart(lines, index)) paragraph.push(lines[index++]!.trim());
    blocks.push(<p key={key}>{inline(paragraph.join(' '), key, reference)}</p>);
  }
  return <div className="readable-markdown">{blocks}</div>;
}
