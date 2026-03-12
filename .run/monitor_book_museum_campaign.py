import json
import time
import sys
from pathlib import Path

ROOT = Path('/home/billal/BEAM-App')
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beam import db

RUN = ROOT / '.run'
RUN.mkdir(parents=True, exist_ok=True)
STATUS = RUN / 'book_museum_campaign_status.json'
WATCH_IDS = list(range(145, 162))

def load_params(jid):
    j = db.get_job(jid)
    if not j:
        return None, None
    try:
        p = json.loads(j['params_json'] or '{}')
    except Exception:
        p = {}
    return j, p

def summarize(jobs):
    out = {
        'Book': {'wikidata': {'done': 0, 'best_links': 0, 'best_result': ''}, 'yago': {'done': 0, 'best_links': 0, 'best_result': ''}, 'dbpedia': {'done': 0, 'best_links': 0, 'best_result': ''}},
        'Museum': {'wikidata': {'done': 0, 'best_links': 0, 'best_result': ''}, 'yago': {'done': 0, 'best_links': 0, 'best_result': ''}, 'dbpedia': {'done': 0, 'best_links': 0, 'best_result': ''}},
    }
    for row in jobs:
        cls = row.get('class_name')
        ep = row.get('endpoint')
        if cls not in out or ep not in out[cls]:
            continue
        if row.get('status') == 'done':
            out[cls][ep]['done'] += 1
            links = int(row.get('final_links') or 0)
            if links >= out[cls][ep]['best_links']:
                out[cls][ep]['best_links'] = links
                out[cls][ep]['best_result'] = row.get('result_path') or ''
    return out

state = {'started_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'running': True, 'watch_ids': WATCH_IDS, 'jobs': []}
while True:
    rows = []
    active = False
    for jid in WATCH_IDS:
        j, p = load_params(jid)
        if not j:
            continue
        status = str(j['status'])
        if status in {'queued', 'running'}:
            active = True
        rows.append({
            'job_id': jid,
            'status': status,
            'phase': j['phase'],
            'class_name': str(p.get('class_name', '')),
            'endpoint': str(p.get('target_endpoint', '')),
            'target_property': str(p.get('target_property', p.get('wikidata_property', ''))),
            'force_align': bool(p.get('force_align')),
            'force_one_to_one_links': bool(p.get('force_one_to_one_links')),
            'final_links': j['final_links_count'],
            'result_path': str(j['result_path'] or ''),
            'error_message': str(j['error_message'] or ''),
            'progress_text': str(j['progress_text'] or ''),
        })
    state['jobs'] = rows
    state['summary'] = summarize(rows)
    state['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    state['running'] = active
    if not active:
        state['finished_at'] = state['updated_at']
    STATUS.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding='utf-8')
    if not active:
        break
    time.sleep(15)
