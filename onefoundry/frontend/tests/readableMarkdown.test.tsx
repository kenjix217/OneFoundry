import { expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { ReadableMarkdown } from '../src/ReadableMarkdown';

it('presents headings, emphasis, lists, tables and code without executing supplied HTML or links', () => {
  const source = '# DistrictAI report\n\n**Output index**\n\n- First finding\n- Second finding\n\n' +
    '| Cost | Monthly |\n| --- | ---: |\n| Hosting | $42 |\n\n```text\n  keep  spaces\n```\n' +
    '<script>alert(1)</script> [outside](javascript:alert(1)) [site](https://example.com)';
  const { container } = render(<ReadableMarkdown source={source} />);
  expect(screen.getByRole('heading', { name: 'DistrictAI report' })).toBeInTheDocument();
  expect(screen.getByText('Output index').tagName).toBe('STRONG');
  expect(screen.getAllByRole('listitem')).toHaveLength(2);
  expect(within(screen.getByRole('table')).getByRole('columnheader', { name: 'Cost' })).toBeInTheDocument();
  expect(within(screen.getByRole('table')).getByRole('cell', { name: '$42' })).toBeInTheDocument();
  expect(container.querySelector('pre code')?.textContent).toBe('  keep  spaces');
  expect(container.querySelector('script,a,img')).toBeNull();
  expect(container).toHaveTextContent('[site](https://example.com)');
});

it('keeps malformed oversized input plain and preserves code and unknown artifact syntax', () => {
  const malformed = `${'['.repeat(140000)}\n**unclosed [unknown](artifact:${'a'.repeat(64)})`;
  const { container } = render(<ReadableMarkdown source={malformed} />);
  expect(container.querySelector('button,strong')).toBeNull();
  expect(container.textContent).toBe(malformed);
  const known = 'b'.repeat(64);
  const other = render(<ReadableMarkdown source={`\`[code](artifact:${known})\` [unknown](artifact:${known})`}
    reference={() => null} />);
  expect(other.container.querySelector('button')).toBeNull();
  expect(other.container.textContent).toContain(`[code](artifact:${known})`);
  expect(other.container.textContent).toContain(`[unknown](artifact:${known})`);
});
