import { beforeEach, describe, expect, it, vi } from 'vitest';

const fetchWithAuth = vi.fn();
vi.mock('../auth/index.js', () => ({ fetchWithAuth }));
vi.mock('../api/rest.js', () => ({ getUserMe: vi.fn() }));

import { handoffRequest } from '../handoffs.js';

describe('human handoff API transport', () => {
  beforeEach(() => {
    fetchWithAuth.mockReset();
  });

  it('uses the existing authenticated fetch wrapper for customer and staff requests', async () => {
    fetchWithAuth.mockResolvedValue({
      ok: true,
      json: async () => ({ tickets: [] }),
    });
    expect(await handoffRequest('GET', '/api/handoffs')).toEqual({ tickets: [] });
    expect(fetchWithAuth).toHaveBeenCalledWith(
      '/api/handoffs',
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('preserves the client idempotency key and JSON body', async () => {
    fetchWithAuth.mockResolvedValue({
      ok: true,
      json: async () => ({ message_id: 'saved' }),
    });
    await handoffRequest('POST', '/api/handoffs/123/messages', {
      body: '您好',
      client_message_id: 'stable-send-key',
    });
    const [, options] = fetchWithAuth.mock.calls[0];
    expect(JSON.parse(options.body)).toEqual({
      body: '您好',
      client_message_id: 'stable-send-key',
    });
  });

  it('does not treat a failed API reply as successful delivery', async () => {
    fetchWithAuth.mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ detail: '工单已被其他坐席领取' }),
    });
    await expect(handoffRequest('POST', '/api/handoffs/123/claim')).rejects.toThrow(
      '工单已被其他坐席领取',
    );
  });

  it('handles non-JSON failures without exposing a raw exception', async () => {
    fetchWithAuth.mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => {
        throw new Error('upstream body parsing failed');
      },
    });
    await expect(handoffRequest('GET', '/api/handoffs')).rejects.toThrow('HTTP 503');
  });
});
