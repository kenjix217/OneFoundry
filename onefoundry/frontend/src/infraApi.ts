import { ApiError, request } from './api';

export type InfraState = 'running' | 'stopped' | 'missing' | 'unknown';
export type InfraHealth = 'healthy' | 'unhealthy' | 'starting' | 'none' | 'unknown';
export type InfraStatus = 'healthy' | 'degraded' | 'unknown';

export interface InfraService {
  name: string;
  state: InfraState;
  health: InfraHealth;
}

export interface InfraSnapshot {
  status: InfraStatus;
  captured_at: string | null;
  services: InfraService[];
  reason: string | null;
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new ApiError('Invalid Infra status response.', null);
  }
  return value as Record<string, unknown>;
}

export async function getInfraStatus(signal?: AbortSignal): Promise<InfraSnapshot> {
  const body = object(await request('/api/infra/status', { signal }));
  if (!['healthy', 'degraded', 'unknown'].includes(String(body.status)) ||
      !Array.isArray(body.services) || body.services.length > 16 ||
      !(body.captured_at === null || typeof body.captured_at === 'string') ||
      !(body.reason === null || typeof body.reason === 'string')) {
    throw new ApiError('Invalid Infra status response.', null);
  }
  const services = body.services.map((entry: unknown): InfraService => {
    const item = object(entry);
    if (typeof item.name !== 'string' || !/^[a-z][a-z0-9_-]{0,63}$/.test(item.name) ||
        !['running', 'stopped', 'missing', 'unknown'].includes(String(item.state)) ||
        !['healthy', 'unhealthy', 'starting', 'none', 'unknown'].includes(String(item.health))) {
      throw new ApiError('Invalid Infra status response.', null);
    }
    return item as unknown as InfraService;
  });
  return { status: body.status as InfraStatus, captured_at: body.captured_at as string | null,
    services, reason: body.reason as string | null };
}
