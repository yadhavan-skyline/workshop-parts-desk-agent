"""Gateway speed test with the REAL app prompt. Run inside ~/hack/kit:
    python3 gwtest.py
Reads the token from TEAM_GATEWAY_TOKEN or the file ~/hack/.gateway_token"""
import http.client, json, os, time
from pathlib import Path
import solution
tok = os.environ.get('TEAM_GATEWAY_TOKEN') or (Path.home()/'hack'/'.gateway_token').read_text().strip()
import sys
HOST = sys.argv[1] if len(sys.argv) > 1 else 'parts-gateway.yadhavan-periyannan.workers.dev'
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 1
for i, rec in [x for _ in range(ROUNDS) for x in enumerate(json.load(open('smoke.json')))]:
    a = solution.decide(rec['observation'], {})['action']
    body = json.dumps({'messages': a['messages'], 'max_output_tokens': a['max_output_tokens']})
    t = time.time()
    try:
        c = http.client.HTTPSConnection(HOST, timeout=60)
        c.request('POST', '/gateway', body, {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tok})
        r = c.getresponse(); raw = r.read()
        print('case %d: HTTP %d in %.1f s' % (i, r.status, time.time() - t))
        if r.status == 200:
            text = json.loads(raw)['text']
            ok = solution.parse_llm(text) is not None
            print('   model JSON parsed:', ok, '|', text[:150].replace('\n', ' '))
        else:
            print('   ', raw[:200])
    except Exception as e:
        print('case %d: FAILED after %.1f s: %s' % (i, time.time() - t, e))
