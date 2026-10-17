// @vitest-environment node
import { describe, expect, it } from 'vitest';

import { localApiTarget } from '../vite.config';

describe('local development API target', () => {
  it('uses the documented loopback default', () => {
    expect(localApiTarget(undefined)).toBe('http://127.0.0.1:8765');
  });

  it('accepts an explicit local port', () => {
    expect(localApiTarget('http://localhost:9000')).toBe('http://localhost:9000');
  });

  it.each([
    'https://127.0.0.1:8765',
    'http://example.com:8765',
    'http://127.0.0.1',
    'http://127.0.0.1:8765/api',
    'http://user:pass@127.0.0.1:8765',
  ])('rejects an invalid proxy target %s', (target) => {
    expect(() => localApiTarget(target)).toThrow('ONEFOUNDRY_API_PROXY_TARGET');
  });
});
