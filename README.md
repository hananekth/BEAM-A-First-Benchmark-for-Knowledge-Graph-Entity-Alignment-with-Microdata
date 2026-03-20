# BEAM-App

BEAM-App is a web UI + worker pipeline to generate BEAM-style entity alignment datasets from Web Data Commons (WDC) classes and Wikidata.

It lets you:
- choose a WDC class and parts,
- align WDC entities to Wikidata,
- build BEAM output files,
- monitor jobs live,
- download completed builds.

This README focuses on how to install, run, and operate the app.

For a fresh VM deployment guide, see:
- `docs/DEPLOYMENT_VM.md`

## What Runs In This Project

Main components:
- `webapp/main.py`: FastAPI web application (UI + API + WebSocket logs)
- `worker/run.py`: background worker that executes queued jobs
- `beam/pipeline.py`: align + build orchestration
- `scripts/align.py`: alignment logic
- `scripts/build_beam_files.py`: BEAM file generation

Data and state locations:
- `Download/<ClassName>/`: local WDC parts and align cache
- `data/<ClassName>/beam_<timestamp>/`: build outputs
- `jobs.db`: job queue/status/events database
- `catalog/wdc_classes_catalog.json`: local WDC class catalog seed (offline fallback)
- `logs/webapp.log`, `logs/worker.log`: runtime logs

## Prerequisites

- Linux/macOS shell
- Python 3.8+
- Network access (for WDC and Wikidata queries)

## Installation

```bash
git clone <your-repo-url>
cd BEAM-App

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For tests:

```bash
pip install -r requirements-dev.txt
```

## Run The App (Recommended)

Start both worker + web app:

```bash
bash scripts/run_server.sh
```

Open:
- `http://localhost:8501` (local machine)
- or `http://<server-ip>:8501` (if running remotely and port is exposed)

Stop everything:

```bash
bash scripts/stop_server.sh
```

Restart everything:

```bash
bash scripts/restart_server.sh
```

## Run On A Remote Server (SSH Tunnel)

From your local machine:

```bash
ssh -L 8501:localhost:8501 [login]@[server]
```

Replace `[login]` and `[server]` with your SSH username and host.

Inside that SSH session (on remote server):

```bash
git clone <your-repo-url>
cd BEAM-App
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash scripts/run_server.sh
```

Back on your local machine, open:

```text
http://localhost:8501
```

## Manual Run (Without Helper Scripts)

Terminal 1:

```bash
python -m worker.run
```

Terminal 2:

```bash
uvicorn webapp.main:app --host 0.0.0.0 --port 8501
```

## First Functional Check (Fast Local Test Data)

Create tiny local classes so you can test quickly without large downloads.

Quick class:

```bash
python scripts/create_testclass_data.py
```

Larger local class:

```bash
python scripts/create_testclass_large_data.py
```

Create multiple matching-pattern classes (label, identifier, url, sameAs):

```bash
python scripts/create_matching_test_classes.py
```

Then open the web UI and choose a preset.

## Using The Web UI

Main form fields:
- `Class name`: WDC class (example: `City`, `Movie`, `Language`)
- `Parts spec`: `all`, list (`1,2,4`), or range (`1-10`)
- `WDC predicate pattern`: key/predicate hint (example: `name`, `eidr`, `telephone`, `sameAs`, `url`)
- `Wikidata property`: `Pxxxx`, `wdt:Pxxxx`, or `rdfs:label`
- `Wikidata class (QID)`: optional class filter, often used with label/link matching
- `Max depth`: bnode traversal depth (default `0`)
- `WDC values are Wikidata URLs`: enable for direct link-style matching (e.g. `sameAs`/`url` containing Wikidata URLs)
- `Ignore align cache`: force recompute alignment
- `Use local parts only`: do not download missing parts

Rules:
- If `WDC values are Wikidata URLs` is **off**, `Wikidata property` is required.
- If `WDC values are Wikidata URLs` is **on**, `wkd_class` is required.

## Built-In Presets

Current presets include:
- local tests (`TestClass*`)
- property matching (`Movie`, `CollegeOrUniversity`)
- label matching (`Language`)
- existing Wikidata links (`City` via `sameAs`)

All presets currently use:
- `parts_spec = all`
- `max_depth = 0`
- `force_align = false`
- `use_local_only = false`

## Job Lifecycle And Status

Job states:
- `queued`
- `running`
- `done`
- `error`
- `cancelled`

Subjobs:
- `align`
- `build`

Important behavior:
- If no alignments are found, build is skipped and the job is marked `error` with message:
  - `No alignments found (0); build skipped.`

