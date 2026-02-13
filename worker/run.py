import json
import multiprocessing as mp
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr

from beam import db
from beam.pipeline import generate_benchmark, PipelineError, _config_hash

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
POLL_INTERVAL = float(os.environ.get("JOB_POLL_INTERVAL", "2"))
MAX_WORKERS_PER_JOB = int(os.environ.get("MAX_WORKERS_PER_JOB", "8"))


def _cpu_workers_for(job_count):
    cpu = os.cpu_count() or 1
    active = max(1, job_count)
    workers = max(1, int((cpu * 0.8) / active))
    return max(1, min(workers, MAX_WORKERS_PER_JOB))


def _terminate_process_tree(proc, grace_s=0.5):
    """Best-effort termination for a worker process and its process group."""
    if not proc:
        return True
    if not proc.is_alive():
        return True

    # Try graceful stop on the full process group first.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass

    deadline = time.time() + max(0.1, grace_s)
    while proc.is_alive() and time.time() < deadline:
        time.sleep(0.05)

    if not proc.is_alive():
        return True

    # Escalate to SIGKILL if still running.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except Exception:
            pass

    proc.join(timeout=0.2)
    return not proc.is_alive()


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _terminate_by_ids(pid=None, pgid=None, grace_s=0.5):
    if not pid and not pgid:
        return True
    try:
        if pgid:
            os.killpg(int(pgid), signal.SIGTERM)
        elif pid:
            os.kill(int(pid), signal.SIGTERM)
    except Exception:
        pass
    deadline = time.time() + max(0.1, grace_s)
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    try:
        if pgid:
            os.killpg(int(pgid), signal.SIGKILL)
        elif pid:
            os.kill(int(pid), signal.SIGKILL)
    except Exception:
        pass
    time.sleep(0.1)
    return not _pid_alive(pid)


def _cancel_if_active(job_id, subjob_type):
    row = db.get_subjob(job_id, subjob_type)
    if not row:
        return
    if row["status"] in ("done", "error", "cancelled", "interrupted"):
        return
    db.update_subjob(row["id"], status="cancelled", ended_at=time.time())


def _align_cache_ready(params):
    try:
        class_name = params.get("class_name")
        if not class_name:
            return False
        align_params = {
            "class_name": class_name,
            "parts_spec": params.get("parts_spec") or "all",
            "pattern": params.get("wdc_predicate_pattern"),
            "wikidata_property": params.get("wikidata_property") or None,
            "wkd_class": params.get("wkd_class") or None,
            "ignore_chars": params.get("ignore_chars") or None,
            "wdc_value_is_wikidata": bool(params.get("wdc_value_is_wikidata")),
        }
        cache_hash = _config_hash(align_params)
        cache_dir = Path("Download") / class_name / "align_cache" / cache_hash
        return (cache_dir / "ALIGN_DONE").exists() and (cache_dir / "wdc_wikidata_links.tsv").exists()
    except Exception:
        return False


def _recover_stale_running_jobs():
    """Recover jobs left in running state after worker/server restarts."""
    now = time.time()
    stale = db.list_jobs_by_status("running")
    for job in stale:
        job_id = job["id"]
        pid = job["job_pid"]
        pgid = job["job_pgid"]
        alive = _pid_alive(pid)

        if alive and job["cancel_requested"]:
            _terminate_by_ids(pid=pid, pgid=pgid, grace_s=0.5)
            alive = _pid_alive(pid)

        if alive:
            # Process is still alive but orphaned from this worker loop; stop it for deterministic recovery.
            _terminate_by_ids(pid=pid, pgid=pgid, grace_s=0.5)

        if job["cancel_requested"]:
            db.update_job(
                job_id,
                status="cancelled",
                ended_at=now,
                error_message="Cancelled (recovered after restart)",
            )
            _cancel_if_active(job_id, "align")
            _cancel_if_active(job_id, "build")
            db.insert_event(job_id, "system", "Recovered stale running state after restart (cancelled)")
        else:
            # Auto-restart non-cancelled jobs.
            try:
                params = json.loads(job["params_json"] or "{}")
            except Exception:
                params = {}
            restart_build_only = (job["phase"] == "build") and _align_cache_ready(params)
            if restart_build_only:
                params["require_cached_align"] = True
                params["skip_build"] = False
                params["force_align"] = False
                db.update_subjob_by_type(job_id, "align", status="done", ended_at=now, cancel_requested=0)
                db.update_subjob_by_type(job_id, "build", status="queued", started_at=None, ended_at=None, cancel_requested=0)
            else:
                db.update_subjob_by_type(job_id, "align", status="queued", started_at=None, ended_at=None, cancel_requested=0)
                db.update_subjob_by_type(job_id, "build", status="queued", started_at=None, ended_at=None, cancel_requested=0)
            db.update_job(
                job_id,
                status="queued",
                phase=None,
                cancel_requested=0,
                started_at=None,
                ended_at=None,
                interrupted=1,
                progress_text=None,
                progress_pct=None,
                current_step=None,
                current_file=None,
                job_pid=None,
                job_pgid=None,
                result_path=None,
                error_message="Auto-requeued after restart",
                params_json=json.dumps(params),
            )
            if restart_build_only:
                db.insert_event(job_id, "system", "Recovered stale running state after restart (auto-requeued: build)")
            else:
                db.insert_event(job_id, "system", "Recovered stale running state after restart (auto-requeued: full)")


