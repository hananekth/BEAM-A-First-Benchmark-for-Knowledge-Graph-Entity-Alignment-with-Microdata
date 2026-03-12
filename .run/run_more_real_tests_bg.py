import json
import time
from pathlib import Path
from beam import pipeline

base = Path('/home/billal/BEAM-App/.run')
base.mkdir(parents=True, exist_ok=True)
status_path = base / 'bg_more_real_tests_status.json'

runs = [
    {
        'label': 'museum_single_tel_part0_custom_chars_v2',
        'params': {
            'class_name': 'Museum',
            'parts_spec': '0',
            'matching_mode': 'property',
            'wdc_predicate_pattern': 'telephone',
            'target_endpoint': 'wikidata',
            'wikidata_property': 'P1329',
            'target_property': 'P1329',
            'wkd_class': 'Q33506',
            'target_class': 'Q33506',
            'ignore_chars': 'spaces;dot;hyphen;(;);tel:;+',
            'use_local_only': True,
            'force_align': True,
            'force_one_to_one_links': False,
            'dedup_wdc_exact_subgraph_by_link_value': False,
        },
    },
    {
        'label': 'museum_multi_tel_name_part0_custom_chars_v2',
        'params': {
            'class_name': 'Museum',
            'parts_spec': '0',
            'matching_mode': 'property',
            'wdc_predicate_pattern': '',
            'property_mapping_rules': 'telephone,name => P1329,rdfs:label || ["spaces;dot;hyphen;(;);tel:;+","spaces;dot;hyphen;museum"]',
            'target_endpoint': 'wikidata',
            'wikidata_property': '',
            'target_property': '',
            'wkd_class': 'Q33506',
            'target_class': 'Q33506',
            'ignore_chars': 'spaces;dot;hyphen;(;);tel:;+',
            'use_local_only': True,
            'force_align': True,
            'force_one_to_one_links': False,
            'dedup_wdc_exact_subgraph_by_link_value': False,
        },
    },
    {
        'label': 'airport_single_iata_part4_yago',
        'params': {
            'class_name': 'Airport',
            'parts_spec': '4',
            'matching_mode': 'property',
            'wdc_predicate_pattern': 'iata',
            'target_endpoint': 'yago',
            'target_property': 'schema:alternateName',
            'target_class': 'schema:Airport',
            'wkd_class': 'schema:Airport',
            'ignore_chars': 'spaces;dot;hyphen;airport',
            'use_local_only': True,
            'force_align': True,
            'force_one_to_one_links': False,
            'dedup_wdc_exact_subgraph_by_link_value': False,
        },
    },
    {
        'label': 'airport_multi_iata_name_part4_yago',
        'params': {
            'class_name': 'Airport',
            'parts_spec': '4',
            'matching_mode': 'property',
            'wdc_predicate_pattern': '',
            'property_mapping_rules': 'iata,name => schema:alternateName,rdfs:label || ["spaces;dot;hyphen","spaces;dot;hyphen;airport"]',
            'target_endpoint': 'yago',
            'target_property': '',
            'target_class': 'schema:Airport',
            'wkd_class': 'schema:Airport',
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
        out = Path(result['out_dir']) if result.get('out_dir') else None
        item.update({
            'ok': True,
            'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'out_dir': str(out) if out else None,
            'links_tsv': str(result.get('links_tsv')),
            'build_done': bool(out and (out / 'BUILD_DONE').exists()),
            'build_skipped': bool(result.get('build_skipped', False)),
            'build_skip_reason': result.get('build_skip_reason'),
        })
    except Exception as e:
        item.update({'ok': False, 'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'error': repr(e)})
    status_path.write_text(json.dumps(state, indent=2), encoding='utf-8')

state['running'] = False
state['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
status_path.write_text(json.dumps(state, indent=2), encoding='utf-8')