## Output Layout

Alignment cache:
- `Download/<ClassName>/align_cache/<hash>/wdc_wikidata_links.tsv`
- `Download/<ClassName>/align_cache/<hash>/ALIGN_DONE`

Build directory:
- `data/<ClassName>/beam_<YYYYMMDD_HHMMSS>/BUILD_CONFIG.json`
- `data/<ClassName>/beam_<...>/BUILD_DONE`
- `data/<ClassName>/beam_<...>/without_link_code/*`
- `data/<ClassName>/beam_<...>/with_link_code/*`

Typical generated files in each variant:
- `ent_links`
- `attr_triples_1`
- `rel_triples_1`
- `attr_triples_2`
- `rel_triples_2`
- `prop_stats_wdc.tsv`
- `prop_stats_wd.tsv`

## API Endpoints (Operational)

UI/API:
- `GET /`
- `GET /api/dashboard`
- `GET /api/class_parts/{class_name}`
- `POST /jobs`
- `POST /jobs/{job_id}/cancel`
- `POST /jobs/{job_id}/cancel_subjob/{subjob_type}`
- `POST /jobs/{job_id}/rerun`
- `POST /jobs/{job_id}/rerun_nocache`
- `POST /jobs/{job_id}/rerun_align`
- `POST /jobs/{job_id}/rerun_build`
- `POST /jobs/{job_id}/delete`
- `GET /builds/{class_name}/{build_name}/download`
- `POST /builds/{class_name}/{build_name}/delete`
- `WS /ws/logs/{job_id}`

## Environment Variables

Worker/process control:
- `MAX_CONCURRENT_JOBS` (default: `8`)
- `JOB_POLL_INTERVAL` (default: `1` second)
- `MAX_WORKERS_PER_JOB` (default: `8`)
- `WDC_CLASSES_CATALOG_PATH` (optional custom path for local class catalog JSON)

Align/Wikidata query tuning:
- `ALIGN_MAX_WORKERS` (default: `8`)
- `WIKIDATA_QUERY_MAX_RETRIES` (default: `4`)
- `WIKIDATA_QUERY_RETRY_DELAY` (default: `2.0`)
- `WIKIDATA_QUERY_TIMEOUT` (default: `300`)

Example:

```bash
MAX_CONCURRENT_JOBS=4 JOB_POLL_INTERVAL=1 bash scripts/run_server.sh
```

## Advanced CLI Usage

Run alignment directly:

```bash
python scripts/align.py City all sameAs --wkd-class Q486972 --wdc-value-is-wikidata --ignore-chars "spaces;-;."
```

Run build directly from class links file:

```bash
python scripts/build_beam_files.py City --max-depth 0
```

Notes:
- `scripts/build_beam_files.py` expects `Download/<ClassName>/wdc_wikidata_links.tsv`.
- In normal app usage, use the web UI + worker; it handles the full flow and status tracking.

## Testing

Run all tests:

```bash
pytest -q
```

Key test modules:
- `tests/test_webapp_routes.py`
- `tests/test_pipeline.py`
- `tests/test_align.py`
- `tests/test_build_beam_files.py`
- `tests/test_worker_recovery.py`
- `tests/test_presets.py`

## Troubleshooting

`uvicorn not found`:
- install dependencies: `pip install -r requirements.txt`

Web UI opens but jobs do not progress:
- check worker process: `pgrep -af "python -m worker.run"`
- check `logs/worker.log`

Jobs stay stale in UI:
- refresh browser
- check `logs/webapp.log`
- verify websocket path `/ws/logs/{job_id}` is reachable

`Failed to fetch Wikidata values`:
- check network access to `https://query.wikidata.org/sparql`
- increase retry/timeout env vars
- use more specific `wkd_class` / property

`No local parts matched ... download is disabled`:
- uncheck `Use local parts only`
- or place required `part_*.nq` files under `Download/<ClassName>/`

WDC class refresh fails (Mannheim down):
- startup now uses local DB + `catalog/wdc_classes_catalog.json` (no auto-scrape)
- `GET /refresh_classes` is manual and keeps existing DB/catalog unchanged on failure

Build marked error with `No alignments found (0); build skipped.`:
- this means alignment produced zero links
- verify class/predicate/property/class filter combo
- try a known-good preset first

## Notes

- `app.py` (Streamlit) exists as legacy tooling; the supported UI is `webapp/main.py` with FastAPI.
- `jobs.db` can grow over time; clean old jobs/builds from the UI when needed.
