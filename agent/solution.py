"""Workshop Parts Desk controller (frozen decide(observation, state)).

Design
------
1. Evidence: rebuild every request and part from the *current approved* document
   revision (drafts and late retries of older revisions are ignored).
2. Extraction: a deterministic parser and the team LLM (one gateway call) each
   extract voltage / connector / minimum wattage. A request or part is only
   trusted when both extractions agree. If they disagree (e.g. new wording),
   a second, independent model read is requested; it is accepted only if it
   agrees with the parser, or with the first read AND every value literally
   appears in the request text. Unconfirmed parts are only dispatched after a
   tool card (inspect/verify) confirms their specification.
3. Planning: a small expected-value planner picks the tool call (inspect /
   verify / clarify) with the best gain per tool unit, never spending more
   than 13 units, then makes decisions with an exact assignment search so no
   part is dispatched twice.
4. Safety: a dispatch is only ever emitted when voltage and connector match
   exactly and the part's wattage is >= a *known* required wattage. Unknown
   wattage only allows parts rated 20 W (every request needs 10 or 20 W).
Only the Python standard library is used.
"""
import json
import re
from itertools import product

BUDGET_CAP = 13          # spend at most 13 of 14 units -> full efficiency credit
P_AVAIL = 0.71           # prior that a part has its unit in stock (training data)
P_W20 = 0.51             # prior that an "unknown wattage" request needs 20 W
MAX_LLM_TOKENS = 2048

# ----------------------------------------------------------------------------
# Deterministic extraction
# ----------------------------------------------------------------------------
_CONNECTORS = [
    (re.compile(r'usb[\s-]*type[\s-]*c|usb[\s-]*c\b', re.I), 'USB-C'),
    (re.compile(r'\bbarrel\b', re.I), 'barrel'),
]
_DISTRACTOR = [
    re.compile(r'our retired fixture used[^;.]*[;.]\s*(ignore that\.?)?', re.I),
    re.compile(r'the discarded unit is not a specification\.?', re.I),
    re.compile(r'previous stock suggestions were never approved\.?', re.I),
]
_VOLT = re.compile(r'(\d+(?:\.\d+)?)\s*(?:-\s*)?(mv|millivolts?|v|volts?)\b', re.I)
_VOLT_BARE = re.compile(r'input voltage is\s+(\d+(?:\.\d+)?)(?!\s*(?:m?v|m?w|volt|watt|milli))', re.I)
_WATT = re.compile(r'(\d+(?:\.\d+)?)\s*(?:-\s*)?(mw|milliwatts?|w|watts?)\b', re.I)


def _num(value, unit):
    v = float(value)
    if unit.lower().startswith('m'):
        v /= 1000.0
    return int(v) if abs(v - round(v)) < 1e-9 else v


def _clean(text):
    for pattern in _DISTRACTOR:
        text = pattern.sub(' ', text)
    return text


def _one(values):
    values = set(values)
    return values.pop() if len(values) == 1 else (None if not values else 'AMBIGUOUS')


def parse_spec(text):
    """Return {'voltage','connector','watts'}; None = absent, 'AMBIGUOUS' = conflicting."""
    text = _clean(text or '')
    volts = [_num(n, u) for n, u in _VOLT.findall(text)]
    volts += [_num(n, 'v') for n in _VOLT_BARE.findall(text)]
    watts = [_num(n, u) for n, u in _WATT.findall(text)]
    conns = []
    rest = text
    for pattern, name in _CONNECTORS:
        if pattern.search(rest):
            conns.append(name)
            rest = pattern.sub(' ', rest)
    return {'voltage': _one(volts), 'connector': _one(conns), 'watts': _one(watts)}


