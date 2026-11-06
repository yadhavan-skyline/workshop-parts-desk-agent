// Team gateway v2: races Gemini and Groq in parallel, first good answer wins.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== 'POST' || url.pathname !== '/gateway') return new Response('not found', {status: 404});
    if (!env.GATEWAY_TOKEN || (request.headers.get('Authorization') || '') !== 'Bearer ' + env.GATEWAY_TOKEN)
      return new Response('forbidden', {status: 403});
    let frame;
    try {
      const raw = await request.text();
      if (!raw || raw.length > 65536) throw 0;
      frame = JSON.parse(raw);
      if (Object.keys(frame).sort().join(',') !== 'max_output_tokens,messages') throw 0;
      if (!Array.isArray(frame.messages) || frame.messages.length < 1 || frame.messages.length > 24) throw 0;
      if (!Number.isInteger(frame.max_output_tokens) || frame.max_output_tokens < 16 || frame.max_output_tokens > 2048) throw 0;
    } catch (e) { return new Response('bad request', {status: 400}); }

    const G = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions';
    const Q = 'https://api.groq.com/openai/v1/chat/completions';
    const maxTokens = Math.max(frame.max_output_tokens, 4096);
    const started = Date.now();

    async function call(p, ms) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), ms);
      try {
        const body = {model: p.model, messages: frame.messages, max_tokens: maxTokens};
        if (p.effort) body.reasoning_effort = p.effort;
        const r = await fetch(p.url, {method: 'POST', signal: ctrl.signal, redirect: 'manual',
          headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + p.key},
          body: JSON.stringify(body)});
        if (r.status !== 200) throw new Error('status ' + r.status);
        const j = await r.json();
        const text = j && j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content;
        if (typeof text !== 'string' || !text.trim() || new TextEncoder().encode(text).length > 65536) throw new Error('empty');
        const u = j.usage || {}, usage = {};
        if (Number.isInteger(u.prompt_tokens)) usage.input_tokens = u.prompt_tokens;
        if (Number.isInteger(u.completion_tokens)) usage.output_tokens = u.completion_tokens;
        return {text, usage};
      } finally { clearTimeout(timer); }
    }

    // Round 1: race the two fastest options in parallel.
    const first = [
      {url: G, key: env.GEMINI_KEY, model: 'gemini-3.5-flash', effort: 'low'},
      {url: Q, key: env.GROQ_KEY, model: 'openai/gpt-oss-120b', effort: 'low'},
    ].filter(p => p.key);
    try {
      const out = await Promise.any(first.map(p => call(p, 25000)));
      return Response.json(out);
    } catch (e) {}

    // Round 2: backups one by one, without the reasoning setting.
    const backups = [
      {url: G, key: env.GEMINI_KEY, model: 'gemini-3.5-flash-lite'},
      {url: G, key: env.GEMINI_KEY, model: 'gemini-3.5-flash'},
      {url: Q, key: env.GROQ_KEY, model: 'openai/gpt-oss-120b'},
    ].filter(p => p.key);
    for (const p of backups) {
      const left = 42000 - (Date.now() - started);
      if (left < 3000) break;
      try { return Response.json(await call(p, left)); } catch (e) {}
    }
    return new Response('Team gateway unavailable', {status: 503});
  }
};
