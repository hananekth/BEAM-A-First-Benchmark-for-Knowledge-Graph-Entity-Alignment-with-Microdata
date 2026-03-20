import json
import time
from pathlib import Path

from beam import db
import worker.run as worker_run


def test_normalize_eta_hint_rejects_zero_like_values():
    assert worker_run._normalize_eta_hint("0s") is None
    assert worker_run._normalize_eta_hint("0m0s") is None
    assert worker_run._normalize_eta_hint("0h00m00s") is None
    assert worker_run._normalize_eta_hint("00:00") is None
    assert worker_run._normalize_eta_hint("N/A") is None
    assert worker_run._normalize_eta_hint("—") is None
    assert worker_run._normalize_eta_hint("12s") == "12s"
    assert worker_run._normalize_eta_hint("1m 03s") == "1m 03s"


def test_extract_progress_pct_parses_integer_and_decimal():
    assert worker_run._extract_progress_pct("Progress: 99.7% | ETA: 5s") == 99.7
    assert worker_run._extract_progress_pct("done 100%") == 100.0
    assert worker_run._extract_progress_pct("nothing here") is None


def test_extract_batch_progress_parses_done_total():
    done, total = worker_run._extract_batch_progress("[WD] Progress: batches 37/240 | 15.4% | ETA: 2m30s")
    assert done == 37
    assert total == 240
    done2, total2 = worker_run._extract_batch_progress("[HB] build active")
    assert done2 is None
    assert total2 is None


def test_should_mark_job_stuck():
    now = time.time()
    assert worker_run._should_mark_job_stuck(now - 200, now, 180) is True
    assert worker_run._should_mark_job_stuck(now - 120, now, 180) is False
    assert worker_run._should_mark_job_stuck(None, now, 180) is False


def test_recover_stale_running_build_requeues_with_resume(monkeypatch):
    class_name = "TestClassRecover"
    out_dir = Path("data") / class_name / "beam_resume_target"
    out_dir.mkdir(parents=True, exist_ok=True)

    params = {
        "class_name": class_name,
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "P31",
        "wkd_class": "Q515",
        "use_local_only": True,
    }
    job_id = db.insert_job(params)
    db.update_job(
        job_id,
        status="running",
        phase="build",
        params_json=json.dumps(params),
        job_pid=999999,
        job_pgid=999999,
        checkpoint_json=json.dumps({"phase": "build", "resume_out_dir": str(out_dir), "step": "build_wd"}),
        checkpoint_at=time.time(),
        result_path=str(out_dir),
    )
    db.update_subjob_by_type(job_id, "align", status="running", started_at=time.time())
    db.update_subjob_by_type(job_id, "build", status="running", started_at=time.time())

    monkeypatch.setattr(worker_run, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(worker_run, "_align_cache_ready", lambda p: True)

    worker_run._recover_stale_running_jobs()

    job = db.get_job(job_id)
    assert job["status"] == "queued"
    assert job["interrupted"] == 1
    assert job["result_path"] == str(out_dir)
    assert job["checkpoint_json"] is not None
    assert job["checkpoint_at"] is not None

    updated_params = json.loads(job["params_json"])
    assert updated_params["require_cached_align"] is True
    assert updated_params["resume_build"] is True
    assert updated_params["resume_out_dir"] == str(out_dir)
    assert updated_params["force_align"] is False

    align_row = db.get_subjob(job_id, "align")
    build_row = db.get_subjob(job_id, "build")
    assert align_row["status"] == "done"
    assert build_row["status"] == "queued"


def test_recover_stale_running_cancelled_job(monkeypatch):
    job_id = db.insert_job({"class_name": "TestClass", "parts_spec": "all"})
    db.update_job(
        job_id,
        status="running",
        phase="build",
        cancel_requested=1,
        job_pid=999999,
        job_pgid=999999,
        checkpoint_json=json.dumps({"phase": "build", "resume_out_dir": "data/TestClass/beam_x"}),
        checkpoint_at=time.time(),
    )
    db.update_subjob_by_type(job_id, "align", status="running", started_at=time.time())
    db.update_subjob_by_type(job_id, "build", status="running", started_at=time.time())

    monkeypatch.setattr(worker_run, "_pid_alive", lambda pid: False)

    worker_run._recover_stale_running_jobs()

    job = db.get_job(job_id)
    assert job["status"] == "cancelled"
    assert job["checkpoint_json"] is None
    assert job["checkpoint_at"] is None

    align_row = db.get_subjob(job_id, "align")
    build_row = db.get_subjob(job_id, "build")
    assert align_row["status"] == "cancelled"
    assert build_row["status"] == "cancelled"


def test_reconcile_skipped_build_jobs_marks_done_job_as_error(tmp_path):
    params = {
        "class_name": "City",
        "parts_spec": "1",
        "wdc_predicate_pattern": "sameAs",
        "wdc_value_is_wikidata": True,
        "wkd_class": "Q486972",
    }
    job_id = db.insert_job(params)
    ended = time.time()
    reason = "No alignments found (0); build skipped."
    db.update_job(
        job_id,
        status="done",
        phase="build",
        ended_at=ended,
        progress_text=reason,
        result_path=None,
        error_message=None,
    )
    db.update_subjob_by_type(job_id, "align", status="done", ended_at=ended)
    db.update_subjob_by_type(
        job_id,
        "build",
        status="done",
        ended_at=ended,
        progress_text=reason,
        current_step="skipped",
    )

    worker_run._reconcile_skipped_build_jobs()

    job = db.get_job(job_id)
    assert job["status"] == "error"
    assert (job["error_message"] or "").lower().startswith("no alignments found")

    build_row = db.get_subjob(job_id, "build")
    assert build_row["status"] == "error"
    assert build_row["current_step"] == "skipped"


def test_reconcile_skipped_build_jobs_repairs_partial_error_state():
    params = {
        "class_name": "City",
        "parts_spec": "1",
        "wdc_predicate_pattern": "sameAs",
        "wdc_value_is_wikidata": True,
        "wkd_class": "Q486972",
    }
    job_id = db.insert_job(params)
    ended = time.time()
    reason = "No alignments found (0); build skipped."
    db.update_job(
        job_id,
        status="error",
        phase="build",
        ended_at=ended,
        progress_text=reason,
        error_message=reason,
    )
    db.update_subjob_by_type(job_id, "align", status="done", ended_at=ended)
    db.update_subjob_by_type(
        job_id,
        "build",
        status="done",
        ended_at=ended,
        progress_text=reason,
        current_step="skipped",
    )

    worker_run._reconcile_skipped_build_jobs()

    job = db.get_job(job_id)
    assert job["status"] == "error"

    build_row = db.get_subjob(job_id, "build")
    assert build_row["status"] == "error"
    assert build_row["current_step"] == "skipped"