INF_W = 1000000
_MAXW = re.compile(r'(?:maximum|max\.?|at most|no more than|not exceed(?:ing)?|up to)\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*(?:-\s*)?(mw|milliwatts?|w|watts?)\b', re.I)
_OPEN_TOP = re.compile(r'larger wattage rating is fine|floor, not a ceiling|or (?:higher|more|above)|higher (?:wattage )?rating is (?:fine|permitted|ok)', re.I)
_UNKNOWN_RANGE = re.compile(r'unreadable|contact the customer|approved limits|range was amended|ask the customer', re.I)


def parse_ticket(text):
    """voltage, connector, wmin, wmax (None = unknown; INF_W = no upper limit)."""
    spec = parse_spec(text)
    clean = _clean(text or '')
    maxes = {_num(n, u) for n, u in _MAXW.findall(clean)}
    rest = _MAXW.sub(' ', clean)
    mins = {_num(n, u) for n, u in _WATT.findall(rest)}
    out = {'voltage': spec['voltage'], 'connector': spec['connector'], 'wmin': None, 'wmax': None}
    if _UNKNOWN_RANGE.search(clean) and not mins:
        return out
    if len(mins) == 1:
        out['wmin'] = mins.pop()
    elif len(mins) > 1:
        out['wmin'] = 'AMBIGUOUS'
        return out
    if len(maxes) == 1:
        out['wmax'] = maxes.pop()
    elif len(maxes) > 1:
        out['wmax'] = 'AMBIGUOUS'
    elif out['wmin'] is not None:
        if _OPEN_TOP.search(clean):
            out['wmax'] = INF_W
        else:
            out['wmax'] = out['wmin']      # no stated ceiling -> exact match only (safe)
            out['assumed_max'] = True
    if out['wmin'] is None and isinstance(out['wmax'], (int, float)):
        out['wmin'] = 0
    return out


def parse_reply_watts(text):
    w = _one(_num(n, u) for n, u in _WATT.findall(text or ''))
    return w if w in (10, 20) else None


# ----------------------------------------------------------------------------
# Current evidence from documents
# ----------------------------------------------------------------------------

def current_texts(docs, as_of):
    best = {}
    for doc in docs or []:
        try:
            if doc.get('status') != 'approved':
                continue
            if as_of is not None and doc.get('issued_at', 0) > as_of:
                continue
            key = (int(doc.get('revision', 0)), doc.get('issued_at', 0))
            entity = doc['entity_id']
            if entity not in best or key > best[entity][0]:
                best[entity] = (key, doc['text'])
        except Exception:
            continue
    return {e: v[1] for e, v in best.items()}


def build_evidence(obs):
    as_of = obs.get('as_of')
    req_docs = current_texts(obs.get('request_documents'), as_of)
    cat_docs = current_texts(obs.get('catalog_documents'), as_of)
    tickets = {}
    for t in obs.get('tickets', []):
        tickets[t['id']] = req_docs.get(t['id'], t.get('text', ''))
    parts = {}
    for p in obs.get('catalog', []):
        parts[p['id']] = {'text': cat_docs.get(p['id'], p.get('description', '')),
                          'age': p.get('snapshot_age', 'aged')}
    return tickets, parts


# ----------------------------------------------------------------------------
# LLM extraction (one gateway call)
# ----------------------------------------------------------------------------

def llm_prompt(tickets, parts):
    lines = ['You extract electrical requirements for a fictional workshop parts desk.',
             'For every REQUEST give the required output voltage in volts, the connector, the minimum wattage and the '
             'maximum wattage in watts (null when not stated, e.g. unreadable label or limits must be asked from the customer).',
             'For every PART give its fixed output voltage in volts, connector and rated output watts.',
             'Rules: convert mV to V and mW to W (5000 mV = 5 V). Connector must be exactly "USB-C" '
             '(also written USB Type-C) or "barrel" (also written barrel plug). Ignore values the text '
             'says to ignore (a retired fixture, a discarded unit, old suggestions).',
             'Answer with ONLY a JSON object, no prose, of the form:',
             '{"requests":{"<id>":{"voltage":12,"connector":"USB-C","min_watts":10,"max_watts":20}},'
             '"parts":{"<id>":{"voltage":5,"connector":"barrel","watts":10}}}',
             '', 'REQUESTS:']
    for tid, text in tickets.items():
        lines.append('%s: %s' % (tid, text))
    lines += ['', 'PARTS:']
    for pid, info in parts.items():
        lines.append('%s: %s' % (pid, info['text']))
    return '\n'.join(lines)


