import json
import time
from pathlib import Path
from beam import pipeline

base = Path('/home/billal/BEAM-App/.run')
base.mkdir(parents=True, exist_ok=True)
status_path = base / 'bg_real_builds_status.json'

runs = [
    {
        'label': 'museum_single_custom_chars',
        'params': {
            'class_name': 'Museum',
            'parts_spec': 'all',
            'matching_mode': 'property',
            'wdc_predicate_pattern': 'telephone',
            'wikidata_property': 'P1329',
            'target_property': 'P1329',
            'wkd_class': 'Q33506',
            'target_class': 'Q33506',
            'ignore_chars': 'spaces;dot;hyphen;(;);tel:',
            'use_local_only': True,
            'force_align': True,
            'force_one_to_one_links': False,
            'dedup_wdc_exact_subgraph_by_link_value': False,
        },
    },
    {
        'label': 'museum_multi_custom_chars',
        'params': {
            'class_name': 'Museum',
            'parts_spec': 'all',
            'matching_mode': 'property',
            'wdc_predicate_pattern': '',
            'property_mapping_rules': 'telephone,name => P1329,rdfs:label || ["spaces;dot;hyphen;(;);tel:","spaces;dot;hyphen;museum"]',
            'wikidata_property': '',
            'target_property': '',
            'wkd_class': 'Q33506',
            'target_class': 'Q33506',
            'ignore_chars': 'spaces;dot;hyphen;(;);tel:',
            'use_local_only': True,
            'force_align': True,
            'force_one_to_one_links': False,
            'dedup_wdc_exact_subgraph_by_link_value': False,
        },
    },
]

state = {
    'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    'running': True,
    'runs': [],
}
status_path.write_text(json.dumps(state, indent=2), encoding='utf-8')

for run in runs:
    item = {'label': run['label'], 'started_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'ok': None}
    state['runs'].append(item)
    status_path.write_text(json.dumps(state, indent=2), encoding='utf-8')
    try:
        result = pipeline.generate_benchmark(run['params'], workers=1)
        out = Path(result['out_dir'])
        item.update({
            'ok': True,
            'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'out_dir': str(out),
            'links_tsv': str(result.get('links_tsv')),
            'build_done': (out / 'BUILD_DONE').exists(),
        })
    except Exception as e:
        item.update({
            'ok': False,
            'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'error': repr(e),
        })
    status_path.write_text(json.dumps(state, indent=2), encoding='utf-8')

state['running'] = False
state['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
status_path.write_text(json.dumps(state, indent=2), encoding='utf-8')
