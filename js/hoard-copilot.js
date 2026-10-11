/** Thin fetch-compatible transport. Agent, memory and history live in Faustus. */
export function createCopilotFetch({ hubUrl, token, fetch: transport = globalThis.fetch.bind(globalThis) }) {
  return async (input, init = {}) => {
    const request = new Request(input, init);
    const url = new URL(request.url);
    const envelope = { path: url.pathname + url.search, method: request.method, headers: {} };
    for (const name of ['X-Faustus-Client-Version', 'X-Odysseus-Run-Id']) {
      const value = request.headers.get(name);
      if (value) envelope.headers[name] = value;
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      const bytes = new Uint8Array(await request.arrayBuffer());
      const type = request.headers.get('Content-Type') || '';
      if (type.startsWith('application/json')) envelope.body = JSON.parse(new TextDecoder().decode(bytes));
      else {
        envelope.content_type = type;
        envelope.raw_body = typeof Buffer !== 'undefined'
          ? Buffer.from(bytes).toString('base64')
          : btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(''));
      }
    }
    return transport(hubUrl.replace(/\/$/, '') + '/api/link/copilot', {
      method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(envelope), signal: request.signal,
    });
  };
}