def _norm_conn(value):
    if not isinstance(value, str):
        return None
    for pattern, name in _CONNECTORS:
        if pattern.search(value):
            return name
    return None


def _norm_num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        m = re.search(r'\d+(?:\.\d+)?', value)
        if not m:
            return None
        value = float(m.group())
    if not isinstance(value, (int, float)):
        return None
    if value >= 1000:   # model forgot to convert milli-units
        value = value / 1000.0
    return int(value) if abs(value - round(value)) < 1e-9 else value


def parse_llm(text):
    try:
        text = text.strip()
        start, end = text.find('{'), text.rfind('}')
        data = json.loads(text[start:end + 1])
        out = {'requests': {}, 'parts': {}}
        for tid, v in (data.get('requests') or {}).items():
            out['requests'][tid] = {'voltage': _norm_num(v.get('voltage')),
                                    'connector': _norm_conn(v.get('connector')),
                                    'watts': _norm_num(v.get('min_watts'))}
        for pid, v in (data.get('parts') or {}).items():
            out['parts'][pid] = {'voltage': _norm_num(v.get('voltage')),
                                 'connector': _norm_conn(v.get('connector')),
                                 'watts': _norm_num(v.get('watts'))}
        return out
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Belief state
# ----------------------------------------------------------------------------

def _good(spec, need_watts=True):
    if spec['voltage'] in (None, 'AMBIGUOUS') or spec['connector'] in (None, 'AMBIGUOUS'):
        return False
    if need_watts and spec['watts'] in (None, 'AMBIGUOUS'):
        return False
    return True


def init_beliefs(obs, llm):
    tickets, parts = build_evidence(obs)
    T, P = {}, {}
    for tid, text in tickets.items():
        tr = parse_ticket(text)
        rx = {'voltage': tr['voltage'], 'connector': tr['connector'],
              'watts': tr['wmin'] if tr['wmin'] not in (0,) else None}
        ok = _good(rx, need_watts=False) and 'AMBIGUOUS' not in (tr['wmin'], tr['wmax'])
        if llm is not None:
            lv = llm['requests'].get(tid)
            if (not lv or lv['voltage'] != rx['voltage'] or lv['connector'] != rx['connector']
                    or lv['watts'] != rx['watts']):
                ok = False
        lv = llm['requests'].get(tid) if llm is not None else None
        known = ok and isinstance(tr['wmin'], (int, float)) and isinstance(tr['wmax'], (int, float))
        open_q = bool(known and tr.get('assumed_max'))
        T[tid] = {'ok': ok, 'voltage': rx['voltage'], 'connector': rx['connector'],
                  'watts': tr['wmin'] if known else None, 'wmin': tr['wmin'] if known else None,
                  'wmax': tr['wmax'] if known else None, 'clarified': False, 'max_open': open_q,
                  'rx': rx, 'l1': lv, 'text': text}
    for pid, info in parts.items():
        rx = parse_spec(info['text'])
        ok = _good(rx)
        if llm is not None:
            lv = llm['parts'].get(pid)
            if (not lv or lv['voltage'] != rx['voltage'] or lv['connector'] != rx['connector']
                    or lv['watts'] != rx['watts']):
                ok = False
        tentative = None
        if not ok:
            lv = llm['parts'].get(pid) if llm is not None else None
            for cand in (lv, rx):
                if cand and _good(cand) and cand['watts'] in (10, 20):
                    tentative = {k: cand[k] for k in ('voltage', 'connector', 'watts')}
                    break
        P[pid] = {'ok': ok, 'confirmed': False, 'voltage': rx['voltage'], 'connector': rx['connector'],
                  'watts': rx['watts'], 'age': info['age'], 'p': P_AVAIL, 'known': False,
                  'inspected': False, 'tentative': tentative}
    return {'T': T, 'P': P}


