/* ============================================================
   API 层：统一封装后端 REST 与 AI 流式接口
   ============================================================ */
(function (global) {
  'use strict';

  async function request(path, options) {
    const res = await fetch(path, options);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const msg = (data && data.error) || ('请求失败 HTTP ' + res.status);
      throw new Error(msg);
    }
    if (data && data.error) throw new Error(data.error);
    return data;
  }

  function qs(params) {
    const parts = Object.keys(params || {})
      .filter(k => params[k] !== undefined && params[k] !== null && params[k] !== '')
      .map(k => encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
    return parts.length ? '?' + parts.join('&') : '';
  }

  const API = {
    health: () => request('/api/health'),
    search: (q) => request('/api/search' + qs({ q: q })),
    stock: (code) => request('/api/stock' + qs({ code: code })),
    quotes: (codes) => request('/api/quotes' + qs({ codes: (codes || []).join(',') })),
    market: () => request('/api/market'),
    sectors: (kind) => request('/api/sectors' + qs({ kind: kind })),
    strategies: () => request('/api/strategies'),
    screenerStart: (body) => request('/api/screener/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
    }),
    screenerProgress: (jobId) => request('/api/screener/progress' + qs({ job_id: jobId })),
    screenerStop: (jobId) => request('/api/screener/stop', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_id: jobId })
    }),

    /* AI 流式：POST + SSE 帧解析
       opts: { endpoint?: string, signal?: AbortSignal } */
    async aiStream(body, handlers, opts) {
      const h = handlers || {};
      const o = opts || {};
      const res = await fetch(o.endpoint || '/api/ai/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
        signal: o.signal
      });
      if (!res.ok) {
        let msg = 'AI 请求失败 HTTP ' + res.status;
        try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (e) { /* ignore */ }
        throw new Error(msg);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split('\n\n');
        buffer = frames.pop();
        for (const frame of frames) {
          const line = frame.trim();
          if (!line.startsWith('data:')) continue;
          let payload;
          try { payload = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
          if (payload.type === 'meta' && h.onMeta) h.onMeta(payload);
          else if (payload.type === 'thinking' && h.onThinking) h.onThinking(payload.text || '');
          else if (payload.type === 'content' && h.onDelta) h.onDelta(payload.text || '');
          else if (payload.type === 'delta' && h.onDelta) h.onDelta(payload.text || '');
          else if (payload.type === 'error' && h.onError) h.onError(payload.message || '模型返回异常');
          else if (payload.type === 'done' && h.onDone) h.onDone();
        }
      }
    }
  };

  global.API = API;
})(window);
