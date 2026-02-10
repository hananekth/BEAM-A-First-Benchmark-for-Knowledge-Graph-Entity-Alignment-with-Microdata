import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr

from beam import db
from beam.pipeline import generate_benchmark, PipelineError

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
POLL_INTERVAL = float(os.environ.get("JOB_POLL_INTERVAL", "2"))


def _cpu_workers_for(job_count):
    cpu = os.cpu_count() or 1
    active = max(1, job_count)
    return max(1, int((cpu * 0.8) / active))


def _run_job(job_id, workers):
    db.init_db()
    job = db.get_job(job_id)
    if not job:
        return
    params = job["params_json"]

    logs_dir = Path("jobs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"job_{job_id}.log"

    db.update_job(
        job_id,
        status="running",
        started_at=time.time(),
        log_path=str(log_path),
        error_message=None,
    )

    try:
        with open(log_path, "a", encoding="utf-8") as f, redirect_stdout(f), redirect_stderr(f):
            print(f"[JOB {job_id}] started")
            print(f"[JOB {job_id}] workers={workers}")
            result = generate_benchmark(json.loads(params), workers=workers)
            print(f"[JOB {job_id}] done")
        db.update_job(
            job_id,
            status="done",
            ended_at=time.time(),
            result_path=result.get("out_dir"),
        )
    except PipelineError as e:
        db.update_job(
            job_id,
            status="error",
            ended_at=time.time(),
            error_message=str(e),
        )
    except Exception as e:
        db.update_job(
            job_id,
            status="error",
            ended_at=time.time(),
            error_message=f"Unexpected error: {e}",
        )
        raise


def main():
    db.init_db()
    running = {}

    while True:
        # Clean finished processes
        finished = []
        for job_id, proc in running.items():
            if not proc.is_alive():
                proc.join(timeout=0.1)
                finished.append(job_id)
        for job_id in finished:
            running.pop(job_id, None)

        # Launch new jobs if capacity
        capacity = MAX_CONCURRENT_JOBS - len(running)
        if capacity > 0:
            queued = db.fetch_next_queued(limit=capacity)
            for row in queued:
                job_id = row["id"]
                workers = _cpu_workers_for(len(running) + 1)
                proc = mp.Process(target=_run_job, args=(job_id, workers), daemon=False)
                proc.start()
                running[job_id] = proc

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if sys.platform == "win32":
        mp.set_start_method("spawn")
    main()