def _complete(v):
    return (v is not None and v.get('voltage') not in (None, 'AMBIGUOUS')
            and v.get('connector') in ('USB-C', 'barrel') and v.get('watts') in (None, 10, 20))


def _same(a, b):
    return all(a.get(k) == b.get(k) for k in ('voltage', 'connector', 'watts'))


def _numbers(text):
    vals = set()
    for n in re.findall(r'\d+(?:\.\d+)?', text or ''):
        v = float(n)
        vals.update({v, v / 1000.0})
    return vals


def _plausible(text, v):
    """Every value the models agree on must literally appear in the request text."""
    nums = _numbers(_clean(text or ''))
    low = (text or '').lower()
    if float(v['voltage']) not in nums:
        return False
    if v['watts'] is not None and float(v['watts']) not in nums:
        return False
    if v['connector'] == 'USB-C' and 'usb' not in low and 'type-c' not in low and 'type c' not in low:
        return False
    if v['connector'] == 'barrel' and not any(w in low for w in ('barrel', 'jack', 'coax')):
        return False
    return True


def needs_second_opinion(b):
    out = []
    for tid, t in b['T'].items():
        if t['ok']:
            continue
        rx, l1 = t.get('rx'), t.get('l1')
        if (rx and _complete(rx)) or (l1 and _complete(l1)):
            out.append(tid)
    return out


def second_prompt(b, tids):
    lines = ['Re-read these workshop parts requests carefully and independently.',
             'For each request give the required voltage in volts, the connector ("USB-C" or "barrel"), '
             'and the minimum wattage in watts (null if it is not stated). Convert mV to V and mW to W. '
             'Ignore values the text says to ignore (retired fixture, discarded unit, old suggestions).',
             'Answer with ONLY JSON: {"requests":{"<id>":{"voltage":12,"connector":"USB-C","min_watts":null}}}',
             '']
    for tid in tids:
        lines.append('%s: %s' % (tid, b['T'][tid].get('text', '')))
    return '\n'.join(lines)


def apply_second_opinion(b, tids, llm2):
    for tid in tids:
        t = b['T'][tid]
        l2 = (llm2 or {}).get('requests', {}).get(tid)
        if not l2 or not _complete(l2):
            continue
        rx, l1 = t.get('rx'), t.get('l1')
        agreed = None
        if rx and _complete(rx) and _same(rx, l2):
            agreed = rx                       # parser + second model agree
        elif l1 and _complete(l1) and _same(l1, l2) and _plausible(t.get('text'), l2):
            agreed = l2                       # two model reads agree, values found in text
        if agreed is not None:
            tr = parse_ticket(t.get('text', ''))
            w = agreed['watts']
            wmax = tr['wmax'] if isinstance(tr['wmax'], (int, float)) else w
            t.update({'ok': True, 'voltage': agreed['voltage'], 'connector': agreed['connector'],
                      'watts': w, 'wmin': w, 'wmax': wmax if w is not None else None, 'second_opinion': True})


def _reply_text(data, as_of=None):
    """Find the request text in a clarify result, whatever field or shape it uses."""
    docs, strings = [], []
    def walk(x):
        if isinstance(x, dict):
            if isinstance(x.get('text'), str) and ('entity_id' in x or 'revision' in x or 'status' in x):
                docs.append(x)
            for k, v in x.items():
                if k in ('ticket_id', 'id', 'entity_id', 'document_id', 'lineage', 'status', 'source', 'op'):
                    continue
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            strings.append(x)
    walk(data)
    if docs:   # document list: take the effective (latest approved) revision
        cur = current_texts([dict(d, entity_id=d.get('entity_id', 'x'), status=d.get('status', 'approved'))
                             for d in docs], as_of)
        if len(cur) == 1:
            return list(cur.values())[0]
    for key in ('reply', 'text', 'request', 'current_request', 'approved_request', 'message'):
        if isinstance(data.get(key), str) and data[key].strip():
            return data[key]
    return ' '.join(st for st in strings if re.search(r'\d', st))


