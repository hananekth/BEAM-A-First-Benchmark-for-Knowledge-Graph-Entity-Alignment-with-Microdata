import json
import sqlite3
import time
from pathlib import Path

DB_PATH = (Path(__file__).resolve().parents[1] / "jobs.db")


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                phase TEXT,
                cancel_requested INTEGER DEFAULT 0,
                align_dir TEXT,
                reused_align INTEGER DEFAULT 0,
                interrupted INTEGER DEFAULT 0,
                progress_text TEXT,
                progress_pct REAL,
                current_step TEXT,
                current_file TEXT,
                checkpoint_json TEXT,
                checkpoint_at REAL,
                job_pid INTEGER,
                job_pgid INTEGER,
                params_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                started_at REAL,
                ended_at REAL,
                log_path TEXT,
                result_path TEXT,
                error_message TEXT,
                final_links_count INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                ts REAL NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                phase TEXT,
                kind TEXT,
                step TEXT,
                worker TEXT,
                progress_pct REAL,
                meta_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subjobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                cancel_requested INTEGER DEFAULT 0,
                progress_text TEXT,
                progress_pct REAL,
                current_step TEXT,
                current_file TEXT,
                started_at REAL,
                ended_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wdc_classes (
                class_name TEXT PRIMARY KEY,
                num_parts INTEGER,
                size_human TEXT,
                updated_at REAL NOT NULL
            )
            """
        )
        # Migrations for existing DBs
        cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        if "phase" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN phase TEXT")
        if "cancel_requested" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER DEFAULT 0")
        if "align_dir" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN align_dir TEXT")
        if "reused_align" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN reused_align INTEGER DEFAULT 0")
        if "interrupted" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN interrupted INTEGER DEFAULT 0")
        if "progress_text" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN progress_text TEXT")
        if "progress_pct" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN progress_pct REAL")
        if "current_step" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN current_step TEXT")
        if "current_file" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN current_file TEXT")
        if "checkpoint_json" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN checkpoint_json TEXT")
        if "checkpoint_at" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN checkpoint_at REAL")
        if "job_pid" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN job_pid INTEGER")
        if "job_pgid" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN job_pgid INTEGER")
        if "final_links_count" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN final_links_count INTEGER")
        sub_cols = [r[1] for r in conn.execute("PRAGMA table_info(subjobs)").fetchall()]
        if "cancel_requested" not in sub_cols:
            conn.execute("ALTER TABLE subjobs ADD COLUMN cancel_requested INTEGER DEFAULT 0")
        event_cols = [r[1] for r in conn.execute("PRAGMA table_info(job_events)").fetchall()]
        if "phase" not in event_cols:
            conn.execute("ALTER TABLE job_events ADD COLUMN phase TEXT")
        if "kind" not in event_cols:
            conn.execute("ALTER TABLE job_events ADD COLUMN kind TEXT")
        if "step" not in event_cols:
            conn.execute("ALTER TABLE job_events ADD COLUMN step TEXT")
        if "worker" not in event_cols:
            conn.execute("ALTER TABLE job_events ADD COLUMN worker TEXT")
        if "progress_pct" not in event_cols:
            conn.execute("ALTER TABLE job_events ADD COLUMN progress_pct REAL")
        if "meta_json" not in event_cols:
            conn.execute("ALTER TABLE job_events ADD COLUMN meta_json TEXT")


def insert_job(params):
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO jobs (status, params_json, created_at)
            VALUES (?, ?, ?)
            """,
            ("queued", json.dumps(params), now),
        )
        job_id = cur.lastrowid
        conn.execute(
            "INSERT INTO subjobs (job_id, type, status) VALUES (?, 'align', 'queued')",
            (job_id,),
        )
        conn.execute(
            "INSERT INTO subjobs (job_id, type, status) VALUES (?, 'build', 'queued')",
            (job_id,),
        )
        return job_id


def get_job(job_id):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row


def list_jobs(limit=50):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return rows


def list_jobs_by_status(status):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY id ASC", (status,)
        ).fetchall()
        return rows


def update_job(job_id, **fields):
    if not fields:
        return
    keys = sorted(fields.keys())
    values = [fields[k] for k in keys]
    sets = ", ".join(f"{k} = ?" for k in keys)
    with _connect() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", values + [job_id])


