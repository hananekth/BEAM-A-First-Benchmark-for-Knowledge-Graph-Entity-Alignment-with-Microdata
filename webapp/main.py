import json
import os
import shutil
import tempfile
import time
import zipfile
import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from beam import db
from beam.wdc_classes import fetch_wdc_classes
from beam.pipeline import _count_local_parts

app = FastAPI()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


PRESETS = {
    "testclass_large_benchmark": {
        "label": "Bigger local benchmark (TestClassLarge / language label)",
        "class_name": "TestClassLarge",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q34770",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": False,
        "max_depth": 0,
        "force_align": True,
        "use_local_only": True,
    },
    "testclass_quick": {
        "label": "Quick local test (TestClass / language label)",
        "class_name": "TestClass",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q34770",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": False,
        "max_depth": 0,
        "use_local_only": True,
    },
    "testclass_label": {
        "label": "TestClass label matching (name -> rdfs:label)",
        "class_name": "TestClassLabel",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q34770",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": False,
        "max_depth": 0,
        "use_local_only": True,
    },
    "testclass_identifier": {
        "label": "TestClass identifier matching (eidr -> P2704)",
        "class_name": "TestClassIdentifier",
        "parts_spec": "all",
        "wdc_predicate_pattern": "eidr",
        "wikidata_property": "wdt:P2704",
        "wkd_class": "Q11424",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": False,
        "max_depth": 0,
        "use_local_only": True,
    },
    "testclass_wikidata_url": {
        "label": "TestClass Wikidata links (url -> P31 city)",
        "class_name": "TestClassWikidataUrl",
        "parts_spec": "all",
        "wdc_predicate_pattern": "url",
        "wikidata_property": "wdt:P31",
        "wkd_class": "Q515",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": True,
        "max_depth": 0,
        "use_local_only": True,
    },
    "testclass_wikidata_sameas": {
        "label": "TestClass Wikidata links (sameAs -> P31 country)",
        "class_name": "TestClassWikidataSameAs",
        "parts_spec": "all",
        "wdc_predicate_pattern": "sameas",
        "wikidata_property": "wdt:P31",
        "wkd_class": "Q6256",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": True,
        "max_depth": 0,
        "use_local_only": True,
    },
    "property_movie": {
        "label": "Match with property (Movie / EIDR)",
        "class_name": "Movie",
        "parts_spec": "all",
        "wdc_predicate_pattern": "eidr",
        "wikidata_property": "wdt:P2704",
        "wkd_class": "Q11424",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": False,
        "max_depth": -1,
    },
    "label_language": {
        "label": "Match with label (Language / rdfs:label)",
        "class_name": "Language",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q34770",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": False,
        "max_depth": -1,
    },
    "property_country_iso2": {
        "label": "Match with property (Country / ISO 3166-1 alpha-2)",
        "class_name": "Country",
        "parts_spec": "all",
        "wdc_predicate_pattern": "iso",
        "wikidata_property": "wdt:P297",
        "wkd_class": "Q6256",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": False,
        "max_depth": -1,
    },
    "wikidata_link_city": {
        "label": "Match with existing Wikidata link (City / url)",
        "class_name": "City",
        "parts_spec": "all",
        "wdc_predicate_pattern": "url",
        "wikidata_property": "wdt:P31",
        "wkd_class": "Q515",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": True,
        "max_depth": -1,
    },
}


def _default_form():
    return {
        "class_name": "",
        "parts_spec": "",
        "wdc_predicate_pattern": "",
        "wikidata_property": "",
        "wkd_class": "",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": False,
        "max_depth": -1,
        "force_align": False,
        "use_local_only": False,
    }


def _clean_text(value: Optional[str]) -> str:
    return (value or "").strip()


def _get_recent_presets(limit=50):
    rows = db.list_jobs(limit=limit)
    recent = []
    seen = set()
    for r in rows:
        try:
            params = json.loads(r["params_json"])
        except Exception:
            continue
        key = tuple(
            params.get(k, "")
            for k in (
                "class_name",
                "parts_spec",
                "wdc_predicate_pattern",
                "wikidata_property",
                "wkd_class",
                "ignore_chars",
                "wdc_value_is_wikidata",
                "max_depth",
            )
        )
        if key in seen:
            continue
        seen.add(key)
        label = (
            f"{params.get('class_name','')} | {params.get('parts_spec','')} | "
            f"{params.get('wdc_predicate_pattern','')} -> "
            f"{params.get('wikidata_property','') or 'Wikidata URL'}"
        )
        recent.append({"label": label, "params": params, "job_id": r["id"]})
    return recent