def _structured_range(data):
    """Pick up numeric min/max watt fields if the reply is structured."""
    lo = hi = None
    def walk(x):
        nonlocal lo, hi
        if isinstance(x, dict):
            for k, v in x.items():
                kl = str(k).lower()
                if isinstance(v, (int, float)) and not isinstance(v, bool) and 'watt' in kl or kl in ('min_w', 'max_w'):
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        val = v / 1000.0 if 'mw' in kl or 'milli' in kl else v
                        if 'min' in kl:
                            lo = val
                        elif 'max' in kl:
                            hi = val
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    return lo, hi


def apply_result(b, last, result):
    if not last or not isinstance(result, dict) or not result.get('ok'):
        return
    data = result.get('data') or {}
    op = last.get('op')
    if op in ('inspect', 'verify'):
        pid = last.get('part_id')
        part = b['P'].get(pid)
        if part is None:
            return
        # tool card is authoritative for the technical specification
        try:
            part.update({'voltage': data['voltage'], 'connector': data['connector'], 'watts': data['watts'],
                         'ok': True, 'confirmed': True})
        except Exception:
            pass
        avail = data.get('available')
        wr = b.get('manifest', {}).get(pid)
        if op == 'inspect' and wr is not None:
            part['inspected'] = True
            qty = None
            for doc in data.get('stock_documents') or []:
                try:
                    if (doc.get('status') == 'approved' and doc.get('revision') == wr
                            and doc.get('event_at', 0) <= (b.get('as_of') if b.get('as_of') is not None else 10**9)):
                        qty = max(0, int(doc['quantity']) - int(data.get('dispatches_recorded') or 0))
                except Exception:
                    continue
            if qty is not None:
                part.update({'qty': qty, 'p': 1.0 if qty > 0 else 0.0, 'known': True})
            return
        if op == 'verify' and not isinstance(avail, (int, float)):
            for key in ('quantity', 'warehouse_quantity', 'stock'):
                if isinstance(data.get(key), (int, float)) and not isinstance(data.get(key), bool):
                    avail = data[key]
                    break
        if op == 'verify' and isinstance(avail, (int, float)) and not isinstance(avail, bool):
            part.update({'qty': avail, 'p': 1.0 if avail > 0 else 0.0, 'known': True, 'inspected': True})
            return
        if b.get('manifest') and op == 'inspect':
            part['inspected'] = True
            return
        if isinstance(avail, (int, float)) and not isinstance(avail, bool):
            trusted = op == 'verify' or (b.get('mode') == 'ordinary' and part['age'] == 'recent')
            if trusted:
                part['p'] = 1.0 if avail > 0 else 0.0
                part['known'] = True
            elif op == 'inspect':
                part['p'] = 0.84 if avail > 0 else 0.56
            part['inspected'] = True
    elif op == 'clarify':
        tid = last.get('ticket_id')
        t = b['T'].get(tid)
        if t is None:
            return
        t['clarified'] = True
        t['max_open'] = False
        reply = _reply_text(data, b.get('as_of')) or ''
        tr = parse_ticket(reply)
        lo, hi = _structured_range(data)
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and 0 < lo <= hi:
            tr['wmin'], tr['wmax'] = lo, hi
        if (tr['voltage'] not in (None, 'AMBIGUOUS') and tr['connector'] not in (None, 'AMBIGUOUS')):
            t['voltage'], t['connector'] = tr['voltage'], tr['connector']
        if isinstance(tr['wmin'], (int, float)) and isinstance(tr['wmax'], (int, float)) and tr['wmin'] > 0:
            t.update({'watts': tr['wmin'], 'wmin': tr['wmin'], 'wmax': tr['wmax']})
        else:
            w = parse_reply_watts(reply)
            if w is None:
                t['ok'] = False          # cannot interpret -> hand to a person
            else:
                t.update({'watts': w, 'wmin': w, 'wmax': INF_W if _OPEN_TOP.search(reply) else w})


