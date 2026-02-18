import json
import multiprocessing as mp
import os
import re
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr

from beam import db
from beam.pipeline import generate_benchmark, PipelineError, _config_hash

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "8"))
POLL_INTERVAL = float(os.environ.get("JOB_POLL_INTERVAL", "1"))
MAX_WORKERS_PER_JOB = int(os.environ.get("MAX_WORKERS_PER_JOB", "8"))
JOB_STUCK_TIMEOUT_S = int(os.environ.get("JOB_STUCK_TIMEOUT_S", os.environ.get("JOB_STUCK_TIMEOUT", "1800")))


def _normalize_eta_hint(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    compact = re.sub(r"\s+", "", raw.lower())
    if compact in {"n/a", "na", "-", "—"}:
        return None

    digits = re.findall(r"\d", compact)
    # Treat purely-zero ETA hints (0s, 0m0s, 0h00m00s, 00:00, etc.) as unknown.
    if digits and all(d == "0" for d in digits):
        tail = re.sub(r"[0-9:\.]", "", compact)
        if not tail or re.fullmatch(r"[hms]+", tail):
            return None
    return raw


def _format_eta_seconds(seconds):
    try:
        total = int(float(seconds))
    except Exception:
        return None
    if total <= 0:
        return None
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _extract_progress_pct(msg):
    if not msg:
        return None
    match = re.search(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%", str(msg))
    if not match:
        return None
    try:
        pct = float(match.group(1))
    except Exception:
        return None
    if pct < 0:
        return 0.0
    if pct > 100:
        return 100.0
    return pct


def _extract_batch_progress(msg):
    if not msg:
        return None, None
    match = re.search(r"\bbatches\s+(\d+)\s*/\s*(\d+)\b", str(msg), flags=re.IGNORECASE)
    if not match:
        return None, None
    try:
        done = int(match.group(1))
        total = int(match.group(2))
    except Exception:
        return None, None
    if total <= 0:
        return None, None
    done = max(0, min(done, total))
    return done, total


def _should_mark_job_stuck(last_activity_ts, now_ts, timeout_s):
    if timeout_s is None:
        return False
    try:
        timeout_s = int(timeout_s)
    except Exception:
        return False
    if timeout_s <= 0:
        return False
    try:
        last_ts = float(last_activity_ts or 0.0)
        now_ts = float(now_ts or 0.0)
    except Exception:
        return False
    if last_ts <= 0 or now_ts <= 0:
        return False
    return (now_ts - last_ts) >= timeout_s


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


def _error_if_active(job_id, subjob_type, reason, ended_at=None):
    row = db.get_subjob(job_id, subjob_type)
    if not row:
        return
    if row["status"] in ("done", "error", "cancelled", "interrupted"):
        return
    db.update_subjob(
        row["id"],
        status="error",
        ended_at=ended_at or time.time(),
        progress_text=str(reason or "Job marked as error"),
    )


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


def _safe_json_dumps(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return json.dumps({})


def _checkpoint_for_job(job):
    try:
        raw = job["checkpoint_json"] if job else None
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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
                checkpoint_json=None,
                checkpoint_at=None,
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
            ckpt = _checkpoint_for_job(job)
            job_result_path = ""
            try:
                job_result_path = str(job["result_path"] or "").strip()
            except Exception:
                job_result_path = ""
            resume_out_dir = (
                str(ckpt.get("resume_out_dir") or "").strip()
                or job_result_path
                or str(params.get("resume_out_dir") or "").strip()
            )
            restart_build_only = (job["phase"] == "build") and _align_cache_ready(params)
            if restart_build_only:
                params["require_cached_align"] = True
                params["skip_build"] = False
                params["force_align"] = False
                if resume_out_dir:
                    params["resume_build"] = True
                    params["resume_out_dir"] = resume_out_dir
                db.update_subjob_by_type(job_id, "align", status="done", ended_at=now, cancel_requested=0)
                db.update_subjob_by_type(job_id, "build", status="queued", started_at=None, ended_at=None, cancel_requested=0)
            else:
                params.pop("resume_build", None)
                params.pop("resume_out_dir", None)
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
                result_path=resume_out_dir if restart_build_only and resume_out_dir else None,
                error_message="Auto-requeued after restart",
                params_json=_safe_json_dumps(params),
                checkpoint_json=_safe_json_dumps(
                    {
                        "phase": "build" if restart_build_only else "align",
                        "step": "queued",
                        "resume_out_dir": resume_out_dir if restart_build_only and resume_out_dir else None,
                        "reason": "recovered_after_restart",
                        "ts": now,
                    }
                ) if restart_build_only else None,
                checkpoint_at=now if restart_build_only else None,
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


def _looks_like_skipped_build_reason(text):
    msg = str(text or "").strip().lower()
    if not msg:
        return False
    return ("build skipped" in msg) or ("no alignments found" in msg)


def _reconcile_legacy_skipped_build_jobs():
    """
    Legacy compatibility:
    old versions could persist jobs as done when build was skipped due to 0 alignments.
    Normalize those rows to error so UI state is consistent.
    """
    now = time.time()
    rows = list(db.list_jobs_by_status("done")) + list(db.list_jobs_by_status("error"))
    for job in rows:
        job_id = job["id"]
        build_row = db.get_subjob(job_id, "build")
        if not build_row:
            continue

        build_step = str(build_row["current_step"] or "").strip().lower()
        build_msg = str(build_row["progress_text"] or "").strip()
        job_msg = str(job["progress_text"] or "").strip()
        err_msg = str(job["error_message"] or "").strip()

        skipped = (
            build_step == "skipped"
            or _looks_like_skipped_build_reason(build_msg)
            or _looks_like_skipped_build_reason(job_msg)
            or _looks_like_skipped_build_reason(err_msg)
        )
        if not skipped:
            continue

        # Never rewrite true completed builds.
        result_path = str(job["result_path"] or "").strip()
        has_build_done = bool(result_path and (Path(result_path) / "BUILD_DONE").exists())
        if has_build_done and job["status"] == "done":
            continue

        reason = build_msg or job_msg or err_msg or "No alignments found (0); build skipped."
        if job["status"] == "done":
            db.update_job(
                job_id,
                status="error",
                phase="build",
                ended_at=job["ended_at"] or now,
                result_path=None if not has_build_done else result_path,
                progress_text=reason,
                error_message=reason,
            )
        if build_row["status"] != "error":
            db.update_subjob_by_type(
                job_id,
                "build",
                status="error",
                ended_at=build_row["ended_at"] or now,
                progress_text=reason,
                current_step="skipped",
            )
            db.insert_event(
                job_id,
                "system",
                "Reconciled legacy skipped build result to error state",
                phase="build",
                kind="reconcile",
                step="skipped",
                worker="build",
            )


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
    try:
        parsed_params = json.loads(params or "{}")
        if not isinstance(parsed_params, dict):
            parsed_params = {}
    except Exception:
        parsed_params = {}

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
                self._eta_by_phase = {"align": None, "build": None}
                self._eta_ts_by_phase = {"align": 0.0, "build": 0.0}
                self._build_batches_done = 0
                self._build_batches_total = 0
                self._build_batch_samples = deque(maxlen=20)
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
                pct = _extract_progress_pct(msg)
                kind = "progress" if (
                    (pct is not None)
                    or ("Progress:" in msg)
                    or ("ETA:" in msg)
                    or ("Lines read" in msg)
                ) else "log"
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
                        if pct is None:
                            db.update_job(self.jid, progress_text=msg)
                            db.update_subjob_by_type(self.jid, self._phase, progress_text=msg)
                        else:
                            db.update_job(self.jid, progress_text=msg, progress_pct=pct)
                            db.update_subjob_by_type(self.jid, self._phase, progress_text=msg, progress_pct=pct)
                    except Exception:
                        pass
                if self._phase == "build":
                    done_batches, total_batches = _extract_batch_progress(msg)
                    if done_batches is not None and total_batches is not None:
                        if total_batches != self._build_batches_total or done_batches < self._build_batches_done:
                            self._build_batch_samples.clear()
                        self._build_batches_done = done_batches
                        self._build_batches_total = total_batches
                        if done_batches > 0:
                            if not self._build_batch_samples or done_batches > self._build_batch_samples[-1][1]:
                                self._build_batch_samples.append((time.time(), done_batches))
                        if 0 < done_batches < total_batches and len(self._build_batch_samples) >= 2:
                            t0, d0 = self._build_batch_samples[0]
                            t1, d1 = self._build_batch_samples[-1]
                            if d1 > d0 and t1 > t0:
                                rate = (d1 - d0) / max(0.001, t1 - t0)
                                remaining = max(0, total_batches - done_batches)
                                if rate > 0 and remaining > 0:
                                    eta_txt = _format_eta_seconds(remaining / rate)
                                    eta_txt = _normalize_eta_hint(eta_txt)
                                    if eta_txt:
                                        self._eta_by_phase["build"] = eta_txt
                                        self._eta_ts_by_phase["build"] = time.time()
                        elif done_batches >= total_batches:
                            self._eta_by_phase["build"] = None
                            self._eta_ts_by_phase["build"] = 0.0
                eta_match = re.search(r"ETA:\s*([^|]+)", msg, flags=re.IGNORECASE)
                if eta_match:
                    eta_txt = _normalize_eta_hint(eta_match.group(1))
                    if eta_txt:
                        self._eta_by_phase[self._phase] = str(eta_txt)
                        self._eta_ts_by_phase[self._phase] = time.time()
                    else:
                        # Avoid stale/meaningless ETA values while the phase continues.
                        self._eta_by_phase[self._phase] = None
                        self._eta_ts_by_phase[self._phase] = 0.0
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
                elif self._phase == "build" and msg.startswith("[WDC]"):
                    step = "build_wdc"
                elif self._phase == "build" and msg.startswith("[WD]"):
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
        checkpoint_lock = threading.Lock()
        checkpoint_state = {"last_saved_at": 0.0}
        resume_out_dir_ref = {"path": str(parsed_params.get("resume_out_dir") or "").strip()}

        def _clear_resume_flags():
            parsed_params.pop("resume_build", None)
            parsed_params.pop("resume_out_dir", None)
            parsed_params.pop("resume_checkpoint_at", None)
            parsed_params.pop("resume_checkpoint_step", None)
            parsed_params.pop("resume_checkpoint_reason", None)

        def _save_build_checkpoint(reason="heartbeat", force=False):
            out_dir = str(resume_out_dir_ref.get("path") or "").strip()
            if not out_dir:
                return
            now = time.time()
            with checkpoint_lock:
                if not force and reason == "heartbeat":
                    if (now - checkpoint_state["last_saved_at"]) < 60.0:
                        return
                parsed_params["resume_build"] = True
                parsed_params["resume_out_dir"] = out_dir
                parsed_params["require_cached_align"] = True
                parsed_params["skip_build"] = False
                parsed_params["force_align"] = False
                parsed_params["resume_checkpoint_at"] = now
                parsed_params["resume_checkpoint_step"] = writer._current_step or "build"
                parsed_params["resume_checkpoint_reason"] = reason
                checkpoint_payload = {
                    "phase": "build",
                    "step": writer._current_step or "build",
                    "current_file": writer._current_file,
                    "resume_out_dir": out_dir,
                    "reason": reason,
                    "ts": now,
                }
                db.update_job(
                    job_id,
                    checkpoint_json=_safe_json_dumps(checkpoint_payload),
                    checkpoint_at=now,
                    result_path=out_dir,
                    params_json=_safe_json_dumps(parsed_params),
                )
                checkpoint_state["last_saved_at"] = now

        def _on_pipeline_checkpoint(payload):
            if not isinstance(payload, dict):
                return
            if payload.get("kind") != "build_started":
                return
            out_dir = str(payload.get("out_dir") or "").strip()
            if out_dir:
                resume_out_dir_ref["path"] = out_dir
            _save_build_checkpoint(reason="build_started", force=True)
            db.insert_event(job_id, "system", f"Checkpoint saved for build restart ({out_dir})")

        with redirect_stdout(writer), redirect_stderr(writer):
            print(f"[JOB {job_id}] started")
            print(f"[JOB {job_id}] workers={workers}")

            heartbeat_stop = threading.Event()

            def heartbeat_loop():
                while not heartbeat_stop.wait(5.0):
                    if writer._phase != "build":
                        continue
                    now = time.time()
                    _save_build_checkpoint(reason="heartbeat", force=False)
                    quiet_s = int(now - writer._last_emit_ts)
                    if quiet_s < 15:
                        continue
                    if (now - writer._last_heartbeat_ts) < 10:
                        continue
                    phase_elapsed = int(now - writer._phase_started_at)
                    step = writer._current_step or "build"
                    eta_hint = _normalize_eta_hint(writer._eta_by_phase.get("build"))
                    eta_ts = float(writer._eta_ts_by_phase.get("build") or 0.0)
                    eta_age_ok = eta_ts > 0 and (now - eta_ts) <= 45.0
                    eta_suffix = f" | ETA: {eta_hint}" if (eta_hint and eta_age_ok) else ""
                    msg = f"[HB] build active | step={step} | phase_elapsed={phase_elapsed}s | quiet={quiet_s}s{eta_suffix}"
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
                writer._eta_by_phase[phase] = None
                writer._eta_ts_by_phase[phase] = 0.0
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
                    _save_build_checkpoint(reason="phase_build", force=True)

            try:
                result = generate_benchmark(
                    parsed_params,
                    workers=workers,
                    should_cancel=should_cancel,
                    set_phase=set_phase,
                    should_skip_build=should_skip_build,
                    on_checkpoint=_on_pipeline_checkpoint,
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
            _clear_resume_flags()
            db.update_job(
                job_id,
                status="error",
                ended_at=time.time(),
                result_path=None,
                align_dir=result.get("align_dir"),
                reused_align=1 if result.get("reused_align") else 0,
                progress_text=reason,
                error_message=reason,
                phase="build",
                params_json=_safe_json_dumps(parsed_params),
                checkpoint_json=None,
                checkpoint_at=None,
            )
            db.update_subjob_by_type(
                job_id,
                "build",
                status="error",
                ended_at=time.time(),
                progress_text=reason,
                current_step="skipped",
            )
            return
        if result.get("build_cancelled"):
            _cancel_if_active(job_id, "build")
            _clear_resume_flags()
            db.update_job(
                job_id,
                status="cancelled",
                ended_at=time.time(),
                result_path=None,
                align_dir=result.get("align_dir"),
                reused_align=1 if result.get("reused_align") else 0,
                error_message="Build cancelled by user",
                params_json=_safe_json_dumps(parsed_params),
                checkpoint_json=None,
                checkpoint_at=None,
            )
            return
        _clear_resume_flags()
        db.update_job(
            job_id,
            status="done",
            ended_at=time.time(),
            result_path=result.get("out_dir"),
            align_dir=result.get("align_dir"),
            reused_align=1 if result.get("reused_align") else 0,
            params_json=_safe_json_dumps(parsed_params),
            checkpoint_json=None,
            checkpoint_at=None,
        )
        db.update_subjob_by_type(job_id, "build", status="done", ended_at=time.time())
    except PipelineError as e:
        if "Cancelled" in str(e):
            align_cancel = db.get_cancel_requested_subjob(job_id, "align")
            build_cancel = db.get_cancel_requested_subjob(job_id, "build")
            _clear_resume_flags()
            db.update_job(
                job_id,
                status="cancelled",
                ended_at=time.time(),
                error_message=str(e),
                params_json=_safe_json_dumps(parsed_params),
                checkpoint_json=None,
                checkpoint_at=None,
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
        _clear_resume_flags()
        db.update_job(
            job_id,
            status="error",
            ended_at=time.time(),
            error_message=str(e),
            params_json=_safe_json_dumps(parsed_params),
            checkpoint_json=None,
            checkpoint_at=None,
        )
        db.update_subjob_by_type(job_id, "align", status="error", ended_at=time.time())
        db.update_subjob_by_type(job_id, "build", status="error", ended_at=time.time())
    except Exception as e:
        _clear_resume_flags()
        db.update_job(
            job_id,
            status="error",
            ended_at=time.time(),
            error_message=f"Unexpected error: {e}",
            params_json=_safe_json_dumps(parsed_params),
            checkpoint_json=None,
            checkpoint_at=None,
        )
        db.update_subjob_by_type(job_id, "align", status="error", ended_at=time.time())
        db.update_subjob_by_type(job_id, "build", status="error", ended_at=time.time())
        raise


def main():
    db.init_db()
    _reconcile_terminal_subjobs()
    _reconcile_legacy_skipped_build_jobs()
    _recover_stale_running_jobs()
    running = {}

    while True:
        # Clean finished processes
        finished = []
        now_loop = time.time()
        for job_id, proc in running.items():
            job = db.get_job(job_id)
            if not job:
                # Job disappeared from DB; stop tracking and terminate process if still alive.
                if proc.is_alive():
                    _terminate_process_tree(proc, grace_s=0.5)
                finished.append(job_id)
                continue

            latest_event_ts = float(db.get_latest_event_ts(job_id) or 0.0)
            last_activity_ts = max(
                latest_event_ts,
                float(job["checkpoint_at"] or 0.0),
                float(job["started_at"] or 0.0),
                float(job["created_at"] or 0.0),
            )
            if job["status"] == "running" and _should_mark_job_stuck(
                last_activity_ts,
                now_loop,
                JOB_STUCK_TIMEOUT_S,
            ):
                quiet_s = int(max(0.0, now_loop - last_activity_ts))
                reason = (
                    f"Job marked as stuck after {quiet_s}s without activity "
                    f"(timeout={JOB_STUCK_TIMEOUT_S}s)"
                )
                _terminate_process_tree(proc, grace_s=0.5)
                db.insert_event(
                    job_id,
                    "system",
                    reason,
                    phase=job["phase"],
                    kind="stuck",
                    step=job["current_step"],
                    worker=job["phase"] or "system",
                )
                db.update_job(
                    job_id,
                    status="error",
                    ended_at=now_loop,
                    progress_text=reason,
                    error_message=reason,
                    checkpoint_json=None,
                    checkpoint_at=None,
                )
                _error_if_active(job_id, "align", reason, ended_at=now_loop)
                _error_if_active(job_id, "build", reason, ended_at=now_loop)
                finished.append(job_id)
                continue

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
                        checkpoint_json=None,
                        checkpoint_at=None,
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
                    checkpoint_json=None,
                    checkpoint_at=None,
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
