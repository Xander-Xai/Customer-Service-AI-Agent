import { beforeEach, describe, expect, it, vi } from 'vitest';

const { fetchWithAuth } = vi.hoisted(() => ({ fetchWithAuth: vi.fn() }));
vi.mock('../auth/index.js', () => ({ fetchWithAuth }));
vi.mock('../api/rest.js', () => ({ getUserMe: vi.fn() }));

import { handoffRequest, renderMessages, selectTicketFromList } from '../handoffs.js';

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

describe('message rendering safety', () => {
  it('renders customer-supplied markup as text, not as executable HTML', () => {
    document.body.innerHTML = '<ul id="messageList"></ul>';
    renderMessages([
      {
        sender_role: 'customer',
        body: '<img src=x onerror=alert(1)>',
        created_at: '2026-11-24',
      },
    ]);
    const list = document.querySelector('#messageList');
    expect(list?.querySelector('img')).toBeNull();
    expect(list?.textContent).toContain('<img src=x onerror=alert(1)>');
    document.body.replaceChildren();
  });
});

describe('ticket selection isolation', () => {
  it('serializes selection and clears drafts only when changing tickets', async () => {
    document.body.innerHTML = `
      <ul id="ticketList"></ul><h2 id="ticketTitle"></h2><p id="ticketStatus"></p>
      <p id="ticketReason"></p><p id="ticketResolution"></p><ul id="messageList"></ul>
      <button id="claimTicketBtn"></button><button id="closeTicketBtn"></button>
      <button id="reopenTicketBtn"></button><form id="messageForm"></form>
      <textarea id="messageBody"></textarea><textarea id="resolutionText"></textarea>
      <section id="resolutionPanel"></section><p id="operationStatus"></p>
    `;
    const reply = (id) => ({
      ok: true,
      json: async () => ({ ticket_id: id, status: 'CLAIMED' }),
    });
    const messages = { ok: true, json: async () => ({ messages: [] }) };
    fetchWithAuth.mockReset();
    let release;
    fetchWithAuth.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    fetchWithAuth.mockResolvedValueOnce(messages);
    try {
      const first = selectTicketFromList('A');
      await selectTicketFromList('B');
      expect(fetchWithAuth).toHaveBeenCalledTimes(1);
      release(reply('A'));
      await first;
      expect(document.getElementById('ticketTitle').textContent).toContain('A');

      document.getElementById('messageBody').value = 'only for A';
      document.getElementById('resolutionText').value = 'resolution for A';
      fetchWithAuth.mockResolvedValueOnce(reply('A')).mockResolvedValueOnce(messages);
      await selectTicketFromList('A');
      expect(document.getElementById('messageBody').value).toBe('only for A');

      fetchWithAuth.mockResolvedValueOnce(reply('B')).mockResolvedValueOnce(messages);
      await selectTicketFromList('B');
      expect(document.getElementById('ticketTitle').textContent).toContain('B');
      expect(document.getElementById('messageBody').value).toBe('');
      expect(document.getElementById('resolutionText').value).toBe('');
    } finally {
      document.body.replaceChildren();
    }
  });
});
