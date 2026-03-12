import json
import time
from pathlib import Path
from collections import Counter
import re
import sys

ROOT = Path('/home/billal/BEAM-App')
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beam import db
from scripts import align

RUN = ROOT / '.run'
REPORTS = ROOT / 'reports'
RUN.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)

STATUS = RUN / 'hanane_batch_status.json'
PHONE_JSON = REPORTS / 'museum_phone_stats.json'
PHONE_TSV = REPORTS / 'museum_phone_counts.tsv'

def save(state):
    STATUS.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding='utf-8')

state = {
    'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    'running': True,
    'queued_jobs': [],
    'phone_stats': {'status': 'pending'},
}
save(state)

# 1) Queue requested builds (all property mode, one-to-one, no-cache)
base = {
    'matching_mode': 'property',
    'parts_spec': 'all',
    'force_align': True,
    'use_local_only': False,
    'force_one_to_one_links': True,
    'dedup_wdc_exact_subgraph_by_link_value': False,
}

jobs = [
    # Airport (IATA)
    dict(base, class_name='Airport', wdc_predicate_pattern='iata', target_endpoint='wikidata', target_property='P238', wikidata_property='P238', target_class='Q1248784', wkd_class='Q1248784', ignore_chars='spaces;dot;hyphen;airport'),
    dict(base, class_name='Airport', wdc_predicate_pattern='iata', target_endpoint='yago', target_property='schema:alternateName', wikidata_property='schema:alternateName', target_class='schema:Airport', wkd_class='schema:Airport', ignore_chars='spaces;dot;hyphen;airport'),
    dict(base, class_name='Airport', wdc_predicate_pattern='iata', target_endpoint='dbpedia', target_property='dbo:iataLocationIdentifier', wikidata_property='dbo:iataLocationIdentifier', target_class='dbo:Airport', wkd_class='dbo:Airport', ignore_chars='spaces;dot;hyphen;airport'),

    # Book (ISBN)
    dict(base, class_name='Book', wdc_predicate_pattern='isbn', target_endpoint='wikidata', target_property='P212', wikidata_property='P212', target_class='Q571', wkd_class='Q571', ignore_chars='spaces;dot;hyphen;isbn;:'),
    dict(base, class_name='Book', wdc_predicate_pattern='isbn', target_endpoint='yago', target_property='schema:isbn', wikidata_property='schema:isbn', target_class='schema:Book', wkd_class='schema:Book', ignore_chars='spaces;dot;hyphen;isbn;:'),
    dict(base, class_name='Book', wdc_predicate_pattern='isbn', target_endpoint='dbpedia', target_property='dbp:isbn', wikidata_property='dbp:isbn', target_class='dbo:Book', wkd_class='dbo:Book', ignore_chars='spaces;dot;hyphen;isbn;:'),

    # Museum (telephone)
    dict(base, class_name='Museum', wdc_predicate_pattern='telephone', target_endpoint='wikidata', target_property='P1329', wikidata_property='P1329', target_class='Q33506', wkd_class='Q33506', ignore_chars='spaces;dot;hyphen;(;);tel:;+'),
    dict(base, class_name='Museum', wdc_predicate_pattern='telephone', target_endpoint='yago', target_property='schema:telephone', wikidata_property='schema:telephone', target_class='schema:Museum', wkd_class='schema:Museum', ignore_chars='spaces;dot;hyphen;(;);tel:;+'),
    dict(base, class_name='Museum', wdc_predicate_pattern='telephone', target_endpoint='dbpedia', target_property='dbp:phone', wikidata_property='dbp:phone', target_class='dbo:Museum', wkd_class='dbo:Museum', ignore_chars='spaces;dot;hyphen;(;);tel:;+'),
]

for p in jobs:
    jid = db.insert_job(p)
    state['queued_jobs'].append({
        'job_id': jid,
        'class_name': p['class_name'],
        'endpoint': p['target_endpoint'],
        'target_property': p['target_property'],
        'parts_spec': p['parts_spec'],
        'force_align': p['force_align'],
        'force_one_to_one_links': p['force_one_to_one_links'],
    })
save(state)

# 2) Museum phone stats
try:
    state['phone_stats']['status'] = 'running'
    save(state)

    # normalization for phone-like values
    strip_chars = align.parse_strip_list('spaces;dot;hyphen;(;);tel:;+')
    align.set_extra_strip_chars(strip_chars)

    # WDC scan
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
    museum_dir = ROOT / 'Download' / 'Museum'
    parts = sorted([p for p in museum_dir.glob('part_*') if p.is_file()])
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

    state['phone_stats'] = {
        'status': 'done',
        'json_report': str(PHONE_JSON),
        'tsv_report': str(PHONE_TSV),
        'summary': report['summary'],
    }
except Exception as e:
    state['phone_stats'] = {
        'status': 'error',
        'error': repr(e),
    }

state['running'] = False
state['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
save(state)
