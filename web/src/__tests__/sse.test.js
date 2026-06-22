import { sendChatStream } from '../api/sse.js';
import { fetchWithAuth } from '../auth/index.js';

vi.mock('../auth/index.js', () => ({
  fetchWithAuth: vi.fn(),
}));

function makeReader(chunks) {
  const queue = [...chunks];
  return {
    read: vi.fn().mockImplementation(async () => {
      if (!queue.length) return { done: true, value: undefined };
      return queue.shift();
    }),
  };
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('SSE client', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('reports an error when stream closes without a done event', async () => {
    const encoder = new TextEncoder();
    const onChunk = vi.fn();
    const onError = vi.fn();
    const onDone = vi.fn();

    fetchWithAuth.mockResolvedValue({
      ok: true,
      body: {
        getReader: () =>
          makeReader([
            {
              done: false,
              value: encoder.encode('data: {"type":"chunk","content":"你好"}\n\n'),
            },
          ]),
      },
    });

    sendChatStream('你好', 'sid-1', 'token-1', { onChunk, onError, onDone });
    await flushPromises();

    expect(onChunk).toHaveBeenCalledWith('你好');
    expect(onDone).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('done'));
  });

  it('parses a final done event even without trailing separator', async () => {
    const encoder = new TextEncoder();
    const onDone = vi.fn();
    const onError = vi.fn();

    fetchWithAuth.mockResolvedValue({
      ok: true,
      body: {
        getReader: () =>
          makeReader([
            {
              done: false,
              value: encoder.encode(
                'data: {"type":"done","content":"ok","session_id":"sid-2","session_token":"token-2"}',
              ),
            },
          ]),
      },
    });

    sendChatStream('你好', 'sid-2', 'token-2', { onDone, onError });
    await flushPromises();

    expect(onDone).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'done', content: 'ok', session_id: 'sid-2' }),
    );
    expect(onError).not.toHaveBeenCalled();
  });

  it('dispatches content_complete before done', async () => {
    const encoder = new TextEncoder();
    const onContentComplete = vi.fn();
    const onDone = vi.fn();

    fetchWithAuth.mockResolvedValue({
      ok: true,
      body: {
        getReader: () =>
          makeReader([
            {
              done: false,
              value: encoder.encode(
                'data: {"type":"content_complete","content":"已完成正文"}\n\ndata: {"type":"done","content":"已完成正文","session_id":"sid-3"}\n\n',
              ),
            },
          ]),
      },
    });

    sendChatStream('你好', 'sid-3', 'token-3', { onContentComplete, onDone });
    await flushPromises();

    expect(onContentComplete).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'content_complete', content: '已完成正文' }),
    );
    expect(onDone).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'done', content: '已完成正文', session_id: 'sid-3' }),
    );
  });
});