def _fmt_ts(ts):
    if not ts:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return None


def _fmt_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    n = float(max(0, num_bytes))
    for unit in units:
        if n < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{num_bytes} B"


def _count_lines(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    c = 0
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for _ in f:
            c += 1
    return c


def _discover_local_class_rows(download_root: str = "Download"):
    root = Path(download_root)
    if not root.exists() or not root.is_dir():
        return []

    rows = []
    for class_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        parts = []
        full_graph = []
        try:
            for fp in class_dir.iterdir():
                if not fp.is_file():
                    continue
                name = fp.name
                if name.startswith("part_") and (name.endswith(".nq") or name.endswith(".nt") or "." not in name):
                    parts.append(fp)
                elif name.endswith("_full_graph.nq"):
                    full_graph.append(fp)
        except Exception:
            continue

        files = parts if parts else full_graph
        if not files:
            continue

        total_size = 0
        for fp in files:
            try:
                total_size += fp.stat().st_size
            except Exception:
                pass
        rows.append(
            {
                "class_name": class_dir.name,
                "num_parts": len(parts) if parts else len(full_graph),
                "size_human": _fmt_size(total_size),
            }
        )
    return rows


def _variant_stats(base: Path, variant: str):
    p = base / variant
    if not p.exists() or not p.is_dir():
        return None
    files = {
        "ent_links": p / "ent_links",
        "attr_triples_1": p / "attr_triples_1",
        "rel_triples_1": p / "rel_triples_1",
        "attr_triples_2": p / "attr_triples_2",
        "rel_triples_2": p / "rel_triples_2",
        "prop_stats_wdc": p / "prop_stats_wdc.tsv",
        "prop_stats_wd": p / "prop_stats_wd.tsv",
    }
    size_total = 0
    for fp in files.values():
        if fp.exists() and fp.is_file():
            try:
                size_total += fp.stat().st_size
            except Exception:
                pass
    links_lines = _count_lines(files["ent_links"])
    links_count = max(0, links_lines - 1) if links_lines else 0
    wd_props = max(0, _count_lines(files["prop_stats_wd"]) - 1)
    wdc_props = max(0, _count_lines(files["prop_stats_wdc"]) - 1)
    return {
        "name": variant,
        "path": str(p),
        "size_total_b": size_total,
        "size_total_h": _fmt_size(size_total),
        "links_count": links_count,
        "wd_props": wd_props,
        "wdc_props": wdc_props,
        "files": {k: str(v) for k, v in files.items() if v.exists()},
    }


def _scan_builds(limit=30):
    builds = []
    root = Path("data")
    if not root.exists():
        return builds
    markers = list(root.glob("*/beam_*/BUILD_DONE"))
    markers.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for marker in markers[:limit]:
        base = marker.parent
        st = marker.stat()
        build_config = None
        cfg_path = base / "BUILD_CONFIG.json"
        if cfg_path.exists():
            try:
                build_config = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                build_config = None
        with_link = _variant_stats(base, "with_link_code")
        without_link = _variant_stats(base, "without_link_code")
        variants_same = False
        if with_link and without_link:
            variants_same = (
                with_link["size_total_b"] == without_link["size_total_b"]
                and with_link["links_count"] == without_link["links_count"]
                and with_link["wdc_props"] == without_link["wdc_props"]
                and with_link["wd_props"] == without_link["wd_props"]
            )
        builds.append(
            {
                "class_name": base.parent.name,
                "build_name": base.name,
                "path": str(base),
                "done_at": _fmt_ts(st.st_mtime),
                "with_link": with_link,
                "without_link": without_link,
                "variants_same": variants_same,
                "build_config": build_config,
            }
        )
    return builds


def _build_config_groups(cfg: dict):
    if not isinstance(cfg, dict):
        return []
    ordered = [
        ("Input", ["class_name"]),
        ("Matching", ["wdc_predicate_pattern", "wikidata_property", "wkd_class", "wdc_value_is_wikidata", "ignore_chars"]),
        ("Build", ["max_depth", "force_align", "use_local_only", "build_name", "result_path"]),
    ]
    used = set()
    groups = []
    for title, keys in ordered:
        items = []
        for k in keys:
            if k in cfg:
                items.append((k, cfg[k]))
                used.add(k)
        if items:
            groups.append({"title": title, "items": items})
    ignored = {
        "parts_spec",
        "parts_count",
        "parts_total_size_human",
        "parts_total_size_bytes",
        "parts_manifest",
    }
    other = [(k, v) for k, v in cfg.items() if (k not in used and k not in ignored)]
    if other:
        groups.append({"title": "Other", "items": other})
    return groups


def _safe_unlink(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _resolve_build_dir(class_name: str, build_name: str):
    data_root = Path("data").resolve()
    base = (data_root / class_name / build_name).resolve()
    try:
        base.relative_to(data_root)
    except ValueError:
        return None
    if not base.exists() or not base.is_dir():
        return None
    if not (base / "BUILD_DONE").exists():
        return None
    return base


def _job_outputs(job):
    out = {"build_done": False, "build_out_with": None, "build_out_without": None, "build_done_file": None}
    result_path = job["result_path"]
    if result_path:
        base = Path(result_path)
        out["build_done_file"] = str(base / "BUILD_DONE")
        if (base / "BUILD_DONE").exists():
            out["build_done"] = True
        if (base / "with_link_code").exists():
            out["build_out_with"] = str(base / "with_link_code")
        if (base / "without_link_code").exists():
            out["build_out_without"] = str(base / "without_link_code")
    return out


@app.on_event("startup")
def _init_db():
    db.init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, preset: Optional[str] = None, recent: Optional[int] = None):
    # Ensure classes cached
    if not db.list_wdc_classes():
        try:
            rows = fetch_wdc_classes()
            if rows:
                db.upsert_wdc_classes(rows)
        except Exception:
            pass
    local_rows = _discover_local_class_rows("Download")
    if local_rows:
        try:
            db.upsert_wdc_classes(local_rows)
        except Exception:
            pass

    form = _default_form()
    if preset and preset in PRESETS:
        form.update(PRESETS[preset])

    if recent:
        job = db.get_job(recent)
        if job:
            try:
                params = json.loads(job["params_json"])
                form.update(params)
            except Exception:
                pass

    wdc_classes = [dict(r) for r in db.list_wdc_classes()]
    class_meta = {r["class_name"]: r for r in wdc_classes}

    local_parts = 0
    if form.get("class_name"):
        local_parts = _count_local_parts(str(Path("Download") / form["class_name"]))

    recent_presets = _get_recent_presets()
    all_jobs = db.list_jobs(limit=50)
    jobs = [j for j in all_jobs if j["status"] != "done"]
    builds = _scan_builds(limit=40)
    build_params = {}
    for j in all_jobs:
        rp = j["result_path"]
        if not rp or rp in build_params:
            continue
        try:
            build_params[rp] = json.loads(j["params_json"] or "{}")
        except Exception:
            continue
    for b in builds:
        params = b.get("build_config") or build_params.get(b["path"])
        if params:
            b["config"] = params
        else:
            b["config"] = {
                "class_name": b["class_name"],
                "build_name": b["build_name"],
                "result_path": b["path"],
                "config_source": "inferred",
            }
        parts = b["config"].get("parts_manifest")
        if not isinstance(parts, list):
            parts = []
        b["parts_manifest"] = parts
        b["parts_count"] = b["config"].get("parts_count", len(parts))
        b["parts_total_size_human"] = b["config"].get("parts_total_size_human")
        b["config_groups"] = _build_config_groups(b["config"])
    jobs_outputs = {}
    jobs_times = {}
    for j in jobs:
        jobs_outputs[j["id"]] = _job_outputs(j)
        jobs_times[j["id"]] = {
            "created": _fmt_ts(j["created_at"]),
            "started": _fmt_ts(j["started_at"]),
            "ended": _fmt_ts(j["ended_at"]),
        }

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "form": form,
            "presets": PRESETS,
            "recent_presets": recent_presets,
            "jobs": jobs,
            "jobs_outputs": jobs_outputs,
            "jobs_times": jobs_times,
            "jobs_params": {j["id"]: json.loads(j["params_json"]) for j in jobs},
            "jobs_subjobs": {j["id"]: [dict(s) for s in db.list_subjobs(j["id"])] for j in jobs},
            "builds": builds,
            "class_meta": class_meta,
            "local_parts": local_parts,
        },
    )


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        return RedirectResponse(url="/", status_code=303)
    if job["status"] not in {"running", "queued"}:
        return RedirectResponse(url="/", status_code=303)
    db.request_cancel(job_id)
    db.request_cancel_subjob(job_id, "align")
    db.request_cancel_subjob(job_id, "build")
    if job["status"] == "queued":
        db.update_subjob_by_type(job_id, "align", status="cancelled", ended_at=time.time())
        db.update_subjob_by_type(job_id, "build", status="cancelled", ended_at=time.time())
    db.insert_event(job_id, "system", "Cancel requested (job)")
    return RedirectResponse(url="/", status_code=303)