def _reconcile_terminal_subjobs():
    """Ensure terminal job states are reflected on subjobs after restarts/code upgrades."""
    terminal_statuses = ("error", "cancelled", "interrupted")
    now = time.time()
    for status in terminal_statuses:
        rows = db.list_jobs_by_status(status)
        for job in rows:
            job_id = job["id"]
            for sj in db.list_subjobs(job_id):
                if sj["status"] in ("queued", "running"):
                    db.update_subjob(sj["id"], status=status, ended_at=now)


def _run_job(job_id, workers):
    # Make this process a new process group so we can kill the whole tree
    try:
        os.setsid()
    except Exception:
        pass
    db.init_db()
    job = db.get_job(job_id)
    if not job:
        return
    params = job["params_json"]
    parsed_params = json.loads(params)

    db.update_job(
        job_id,
        status="running",
        started_at=time.time(),
        log_path=None,
        error_message=None,
        phase="align",
        cancel_requested=0,
        progress_text=None,
        progress_pct=None,
        job_pgid=os.getpid(),
    )
    build_row = db.get_subjob(job_id, "build")
    build_cancel_requested = 1 if (build_row and build_row["cancel_requested"]) else 0
    build_initial_status = "cancelled" if build_cancel_requested else "queued"
    build_only_mode = bool(parsed_params.get("require_cached_align"))
    db.update_subjob_by_type(
        job_id,
        "align",
        status="done" if build_only_mode else "running",
        started_at=time.time() if not build_only_mode else None,
        ended_at=time.time() if build_only_mode else None,
        progress_text="Using cached alignment" if build_only_mode else None,
        progress_pct=None,
        current_step=None,
        current_file=None,
        cancel_requested=0,
    )
    db.update_subjob_by_type(
        job_id,
        "build",
        status=build_initial_status,
        started_at=None,
        ended_at=time.time() if build_initial_status == "cancelled" else None,
        progress_text=None,
        progress_pct=None,
        current_step=None,
        current_file=None,
        cancel_requested=build_cancel_requested,
    )
    # If already cancelled before start, exit early
    if db.get_cancel_requested(job_id) or (db.get_job(job_id)["status"] == "cancelled"):
        db.update_job(job_id, status="cancelled", ended_at=time.time(), error_message="Cancelled before start")
        _cancel_if_active(job_id, "align")
        _cancel_if_active(job_id, "build")
        return

    try:
        class DbWriter:
            def __init__(self, jid):
                self.jid = jid
                self._buf = ""
                self._phase = "align"
                self._last_logged_msg = None
                self._last_download_pct_logged = None
                self._last_emit_ts = time.time()
                self._last_heartbeat_ts = 0.0
                self._phase_started_at = time.time()
                self._current_step = None
                self._current_file = None
                self._last_step_key = None
                self._last_scan_pct_logged = None
                self._last_scan_log_ts = 0.0
                self._translations = (
                    ("Téléchargement/Décompression", "Download/Decompress"),
                    ("Téléchargement depuis", "Downloading from"),
                    ("Téléchargement:", "Download:"),
                    ("Déjà disponible", "Already available"),
                    (".gz supprimé (déjà décompressé)", ".gz removed (already decompressed)"),
                    ("Déjà téléchargé", "Already downloaded"),
                    ("Téléchargé", "Downloaded"),
                    ("Décompression...", "Decompressing..."),
                    ("Décompressé", "Decompressed"),
                    ("Erreur décompression", "Decompression error"),
                    ("Erreur:", "Error:"),
                    ("Extraction directe", "Direct extraction"),
                    ("Récupération des valeurs Wikidata", "Fetching Wikidata values"),
                    ("Linking WDC", "Linking WDC"),
                    ("Export des résultats", "Exporting results"),
                    ("Lignes lues", "Lines read"),
                    ("Valeurs distinctes", "Distinct values"),
                    ("parts sélectionnées", "parts selected"),
                )

            def _to_english(self, msg):
                out = msg
                for src, dst in self._translations:
                    out = out.replace(src, dst)
                return out

            def _emit_event(self, msg, kind="log", step=None, pct=None, worker=None, meta=None):
                try:
                    db.insert_event(
                        self.jid,
                        "log",
                        msg,
                        phase=self._phase,
                        kind=kind,
                        step=step,
                        worker=worker or self._phase,
                        progress_pct=pct,
                        meta=meta,
                    )
                except Exception:
                    # Logging must never break the running pipeline.
                    pass

            def write(self, data):
                if not data:
                    return
                self._buf += data.replace("\r", "\n")
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    self._emit(line)

            def flush(self):
                if self._buf:
                    self._emit(self._buf)
                    self._buf = ""

            def _emit(self, line):
                msg = line.strip()
                if not msg:
                    return
                msg = self._to_english(msg)
                self._last_emit_ts = time.time()
                m = re.search(r"(\\d{1,3}\\.\\d)%", msg)
                pct = float(m.group(1)) if m else None
                kind = "progress" if (m or ("Progress:" in msg) or ("ETA:" in msg) or ("Lines read" in msg)) else "log"
                # Throttle very chatty scanner lines to reduce DB pressure.
                should_emit = True
                if msg.startswith("Lines read:"):
                    now = time.time()
                    if self._last_scan_pct_logged is not None and pct is not None:
                        pct_delta = pct - self._last_scan_pct_logged
                    else:
                        pct_delta = None
                    if (pct_delta is not None and pct_delta < 0.2) and (now - self._last_scan_log_ts) < 1.0:
                        should_emit = False
                    else:
                        if pct is not None:
                            self._last_scan_pct_logged = pct
                        self._last_scan_log_ts = now
                # Keep raw logs cleaner: throttle very chatty download progress lines.
                if msg.startswith("Download:") and pct is not None:
                    if self._last_download_pct_logged is not None and (pct - self._last_download_pct_logged) < 0.5:
                        pass
                    else:
                        if should_emit:
                            self._emit_event(msg, kind=kind, pct=pct)
                        self._last_download_pct_logged = pct
                elif msg != self._last_logged_msg:
                    if should_emit:
                        self._emit_event(msg, kind=kind, pct=pct)
                self._last_logged_msg = msg
                # update progress if line looks like progress
                if kind == "progress":
                    try:
                        db.update_job(self.jid, progress_text=msg, progress_pct=pct)
                        db.update_subjob_by_type(self.jid, self._phase, progress_text=msg, progress_pct=pct)
                    except Exception:
                        pass
                # step detection
                step = None
                current_file = None
                if ("Download/Decompress" in msg) or ("Téléchargement/Décompression" in msg):
                    step = "download"
                elif ("Direct extraction" in msg) or ("Extraction directe" in msg):
                    step = "extract"
                elif "Scan:" in msg:
                    step = "scan"
                    m2 = re.search(r"Scan:\\s*(.+)$", msg)
                    if m2:
                        current_file = m2.group(1).strip()
                elif ("Fetching Wikidata values" in msg) or ("Récupération des valeurs Wikidata" in msg):
                    step = "wikidata"
                elif "Linking WDC" in msg:
                    step = "linking"
                elif ("Exporting results" in msg) or ("Export des résultats" in msg):
                    step = "export"
                elif "[WDC] depth" in msg:
                    step = "build_wdc"
                elif "[WD] batch" in msg:
                    step = "build_wd"
                if step or current_file:
                    if step:
                        self._current_step = step
                    if current_file:
                        self._current_file = current_file
                    try:
                        db.update_job(self.jid, current_step=step, current_file=current_file)
                        db.update_subjob_by_type(self.jid, self._phase, current_step=step, current_file=current_file)
                    except Exception:
                        pass
                    step_key = (self._phase, self._current_step, self._current_file)
                    if step_key != self._last_step_key:
                        self._emit_event(
                            msg,
                            kind="step",
                            step=self._current_step,
                            pct=pct,
                            meta={"current_file": self._current_file} if self._current_file else None,
                        )
                        self._last_step_key = step_key

        writer = DbWriter(job_id)
        with redirect_stdout(writer), redirect_stderr(writer):
            print(f"[JOB {job_id}] started")
            print(f"[JOB {job_id}] workers={workers}")

            heartbeat_stop = threading.Event()

            def heartbeat_loop():
                while not heartbeat_stop.wait(5.0):
                    if writer._phase != "build":
                        continue
                    now = time.time()
                    quiet_s = int(now - writer._last_emit_ts)
                    if quiet_s < 15:
                        continue
                    if (now - writer._last_heartbeat_ts) < 10:
                        continue
                    phase_elapsed = int(now - writer._phase_started_at)
                    step = writer._current_step or "build"
                    msg = f"[HB] build active | step={step} | phase_elapsed={phase_elapsed}s | quiet={quiet_s}s"
                    try:
                        db.insert_event(
                            writer.jid,
                            "log",
                            msg,
                            phase="build",
                            kind="heartbeat",
                            step=step,
                            worker="build",
                        )
                        db.update_job(writer.jid, progress_text=msg)
                        db.update_subjob_by_type(writer.jid, "build", progress_text=msg)
                    except Exception:
                        pass
                    writer._last_heartbeat_ts = now

            hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
            hb_thread.start()

            def should_cancel():
                # Full job cancellation or align cancellation always stops the whole pipeline.
                if db.get_cancel_requested(job_id) or db.get_cancel_requested_subjob(job_id, "align"):
                    return True
                # Build cancellation only interrupts once build phase starts.
                if writer._phase == "build" and db.get_cancel_requested_subjob(job_id, "build"):
                    return True
                return False

            def should_skip_build():
                return db.get_cancel_requested_subjob(job_id, "build")

            def set_phase(phase):
                writer._phase = phase
                writer._phase_started_at = time.time()
                writer._last_heartbeat_ts = 0.0
                try:
                    db.update_job(job_id, phase=phase)
                    db.insert_event(job_id, "system", f"Phase switched to {phase}", phase=phase, kind="phase", step=phase, worker=phase)
                except Exception:
                    pass
                if phase == "align":
                    db.update_subjob_by_type(job_id, "align", status="running", started_at=time.time())
                if phase == "build":
                    align_row = db.get_subjob(job_id, "align")
                    if align_row and align_row["status"] == "running":
                        db.update_subjob(align_row["id"], status="done", ended_at=time.time())
                    db.update_subjob_by_type(job_id, "build", status="running", started_at=time.time())

            try:
                result = generate_benchmark(
                    parsed_params,
                    workers=workers,
                    should_cancel=should_cancel,
                    set_phase=set_phase,
                    should_skip_build=should_skip_build,
                )
            finally:
                heartbeat_stop.set()
                hb_thread.join(timeout=1.0)
            print(f"[JOB {job_id}] done")
        align_row = db.get_subjob(job_id, "align")
        if align_row and align_row["status"] != "done":
            db.update_subjob(align_row["id"], status="done", ended_at=time.time())
        if result.get("build_skipped"):
            reason = result.get("build_skip_reason") or "Build skipped."
            db.insert_event(job_id, "system", reason, phase="build", kind="skip", step="build", worker="build")
            db.update_job(
                job_id,
                status="done",
                ended_at=time.time(),
                result_path=None,
                align_dir=result.get("align_dir"),
                reused_align=1 if result.get("reused_align") else 0,
                progress_text=reason,
                phase="build",
            )
            db.update_subjob_by_type(
                job_id,
                "build",
                status="done",
                ended_at=time.time(),
                progress_text=reason,
                current_step="skipped",
            )
            return
        if result.get("build_cancelled"):
            _cancel_if_active(job_id, "build")
            db.update_job(
                job_id,
                status="cancelled",
                ended_at=time.time(),
                result_path=None,
                align_dir=result.get("align_dir"),
                reused_align=1 if result.get("reused_align") else 0,
                error_message="Build cancelled by user",
            )
            return
        db.update_job(
            job_id,
            status="done",
            ended_at=time.time(),
            result_path=result.get("out_dir"),
            align_dir=result.get("align_dir"),
            reused_align=1 if result.get("reused_align") else 0,
        )
        db.update_subjob_by_type(job_id, "build", status="done", ended_at=time.time())
    except PipelineError as e:
        if "Cancelled" in str(e):
            align_cancel = db.get_cancel_requested_subjob(job_id, "align")
            build_cancel = db.get_cancel_requested_subjob(job_id, "build")
            db.update_job(
                job_id,
                status="cancelled",
                ended_at=time.time(),
                error_message=str(e),
            )
            if align_cancel:
                _cancel_if_active(job_id, "align")
                _cancel_if_active(job_id, "build")
            elif build_cancel:
                _cancel_if_active(job_id, "build")
            else:
                _cancel_if_active(job_id, "align")
                _cancel_if_active(job_id, "build")
            return
        db.update_job(
            job_id,
            status="error",
            ended_at=time.time(),
            error_message=str(e),
        )
        db.update_subjob_by_type(job_id, "align", status="error", ended_at=time.time())
        db.update_subjob_by_type(job_id, "build", status="error", ended_at=time.time())
    except Exception as e:
        db.update_job(
            job_id,
            status="error",
            ended_at=time.time(),
            error_message=f"Unexpected error: {e}",
        )
        db.update_subjob_by_type(job_id, "align", status="error", ended_at=time.time())
        db.update_subjob_by_type(job_id, "build", status="error", ended_at=time.time())
        raise


