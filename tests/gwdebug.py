"""Shows the REAL error inside local_runner's gateway call. Run in ~/hack/kit:
    python3 gwdebug.py"""
import json, os, ssl, socket, time, traceback
from pathlib import Path
import local_runner as lr, agentic_simulator as sim, solution
from urllib.parse import urlsplit
URL = 'https://parts-gateway.yadhavan-periyannan.workers.dev/gateway'
tok = os.environ.get('TEAM_GATEWAY_TOKEN') or (Path.home()/'hack'/'.gateway_token').read_text().strip()
print('token length', len(tok))
try:
    p = lr.GatewayProvider(URL, tok)
except Exception as e:
    print('PROVIDER INIT FAILED:', e); raise SystemExit
host = urlsplit(URL).hostname
print('addresses:', sorted({a[4][0] for a in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}))
print('chosen   :', lr._resolve_address(host, 443, 10))
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.load_default_certs()
print('CA certs loaded by runner context:', len(ctx.get_ca_certs()))
a = solution.decide(json.load(open('smoke.json'))[0]['observation'], {})['action']
payload = sim.model_request(a)
raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
deadline = time.monotonic() + 45
addr = lr._resolve_address(host, 443, 45)
conn = lr._LocalHTTPSConnection(host, addr, 443, p._context, deadline)
try:
    conn.request('POST', '/gateway', raw, {'Content-Type': 'application/json', 'Accept': 'application/json',
                 'Authorization': 'Bearer ' + tok, 'Host': host, 'Connection': 'close'})
    r = conn.getresponse(); body = r.read()
    print('HTTP', r.status, body[:200])
    print('reply ok:', sim.model_reply(sim.decode(body)) is not None)
except Exception:
    traceback.print_exc()