@app.post("/jobs/{job_id}/cancel_subjob/{subjob_type}")
def cancel_subjob(job_id: int, subjob_type: str):
    if subjob_type not in {"align", "build"}:
        return RedirectResponse(url="/", status_code=303)
    job = db.get_job(job_id)
    if not job:
        return RedirectResponse(url="/", status_code=303)
    if job["status"] not in {"running", "queued"}:
        return RedirectResponse(url="/", status_code=303)
    sj = db.get_subjob(job_id, subjob_type)
    if not sj or sj["status"] not in {"running", "queued"}:
        return RedirectResponse(url="/", status_code=303)

    db.request_cancel_subjob(job_id, subjob_type)
    if subjob_type == "align":
        # Align cancel implies full job cancel and build cancel.
        db.request_cancel(job_id)
        db.request_cancel_subjob(job_id, "build")
        db.insert_event(job_id, "system", "Cancel requested (align; build will be cancelled too)")
    else:
        # Build cancel does not interrupt align. If already in build, stop current process.
        if job["phase"] == "build":
            db.request_cancel(job_id)
        db.insert_event(job_id, "system", "Cancel requested (build only)")

    if job["status"] == "queued":
        if subjob_type == "align":
            db.update_subjob_by_type(job_id, "align", status="cancelled", ended_at=time.time())
            db.update_subjob_by_type(job_id, "build", status="cancelled", ended_at=time.time())
        else:
            db.update_subjob_by_type(job_id, "build", status="cancelled", ended_at=time.time())
    return RedirectResponse(url="/", status_code=303)