def main():
    db.init_db()
    _reconcile_terminal_subjobs()
    _recover_stale_running_jobs()
    running = {}

    while True:
        # Clean finished processes
        finished = []
        for job_id, proc in running.items():
            job = db.get_job(job_id)
            build_cancel_now = bool(job and job["phase"] == "build" and db.get_cancel_requested_subjob(job_id, "build"))
            align_cancel_now = db.get_cancel_requested_subjob(job_id, "align")
            if db.get_cancel_requested(job_id) or align_cancel_now or build_cancel_now:
                stopped = _terminate_process_tree(proc, grace_s=0.5)
                if stopped:
                    if align_cancel_now:
                        _cancel_if_active(job_id, "align")
                        _cancel_if_active(job_id, "build")
                    elif build_cancel_now:
                        _cancel_if_active(job_id, "build")
                    else:
                        _cancel_if_active(job_id, "align")
                        _cancel_if_active(job_id, "build")
                    db.update_job(
                        job_id,
                        status="cancelled",
                        ended_at=time.time(),
                        error_message="Cancelled by user",
                    )
                    finished.append(job_id)
                else:
                    db.insert_event(job_id, "system", "Cancellation requested; waiting for process to stop")
                continue
            if not proc.is_alive():
                proc.join(timeout=0.1)
                finished.append(job_id)
        for job_id in finished:
            running.pop(job_id, None)
            job = db.get_job(job_id)
            if job and job["status"] == "running":
                db.update_job(
                    job_id,
                    status="interrupted",
                    ended_at=time.time(),
                    error_message="Job interrupted (process stopped)",
                    interrupted=1,
                )

        # Launch new jobs if capacity
        capacity = MAX_CONCURRENT_JOBS - len(running)
        if capacity > 0:
            queued = db.fetch_next_queued(limit=capacity)
            for row in queued:
                job_id = row["id"]
                if db.get_cancel_requested(job_id):
                    db.update_job(
                        job_id,
                        status="cancelled",
                        ended_at=time.time(),
                        error_message="Cancelled before start",
                    )
                    _cancel_if_active(job_id, "align")
                    _cancel_if_active(job_id, "build")
                    continue
                workers = _cpu_workers_for(len(running) + 1)
                proc = mp.Process(target=_run_job, args=(job_id, workers), daemon=False)
                proc.start()
                db.update_job(job_id, job_pid=proc.pid)
                running[job_id] = proc

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if sys.platform == "win32":
        mp.set_start_method("spawn")
    main()
