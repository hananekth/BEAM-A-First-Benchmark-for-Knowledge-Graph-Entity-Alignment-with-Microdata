import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path('/home/billal/BEAM-App')
RUN = ROOT / '.run'
RUN.mkdir(parents=True, exist_ok=True)
status_path = RUN / 'reliability_suite_status.json'
report_path = RUN / 'reliability_suite_report.json'
pytest_log = RUN / 'reliability_pytest.log'

state = {
    'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    'running': True,
    'steps': [],
}

def save():
    status_path.write_text(json.dumps(state, indent=2), encoding='utf-8')

def add_step(name):
    step = {'name': name, 'started_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'ok': None}
    state['steps'].append(step)
    save()
    return step

def finish_step(step, ok, **extra):
    step['ok'] = bool(ok)
    step['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    step.update(extra)
    save()

def run_cmd(cmd, timeout=None, log_path=None):
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    if log_path:
        log_path.write_text((proc.stdout or '') + '\n' + (proc.stderr or ''), encoding='utf-8')
    return proc

def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return None

save()

# 1) Full pytest
step = add_step('pytest_full')
try:
    proc = run_cmd([sys.executable, '-m', 'pytest', '-q'], timeout=3600, log_path=pytest_log)
    finish_step(step, proc.returncode == 0, returncode=proc.returncode, log=str(pytest_log))
except Exception as e:
    finish_step(step, False, error=repr(e), log=str(pytest_log))

# 2) Wait for background status files to settle
for status_file, timeout_s in [
    (RUN / 'bg_real_builds_status.json', 7200),
    (RUN / 'bg_airport_p4_status.json', 7200),
]:
    step = add_step(f'wait_{status_file.name}')
    try:
        start = time.time()
        last = 'unknown'
        while time.time() - start < timeout_s:
            data = read_json(status_file)
            if isinstance(data, dict):
                running = data.get('running')
                if running is False:
                    last = 'done'
                    break
                if running is True:
                    last = 'running'
                else:
                    last = f'running={running!r}'
            else:
                last = 'missing_or_invalid'
            if last == 'done':
                break
            time.sleep(10)
        ok = (last == 'done')
        finish_step(step, ok, final_state=last, waited_seconds=int(time.time() - start))
    except Exception as e:
        finish_step(step, False, error=repr(e))

# 3) Validate produced status files + build outputs
step = add_step('validate_outputs')
errors = []
summary = {'runs': []}
try:
    targets = [
        RUN / 'bg_real_builds_status.json',
        RUN / 'bg_airport_p4_status.json',
    ]
    for sp in targets:
        data = read_json(sp)
        if not isinstance(data, dict):
            errors.append(f'missing_or_invalid_status:{sp}')
            continue
        if data.get('running') not in {False}:
            errors.append(f'still_running:{sp}')
        for r in data.get('runs') or []:
            entry = {'status_file': str(sp), 'label': r.get('label'), 'ok': r.get('ok'), 'out_dir': r.get('out_dir')}
            out_dir = r.get('out_dir')
            if not r.get('ok'):
                errors.append(f'run_failed:{r.get("label")}')
            if out_dir:
                out = Path(out_dir)
                entry['build_done'] = (out / 'BUILD_DONE').exists()
                wl = out / 'with_link_code' / 'ent_links'
                if wl.exists():
                    with wl.open('r', encoding='utf-8', errors='ignore') as f:
                        entry['with_link_lines'] = sum(1 for _ in f)
                else:
                    entry['with_link_lines'] = -1
                if not entry['build_done']:
                    errors.append(f'build_done_missing:{out}')
                if entry['with_link_lines'] <= 0:
                    errors.append(f'ent_links_empty:{out}')
            summary['runs'].append(entry)
    finish_step(step, len(errors) == 0, errors=errors, summary=summary)
except Exception as e:
    finish_step(step, False, error=repr(e), errors=errors, summary=summary)

state['running'] = False
state['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
save()
report_path.write_text(json.dumps({'state': state}, indent=2), encoding='utf-8')