@app.post("/jobs/{job_id}/rerun")
def rerun_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        return RedirectResponse(url="/", status_code=303)
    try:
        params = json.loads(job["params_json"])
    except Exception:
        return RedirectResponse(url="/", status_code=303)
    db.insert_job(params)
    return RedirectResponse(url="/", status_code=303)


@app.post("/jobs/{job_id}/rerun_nocache")
def rerun_job_nocache(job_id: int):
    job = db.get_job(job_id)
    if not job:
        return RedirectResponse(url="/", status_code=303)
    try:
        params = json.loads(job["params_json"])
    except Exception:
        return RedirectResponse(url="/", status_code=303)
    params["force_align"] = True
    params["skip_build"] = False
    params.pop("require_cached_align", None)
    db.insert_job(params)
    return RedirectResponse(url="/", status_code=303)


@app.post("/jobs/{job_id}/rerun_align")
def rerun_align(job_id: int):
    job = db.get_job(job_id)
    if not job:
        return RedirectResponse(url="/", status_code=303)
    try:
        params = json.loads(job["params_json"])
    except Exception:
        return RedirectResponse(url="/", status_code=303)
    params["skip_build"] = True
    db.insert_job(params)
    return RedirectResponse(url="/", status_code=303)


@app.post("/jobs/{job_id}/rerun_build")
def rerun_build(job_id: int):
    job = db.get_job(job_id)
    if not job:
        return RedirectResponse(url="/", status_code=303)
    try:
        params = json.loads(job["params_json"])
    except Exception:
        return RedirectResponse(url="/", status_code=303)
    params["require_cached_align"] = True
    params["skip_build"] = False
    db.insert_job(params)
    return RedirectResponse(url="/", status_code=303)