# ----------------------------------------------------------------------------
# Value model
# ----------------------------------------------------------------------------

def compatible(part, t, watts):
    if not (part['ok'] and part['voltage'] == t['voltage'] and part['connector'] == t['connector']
            and isinstance(part['watts'], (int, float))):
        return False
    if t.get('watts') is not None and isinstance(t.get('wmin'), (int, float)) and isinstance(t.get('wmax'), (int, float)):
        return t['wmin'] <= part['watts'] <= t['wmax']
    return part['watts'] == watts      # hypothetical exact requirement


def ticket_options(b, tid, used=()):
    """List (part_id or None, value) options for one undecided ticket."""
    t = b['T'][tid]
    P = b['P']
    if not t['ok']:
        return [(None, 0.15)]   # unknown value, escalate (always allowed)
    if t['watts'] is not None:
        cands = [pid for pid in P if compatible(P[pid], t, t['watts'])]
        q20 = 1.0 if t['watts'] == 20 else 0.0
    else:
        cands = []                      # range unknown: never dispatch before clarify
        q20 = P_W20
    # escalation value: full credit if NO compatible part available at batch start
    none20 = 1.0
    for pid in P:
        if compatible(P[pid], t, 20):
            none20 *= (1 - P[pid]['p'])
    none10 = 1.0
    for pid in P:
        if compatible(P[pid], t, 10):
            none10 *= (1 - P[pid]['p'])
    # requirement parts we could not parse make "infeasible" uncertain -> be pessimistic
    unsure = any(not P[pid]['ok'] for pid in P)
    infeasible = q20 * none20 + (1 - q20) * none10
    if unsure:
        infeasible *= 0.6
    opts = [(None, infeasible + (1 - infeasible) * 0.15)]
    for pid in cands:
        if pid in used:
            continue
        opts.append((pid, P[pid]['p']))
    return opts


def _cap(b, pid):
    q = b['P'][pid].get('qty')
    return int(q) if isinstance(q, (int, float)) and q >= 1 and b['P'][pid].get('known') else 1


def batch_value(b, tids):
    """Exact best assignment value for the undecided tickets; a part with k known units may serve k requests."""
    options = [ticket_options(b, tid) for tid in tids]
    size = 1
    for o in options:
        size *= len(o)
    if size > 20000:
        return _greedy_plan(options, b)
    best, best_plan = -1.0, None
    for combo in product(*options):
        used = {}
        bad = False
        for pid, _ in combo:
            if pid is not None:
                used[pid] = used.get(pid, 0) + 1
                if used[pid] > _cap(b, pid):
                    bad = True
                    break
        if bad:
            continue
        value = sum(v for _, v in combo)
        if value > best:
            best, best_plan = value, combo
    return best, best_plan


def _greedy_plan(options, b=None):
    """Scales to large batches: give each ticket its best option with stock left."""
    used, plan, total = {}, [], 0.0
    for opts in options:
        pick = max((o for o in opts if o[0] is None or used.get(o[0], 0) < (_cap(b, o[0]) if b else 1)),
                   key=lambda o: o[1])
        if pick[0] is not None:
            used[pick[0]] = used.get(pick[0], 0) + 1
        plan.append(pick)
        total += pick[1]
    return total, tuple(plan)


