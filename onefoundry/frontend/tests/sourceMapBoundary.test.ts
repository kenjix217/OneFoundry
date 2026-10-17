import { SourceMapConsumer, type RawSourceMap } from 'source-map-js';
import { describe, expect, it } from 'vitest';

describe('build source-map boundary', () => {
  it('rejects an indexed map that would expand beyond the supported line bound', () => {
    // CVE-2026-93749: keep the transitive PostCSS consumer on its patched contract.
    const indexed = {
      version: 3,
      sections: [{
        offset: { line: 10_000_001, column: 0 },
        map: { version: 3, sources: ['input.js'], names: [], mappings: 'AAAA' },
      }],
    };
    // The dependency's public types describe flat maps; its runtime also accepts indexed maps.
    expect(() => new SourceMapConsumer(indexed as unknown as RawSourceMap)).toThrow();
  });
});