@app.post("/jobs/{job_id}/delete")
def delete_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        return RedirectResponse(url="/", status_code=303)
    # Never delete active jobs to avoid orphaned worker processes.
    if job["status"] in {"running", "queued"}:
        return RedirectResponse(url="/", status_code=303)
    db.delete_job(job_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/jobs")
def create_job(
    class_name: str = Form(...),
    parts_spec: str = Form(""),
    wdc_predicate_pattern: str = Form(""),
    wikidata_property: str = Form(""),
    wkd_class: str = Form(""),
    ignore_chars: str = Form(""),
    wdc_value_is_wikidata: Optional[str] = Form(None),
    max_depth: int = Form(-1),
    force_align: Optional[str] = Form(None),
    use_local_only: Optional[str] = Form(None),
):
    params = {
        "class_name": _clean_text(class_name),
        "parts_spec": _clean_text(parts_spec),
        "wdc_predicate_pattern": _clean_text(wdc_predicate_pattern),
        "wikidata_property": _clean_text(wikidata_property),
        "wkd_class": _clean_text(wkd_class),
        "ignore_chars": _clean_text(ignore_chars),
        "wdc_value_is_wikidata": bool(wdc_value_is_wikidata),
        "max_depth": int(max_depth),
        "force_align": bool(force_align),
        "use_local_only": bool(use_local_only),
    }
    db.insert_job(params)
    return RedirectResponse(url="/", status_code=303)


@app.get("/refresh_classes")
def refresh_classes():
    rows = fetch_wdc_classes()
    if rows:
        db.upsert_wdc_classes(rows)
    return RedirectResponse(url="/", status_code=303)


@app.get("/builds/{class_name}/{build_name}/download")
def download_build(class_name: str, build_name: str):
    build_dir = _resolve_build_dir(class_name, build_name)
    if not build_dir:
        return RedirectResponse(url="/", status_code=303)
    data_root = Path("data").resolve()
    fd, zip_path = tempfile.mkstemp(prefix=f"beam_{class_name}_{build_name}_", suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fp in build_dir.rglob("*"):
            if fp.is_file():
                zf.write(fp, arcname=str(fp.resolve().relative_to(data_root)))
    filename = f"{class_name}_{build_name}.zip"
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(_safe_unlink, zip_path),
    )


@app.post("/builds/{class_name}/{build_name}/delete")
def delete_build(class_name: str, build_name: str):
    build_dir = _resolve_build_dir(class_name, build_name)
    if not build_dir:
        return RedirectResponse(url="/", status_code=303)
    try:
        db.delete_jobs_by_result_path(str(build_dir))
    except Exception:
        pass
    shutil.rmtree(build_dir, ignore_errors=True)
    return RedirectResponse(url="/", status_code=303)


@app.websocket("/ws/logs/{job_id}")
async def ws_logs(websocket: WebSocket, job_id: int):
    await websocket.accept()
    try:
        job = db.get_job(job_id)
        if not job:
            await websocket.send_text("Job not found")
            await websocket.close()
            return
        last_id = 0
        def _event_payload(row):
            meta = None
            try:
                if row["meta_json"]:
                    meta = json.loads(row["meta_json"])
            except Exception:
                meta = None
            return {
                "type": "event",
                "id": row["id"],
                "ts": row["ts"],
                "level": row["level"],
                "message": row["message"],
                "phase": row["phase"],
                "kind": row["kind"],
                "step": row["step"],
                "worker": row["worker"],
                "progress_pct": row["progress_pct"],
                "meta": meta,
            }
        # send recent history
        rows = db.list_events(job_id, since_id=None, limit=200)
        for r in rows:
            await websocket.send_text(json.dumps(_event_payload(r)))
            last_id = r["id"]
        while True:
            # Push updates at a fixed cadence even if client pings stall.
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            job = db.get_job(job_id)
            if job:
                payload = {
                    "type": "progress",
                    "status": job["status"],
                    "cancel_requested": job["cancel_requested"],
                    "phase": job["phase"],
                    "progress_text": job["progress_text"],
                    "progress_pct": job["progress_pct"],
                    "current_step": job["current_step"],
                    "current_file": job["current_file"],
                    "result_path": job["result_path"],
                    "align_dir": job["align_dir"],
                    "reused_align": bool(job["reused_align"]),
                    "error_message": job["error_message"],
                    "outputs": _job_outputs(job),
                    "subjobs": [dict(s) for s in db.list_subjobs(job_id)],
                }
                await websocket.send_text(json.dumps(payload))
            rows = db.list_events(job_id, since_id=last_id, limit=200)
            if rows:
                for r in rows:
                    await websocket.send_text(json.dumps(_event_payload(r)))
                    last_id = r["id"]
    except WebSocketDisconnect:
        return