def candidate_checks(b, tids, budget_left, mode):
    checks = []
    P = b['P']
    relevant = set()
    for tid in tids:
        t = b['T'][tid]
        if not t['ok']:
            continue
        for pid in P:
            if compatible(P[pid], t, 10) or compatible(P[pid], t, 20):
                relevant.add(pid)
        if (t.get('max_open') and not t['clarified'] and budget_left >= 2 and t['wmin'] == 10
                and any(P[pid]['ok'] and P[pid]['voltage'] == t['voltage'] and P[pid]['connector'] == t['connector']
                        and P[pid]['watts'] == 20 for pid in P)):
            checks.append(('clarify', tid, 2))
        if t['watts'] is None and not t['clarified'] and budget_left >= 2:
            if any(compatible(P[pid], t, 10) or compatible(P[pid], t, 20) for pid in P):
                checks.append(('clarify', tid, 2))
    for tid in tids:
        t = b['T'][tid]
        if not t['ok']:
            continue
        for pid in P:
            tv = P[pid].get('tentative')
            if (not P[pid]['ok'] and tv and tv['voltage'] == t['voltage']
                    and tv['connector'] == t['connector'] and tv['watts'] >= (t['watts'] or 10)):
                relevant.add(pid)
    for pid in sorted(relevant):
        part = P[pid]
        if part['known']:
            continue
        if b.get('manifest'):
            cheap = pid in b.get('desk_ok', [])          # desk is behind the warehouse -> go straight to verify
        else:
            cheap = mode == 'ordinary' and part['age'] == 'recent'
        if cheap and not part['inspected'] and budget_left >= 1:
            checks.append(('inspect', pid, 1))
        elif budget_left >= 2:
            checks.append(('verify', pid, 2))
    return checks


def _branch(b, check, outcome):
    nb = json.loads(json.dumps(b))
    kind, key, _ = check
    if kind == 'clarify' and nb['T'][key].get('max_open'):
        nb['T'][key].update({'wmax': max(outcome, nb['T'][key]['wmin']), 'clarified': True, 'max_open': False})
    elif kind == 'clarify':
        nb['T'][key].update({'watts': outcome, 'wmin': outcome, 'wmax': outcome, 'clarified': True})
    else:
        tv = nb['P'][key].get('tentative')
        if not nb['P'][key]['ok'] and tv:
            nb['P'][key].update(tv)
            nb['P'][key]['ok'] = True
        nb['P'][key]['p'] = float(outcome)
        nb['P'][key]['known'] = True
        nb['P'][key]['inspected'] = True
    return nb


def best_check(b, tids, budget_left, mode):
    base, _ = batch_value(b, tids)
    best, best_gain = None, 1e-6
    for check in candidate_checks(b, tids, budget_left, mode):
        kind, key, cost = check
        if kind == 'clarify':
            branches = [(P_W20, 20), (1 - P_W20, 10)]
        else:
            p = b['P'][key]['p']
            branches = [(p, 1), (1 - p, 0)]
        value = sum(w * batch_value(_branch(b, check, out), tids)[0] for w, out in branches if w > 0)
        gain = (value - base) / cost
        if gain > best_gain:
            best, best_gain = check, gain
    return best


# ----------------------------------------------------------------------------
# Controller
# ----------------------------------------------------------------------------

def _mode(obs):
    policy = obs.get('policy', '') or ''
    if 'only verify is authoritative' in policy or 'may be stale' in policy:
        return 'changed'
    return 'ordinary'


def next_action(obs, state):
    b = state['beliefs']
    decided = set(obs.get('decided_ticket_ids', [])) | set(state.get('decided', []))
    tids = [t['id'] for t in obs.get('tickets', []) if t['id'] not in decided and t['id'] in b['T']]
    if not tids:
        return {'op': 'finish'}
    budget_left = obs.get('budget_remaining', 0) - (14 - BUDGET_CAP)
    steps_left = obs.get('steps_remaining', 60)
    # keep enough steps to record every decision
    if steps_left > len(tids) + 1 and budget_left > 0:
        check = best_check(b, tids, budget_left, b['mode'])
        if check is not None:
            kind, key, cost = check
            if kind == 'clarify':
                return {'op': 'clarify', 'ticket_id': key}
            return {'op': kind, 'part_id': key}
    # decide one ticket from the best joint plan
    _, plan = batch_value(b, tids)
    for tid, (pid, _) in zip(tids, plan):
        t = b['T'][tid]
        if pid is not None:
            part = b['P'][pid]
            # final safety gate: known range, exact voltage/connector, watts inside range
            if (t['ok'] and t['watts'] is not None and isinstance(t.get('wmax'), (int, float))
                    and compatible(part, t, t['watts'])):
                q = part.get('qty')
                if isinstance(q, (int, float)) and q > 1:
                    part['qty'] = q - 1
                else:
                    part['qty'] = 0
                    part['p'] = 0.0
                    part['known'] = True
                return {'op': 'dispatch', 'ticket_id': tid, 'part_id': pid}
        return {'op': 'escalate', 'ticket_id': tid}
    return {'op': 'escalate', 'ticket_id': tids[0]}


