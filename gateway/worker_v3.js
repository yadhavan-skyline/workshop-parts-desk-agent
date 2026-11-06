// Team gateway v3: rate-limit resistant, ordered by free daily quota. Races two primary models, then falls back
// through many separate free-tier model quotas (each model has its own limit).
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === 'GET' && url.pathname === '/health') {
      const k = (v, pre) => !v ? 'MISSING' : (v.trim() !== v ? 'HAS SPACES' : (v.includes('=') ? 'HAS = SIGN (pasted whole line?)' : (pre && !v.startsWith(pre) ? 'unexpected start: ' + v.slice(0, 4) + '...' : 'looks ok, length ' + v.length)));
      return Response.json({GEMINI_KEY: k(env.GEMINI_KEY, ''), GROQ_KEY: k(env.GROQ_KEY, 'gsk_'), GATEWAY_TOKEN: k(env.GATEWAY_TOKEN, '')});
    }
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
    const maxTokens = frame.max_output_tokens;          // no inflation: keeps Groq token-per-minute use low
    const started = Date.now();
    const DEADLINE = 41000;

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
    const left = () => DEADLINE - (Date.now() - started);
    const errors = [];

    // Round 1: two independent providers in parallel; first good answer wins.
    const first = [
      {url: Q, key: env.GROQ_KEY, model: 'openai/gpt-oss-120b', effort: 'low'},   // ~1000/day
      {url: G, key: env.GEMINI_KEY, model: 'gemini-3.5-flash-lite'},             // 500/day
    ].filter(p => p.key);
    try {
      return Response.json(await Promise.any(first.map(p => call(p, 15000).catch(err => { errors.push(p.model + ': ' + err.message); throw err; }))));
    } catch (e) {}

    // Round 2: separate quotas, one at a time (rate-limited ones fail fast).
    const backups = [
      {url: G, key: env.GEMINI_KEY, model: 'gemini-3.1-flash-lite'},
      {url: Q, key: env.GROQ_KEY, model: 'openai/gpt-oss-20b', effort: 'low'},
      {url: Q, key: env.GROQ_KEY, model: 'qwen/qwen3.8-27b'},
      {url: G, key: env.GEMINI_KEY, model: 'gemini-flash-lite-latest'},
      {url: G, key: env.GEMINI_KEY, model: 'gemini-3.5-flash'},     // only 20/day on free tier
      {url: G, key: env.GEMINI_KEY, model: 'gemini-3.8-flash'},     // only 20/day on free tier
    ].filter(p => p.key);
    for (const p of backups) {
      const ms = Math.min(12000, left());
      if (ms < 2500) break;
      try { return Response.json(await call(p, ms)); } catch (e) { errors.push(p.model + ': ' + e.message); }
    }
    return new Response('Team gateway unavailable | ' + (errors.join('; ') || 'no API keys configured'), {status: 503});
  }
};
