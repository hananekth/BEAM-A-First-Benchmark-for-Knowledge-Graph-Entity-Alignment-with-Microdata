import json
import time
from pathlib import Path
from beam import pipeline

base = Path('/home/billal/BEAM-App/.run')
base.mkdir(parents=True, exist_ok=True)
status_path = base / 'bg_airport_p4_status.json'

runs = [
    {
        'label': 'airport_p4_single_iata_custom_chars',
        'params': {
            'class_name': 'Airport',
            'parts_spec': '4',
            'matching_mode': 'property',
            'wdc_predicate_pattern': 'iata',
            'wikidata_property': 'P238',
            'target_property': 'P238',
            'wkd_class': 'Q1248784',
            'target_class': 'Q1248784',
            'ignore_chars': 'spaces;dot;hyphen;airport',
            'use_local_only': True,
            'force_align': True,
            'force_one_to_one_links': False,
            'dedup_wdc_exact_subgraph_by_link_value': False,
        },
    },
    {
        'label': 'airport_p4_multi_iata_name_custom_chars',
        'params': {
            'class_name': 'Airport',
            'parts_spec': '4',
            'matching_mode': 'property',
            'wdc_predicate_pattern': '',
            'property_mapping_rules': 'iata,name => P238,rdfs:label || ["spaces;dot;hyphen","spaces;dot;hyphen;airport"]',
            'wikidata_property': '',
            'target_property': '',
            'wkd_class': 'Q1248784',
            'target_class': 'Q1248784',
            'ignore_chars': 'spaces;dot;hyphen;airport',
            'use_local_only': True,
            'force_align': True,
            'force_one_to_one_links': False,
            'dedup_wdc_exact_subgraph_by_link_value': False,
        },
    },
]

state = {'started_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'running': True, 'runs': []}
status_path.write_text(json.dumps(state, indent=2), encoding='utf-8')

for run in runs:
    item = {'label': run['label'], 'started_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'ok': None}
    state['runs'].append(item)
    status_path.write_text(json.dumps(state, indent=2), encoding='utf-8')
    try:
        result = pipeline.generate_benchmark(run['params'], workers=1)
        out = Path(result['out_dir'])
        item.update({'ok': True, 'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'out_dir': str(out), 'links_tsv': str(result.get('links_tsv')), 'build_done': (out / 'BUILD_DONE').exists()})
    except Exception as e:
        item.update({'ok': False, 'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'error': repr(e)})
    status_path.write_text(json.dumps(state, indent=2), encoding='utf-8')

state['running'] = False
state['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
status_path.write_text(json.dumps(state, indent=2), encoding='utf-8')