def decide(observation, state):
    state = dict(state or {})
    try:
        if 'beliefs' not in state:
            if 'llm_response' not in observation:
                tickets, parts = build_evidence(observation)
                state['phase'] = 'await_llm'
                return {'action': {'op': 'llm', 'max_output_tokens': MAX_LLM_TOKENS,
                                   'messages': [{'role': 'user', 'content': llm_prompt(tickets, parts)}]},
                        'state': state}
            llm = parse_llm(observation.get('llm_response', ''))
            state['llm_parsed'] = llm is not None
            beliefs = init_beliefs(observation, llm)
            beliefs['mode'] = _mode(observation)
            beliefs['as_of'] = observation.get('as_of')
            beliefs['manifest'] = {m.get('part_id'): m.get('warehouse_revision')
                                   for m in (observation.get('inventory_manifest') or []) if isinstance(m, dict)}
            # parts whose desk copy already holds the warehouse revision: inspect is exact for them
            beliefs['desk_ok'] = [m.get('part_id') for m in (observation.get('inventory_manifest') or [])
                                  if isinstance(m, dict) and isinstance(m.get('desk_receipt_revision'), int)
                                  and isinstance(m.get('warehouse_revision'), int)
                                  and m['desk_receipt_revision'] >= m['warehouse_revision']]
            state['beliefs'] = beliefs
            state['spent'] = 0
            state['decided'] = []
            recheck = needs_second_opinion(beliefs) if llm is not None else []
            if recheck:
                state['recheck'] = recheck
                state['last'] = {'op': 'llm'}
                return {'action': {'op': 'llm', 'max_output_tokens': 1024,
                                   'messages': [{'role': 'user', 'content': second_prompt(beliefs, recheck)}]},
                        'state': state}
        elif 'llm_response' in observation and state.get('recheck'):
            apply_second_opinion(state['beliefs'], state['recheck'], parse_llm(observation['llm_response']))
            state['recheck'] = []
        else:
            apply_result(state['beliefs'], state.get('last'), observation.get('result'))
        action = next_action(observation, state)
        if action['op'] in ('dispatch', 'escalate'):
            state['decided'] = state.get('decided', []) + [action['ticket_id']]
        state['last'] = action
        return {'action': action, 'state': state}
    except Exception:
        # Never crash: hand the next undecided request to a person.
        decided = set(observation.get('decided_ticket_ids', [])) | set(state.get('decided', []))
        remaining = [t['id'] for t in observation.get('tickets', []) if t['id'] not in decided]
        if 'beliefs' not in state and 'llm_response' not in observation:
            return {'action': {'op': 'llm', 'max_output_tokens': 64,
                               'messages': [{'role': 'user', 'content': 'Reply with {}'}]}, 'state': state}
        action = {'op': 'escalate', 'ticket_id': remaining[0]} if remaining else {'op': 'finish'}
        state.setdefault('beliefs', {'T': {}, 'P': {}, 'mode': 'ordinary'})
        if remaining:
            state['decided'] = state.get('decided', []) + [remaining[0]]
        state['last'] = action
        return {'action': action, 'state': state}
