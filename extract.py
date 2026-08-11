import json
d = json.load(open('macrobond_probe.json'))
for k in ('cta', 'ici'):
    print('===', k)
    seen = set()
    for q, v in d['targets'].get(k, {}).get('queries', {}).items():
        for h in v.get('hits', []):
            s = h.get('series', {})
            if not s.get('ok') or s['code'] in seen:
                continue
            seen.add(s['code'])
            print(f"{s['code']:<26} {s.get('inferred_frequency','?'):<8} "
                  f"{s['first_date']} -> {s['last_date']}  n={s['n']:<6} "
                  f"{str(h.get('search_hit',{}).get('title'))[:65]}")
