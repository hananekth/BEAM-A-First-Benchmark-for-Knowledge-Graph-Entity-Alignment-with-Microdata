# Deployment On A New VM (Python Already Installed)

This setup is designed for a fresh VM where Python is already present (e.g. Python 3.12.3).
No Docker is required.

## 1) Clone

```bash
git clone <repo-url> BEAM-App
cd BEAM-App
```

## 2) Bootstrap (one command)

```bash
bash scripts/bootstrap_vm.sh
```

Optional (with test dependencies):

```bash
bash scripts/bootstrap_vm.sh --dev
```

This will:
- create `.venv`
- install dependencies
- create runtime folders (`.run`, `logs`, `Download`, `data`)
- initialize/migrate `jobs.db`
- create `.env` from `.env.example` if missing

## 3) Start services

```bash
bash scripts/run_webapp.sh
bash scripts/run_worker.sh
```

UI:
- local: `http://127.0.0.1:8501`
- remote: `http://<vm-ip>:8501`

## 4) Health check

```bash
bash scripts/check_health.sh
```

## 5) Stop services

```bash
bash scripts/stop_all.sh
```

## 6) Reset to fresh runtime instance (keep presets)

```bash
bash scripts/init_fresh_instance.sh
```

What it does:
- backup `jobs.db` into `.run/db_backups/`
- clear job/subjob/event history
- keep presets (presets are code-defined)
- keep `Download/` and `data/` folders

## 7) Optional tmux usage

```bash
tmux new -s beam_web 'cd ~/BEAM-App && bash scripts/run_webapp.sh'
tmux new -s beam_worker 'cd ~/BEAM-App && bash scripts/run_worker.sh'
```

## 8) Troubleshooting

- Port busy:
```bash
lsof -i :8501
bash scripts/stop_all.sh
```

- Web not reachable:
```bash
tail -n 100 logs/webapp.log
```

- Worker not progressing jobs:
```bash
tail -n 100 logs/worker.log
```

- DB reset needed:
```bash
bash scripts/init_fresh_instance.sh
```
