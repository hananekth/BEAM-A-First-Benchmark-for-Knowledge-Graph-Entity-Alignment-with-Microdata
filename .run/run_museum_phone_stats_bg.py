import json
import time
import sys
import re
from pathlib import Path
from collections import Counter

ROOT = Path('/home/billal/BEAM-App')
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import align

RUN = ROOT / '.run'
REPORTS = ROOT / 'reports'
RUN.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)
STATUS = RUN / 'hanane_museum_phone_stats_status.json'
PHONE_JSON = REPORTS / 'museum_phone_stats.json'
PHONE_TSV = REPORTS / 'museum_phone_counts.tsv'


def save(obj):
    STATUS.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding='utf-8')

state = {'started_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'running': True, 'status': 'running'}
save(state)

strip_chars = align.parse_strip_list('spaces;dot;hyphen;(;);tel:;+')
align.set_extra_strip_chars(strip_chars)

pred_re = re.compile(r'^\s*(\S+)\s+<([^>]+)>\s+(".*?"(?:\^\^<[^>]+>|@[a-zA-Z-]+)?|<[^>]+>|_:[^\s]+)')

def lit_lex(o):
    if not o.startswith('"'):
        return None
    esc = False
    for i, ch in enumerate(o[1:], start=1):
        if esc:
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == '"':
            return o[1:i]
    return None

wdc_counter = Counter()
parts = sorted([p for p in (ROOT / 'Download' / 'Museum').glob('part_*') if p.is_file()])
for part in parts:
    with part.open('r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            m = pred_re.match(line)
            if not m:
                continue
            pred = m.group(2).lower()
            if 'telephone' not in pred and 'phone' not in pred:
                continue
            lex = lit_lex(m.group(3))
            if not lex:
                continue
            nv = align.normalize_for_phone_matching(lex)
            if nv:
                wdc_counter[nv] += 1


def target_counter(endpoint, prop, cls):
    m = align.fetch_target_values(target_property=prop, target_class=cls, target_endpoint=endpoint)
    c = Counter()
    for k, vals in (m or {}).items():
        c[k] += len(vals)
    return c

wkd_counter = target_counter('wikidata', 'P1329', 'Q33506')
yago_counter = target_counter('yago', 'schema:telephone', 'schema:Museum')
dbp_counter = target_counter('dbpedia', 'dbp:phone', 'dbo:Museum')


def summarize(counter):
    total = int(sum(counter.values()))
    unique = int(len(counter))
    repeated_keys = int(sum(1 for v in counter.values() if v > 1))
    repeated_occ = int(sum(v for v in counter.values() if v > 1))
    max_rep = int(max(counter.values()) if counter else 0)
    return {
        'total_values': total,
        'unique_values': unique,
        'repeated_keys': repeated_keys,
        'repeated_occurrences': repeated_occ,
        'max_repetition': max_rep,
    }

all_keys = sorted(set(wdc_counter) | set(wkd_counter) | set(yago_counter) | set(dbp_counter))
combined = {
    k: {
        'wdc': int(wdc_counter.get(k, 0)),
        'wikidata': int(wkd_counter.get(k, 0)),
        'yago': int(yago_counter.get(k, 0)),
        'dbpedia': int(dbp_counter.get(k, 0)),
    }
    for k in all_keys
}

report = {
    'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    'class_name': 'Museum',
    'key': 'telephone',
    'summary': {
        'wdc': summarize(wdc_counter),
        'wikidata': summarize(wkd_counter),
        'yago': summarize(yago_counter),
        'dbpedia': summarize(dbp_counter),
    },
    'value_counts': combined,
}
PHONE_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')

with PHONE_TSV.open('w', encoding='utf-8') as out:
    out.write('value\twdc\twikidata\tyago\tdbpedia\n')
    for k in all_keys:
        v = combined[k]
        out.write(f"{k}\t{v['wdc']}\t{v['wikidata']}\t{v['yago']}\t{v['dbpedia']}\n")

state.update({
    'running': False,
    'status': 'done',
    'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    'json_report': str(PHONE_JSON),
    'tsv_report': str(PHONE_TSV),
    'summary': report['summary'],
})
save(state)