def insert_event(job_id, level, message, phase=None, kind=None, step=None, worker=None, progress_pct=None, meta=None):
    now = time.time()
    meta_json = json.dumps(meta) if isinstance(meta, (dict, list)) else None
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO job_events (
                job_id, ts, level, message, phase, kind, step, worker, progress_pct, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, now, level, message, phase, kind, step, worker, progress_pct, meta_json),
        )


def list_subjobs(job_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM subjobs WHERE job_id = ? ORDER BY id ASC", (job_id,)
        ).fetchall()
        return rows


def get_subjob(job_id, subjob_type):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM subjobs WHERE job_id = ? AND type = ?",
            (job_id, subjob_type),
        ).fetchone()
        return row


def update_subjob(subjob_id, **fields):
    if not fields:
        return
    keys = sorted(fields.keys())
    values = [fields[k] for k in keys]
    sets = ", ".join(f"{k} = ?" for k in keys)
    with _connect() as conn:
        conn.execute(f"UPDATE subjobs SET {sets} WHERE id = ?", values + [subjob_id])


def update_subjob_by_type(job_id, subjob_type, **fields):
    row = get_subjob(job_id, subjob_type)
    if not row:
        return
    update_subjob(row["id"], **fields)


def request_cancel_subjob(job_id, subjob_type):
    with _connect() as conn:
        conn.execute(
            "UPDATE subjobs SET cancel_requested = 1 WHERE job_id = ? AND type = ?",
            (job_id, subjob_type),
        )
        if subjob_type == "align":
            # Align cancellation always implies full job cancellation.
            conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
        elif subjob_type == "build":
            # If build is still queued (or the job is queued), mark it cancelled directly.
            conn.execute(
                "UPDATE subjobs SET status = 'cancelled', ended_at = ? WHERE job_id = ? AND type = 'build' AND status = 'queued'",
                (time.time(), job_id),
            )


def get_cancel_requested_subjob(job_id, subjob_type):
    with _connect() as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM subjobs WHERE job_id = ? AND type = ?",
            (job_id, subjob_type),
        ).fetchone()
        if not row:
            return False
        return bool(row["cancel_requested"])


def list_events(job_id, since_id=None, limit=500):
    with _connect() as conn:
        if since_id:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (job_id, since_id, limit),
            ).fetchall()
        else:
            # Initial load should return the most recent events, not the oldest.
            rows = conn.execute(
                "SELECT * FROM (SELECT * FROM job_events WHERE job_id = ? ORDER BY id DESC LIMIT ?) t ORDER BY id ASC",
                (job_id, limit),
            ).fetchall()
        return rows


def get_latest_event_ts(job_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(ts) AS ts FROM job_events WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if not row:
            return None
        value = row["ts"]
        if value is None:
            return None
        try:
            return float(value)
        except Exception:
            return None


def delete_job(job_id):
    with _connect() as conn:
        conn.execute("DELETE FROM job_events WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM subjobs WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def delete_jobs_by_result_path(result_path):
    with _connect() as conn:
        rows = conn.execute("SELECT id FROM jobs WHERE result_path = ?", (result_path,)).fetchall()
        for r in rows:
            jid = int(r["id"])
            conn.execute("DELETE FROM job_events WHERE job_id = ?", (jid,))
            conn.execute("DELETE FROM subjobs WHERE job_id = ?", (jid,))
            conn.execute("DELETE FROM jobs WHERE id = ?", (jid,))


def request_cancel(job_id):
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,)
        )
        conn.execute(
            "UPDATE jobs SET status = 'cancelled', ended_at = ? WHERE id = ? AND status = 'queued'",
            (time.time(), job_id),
        )


def get_cancel_requested(job_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            return False
        return bool(row["cancel_requested"])


def fetch_next_queued(limit=1):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return rows


def count_running():
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE status = 'running'"
        ).fetchone()
        return int(row["c"])


def upsert_wdc_classes(rows):
    now = time.time()
    with _connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO wdc_classes (class_name, num_parts, size_human, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(class_name) DO UPDATE SET
                    num_parts = excluded.num_parts,
                    size_human = excluded.size_human,
                    updated_at = excluded.updated_at
                """,
                (r.get("class_name"), r.get("num_parts"), r.get("size_human"), now),
            )


def list_wdc_classes():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM wdc_classes ORDER BY class_name ASC"
        ).fetchall()
        return rows


def latest_wdc_update():
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(updated_at) AS t FROM wdc_classes"
        ).fetchone()
        if not row or row["t"] is None:
            return None
        return float(row["t"])


def clear_wdc_classes():
    with _connect() as conn:
        conn.execute("DELETE FROM wdc_classes")
