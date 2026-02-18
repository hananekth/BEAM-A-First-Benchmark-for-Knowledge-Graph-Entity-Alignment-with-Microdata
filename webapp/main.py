import json
import os
import shutil
import tempfile
import time
import zipfile
import asyncio
import re
from pathlib import Path
from functools import lru_cache
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from beam import db
from beam.wdc_classes import fetch_wdc_classes

app = FastAPI()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

WDC_PARTS_BASE_URL = "https://data.dws.informatik.uni-mannheim.de/structureddata/2024-12/quads/classspecific/"
_PART_HREF_RE = re.compile(r"^part_(\d+)\.gz$", re.IGNORECASE)
_PART_NAME_RE = re.compile(r"^part_(\d+)(?:\.[A-Za-z0-9]+)?$", re.IGNORECASE)


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
        "force_align": False,
        "use_local_only": False,
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
        "force_align": False,
        "use_local_only": False,
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
        "force_align": False,
        "use_local_only": False,
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
        "force_align": False,
        "use_local_only": False,
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
        "force_align": False,
        "use_local_only": False,
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
        "force_align": False,
        "use_local_only": False,
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
        "max_depth": 0,
        "force_align": False,
        "use_local_only": False,
    },
    "label_language": {
        "label": "Match with label (Language / rdfs:label)",
        "class_name": "Language",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q34772",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": False,
        "max_depth": 0,
        "force_align": False,
        "use_local_only": False,
    },
    "property_college_or_university_telephone": {
        "label": "Match with property (CollegeOrUniversity / telephone)",
        "class_name": "CollegeOrUniversity",
        "parts_spec": "all",
        "wdc_predicate_pattern": "telephone",
        "wikidata_property": "P1329",
        "wkd_class": "Q38723",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": False,
        "max_depth": 0,
        "force_align": False,
        "use_local_only": False,
    },
    "wikidata_link_city": {
        "label": "Match with existing Wikidata link (City / sameAs)",
        "class_name": "City",
        "parts_spec": "all",
        "wdc_predicate_pattern": "sameAs",
        "wikidata_property": "",
        "wkd_class": "Q486972",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": True,
        "max_depth": 0,
        "force_align": False,
        "use_local_only": False,
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
        "max_depth": 0,
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


def _part_number_from_name(name: str):
    if not name:
        return None
    m = _PART_HREF_RE.match(name) or _PART_NAME_RE.match(name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _discover_local_part_numbers(class_name: str):
    class_dir = Path("Download") / (class_name or "")
    if not class_dir.exists() or not class_dir.is_dir():
        return []

    numbers = set()
    for fp in class_dir.iterdir():
        if not fp.is_file():
            continue
        name = fp.name
        if not name.startswith("part_"):
            continue
        if not (name.endswith(".nq") or name.endswith(".nt") or "." not in name):
            continue
        num = _part_number_from_name(name)
        if num is not None:
            numbers.add(num)
    return sorted(numbers)


@lru_cache(maxsize=256)
def _discover_online_part_numbers(class_name: str):
    if not class_name:
        return [], "class_name is empty"
    url = urljoin(WDC_PARTS_BASE_URL, f"{class_name}/")
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        numbers = set()
        for link in soup.find_all("a"):
            href = (link.get("href") or "").strip()
            num = _part_number_from_name(href)
            if num is not None:
                numbers.add(num)
        return sorted(numbers), None
    except Exception as exc:
        return [], str(exc)


def _format_part_ranges(values):
    if not values:
        return "—"
    nums = sorted(set(int(v) for v in values))
    chunks = []
    start = nums[0]
    prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        chunks.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = n
    chunks.append(f"{start}-{prev}" if start != prev else str(start))
    if len(chunks) > 28:
        return ", ".join(chunks[:28]) + f", ... (+{len(chunks)-28} ranges)"
    return ", ".join(chunks)


def _format_part_list(values, limit=60):
    if not values:
        return "—"
    nums = [int(v) for v in sorted(set(values))]
    if len(nums) <= limit:
        return ", ".join(str(v) for v in nums)
    return ", ".join(str(v) for v in nums[:limit]) + f", ... (+{len(nums)-limit})"


def _class_meta_by_name(class_name: str):
    for row in db.list_wdc_classes():
        if row["class_name"] == class_name:
            return dict(row)
    return None


def _build_class_parts_info(class_name: str):
    class_name = _clean_text(class_name)
    local_numbers = _discover_local_part_numbers(class_name)
    online_numbers, online_error = _discover_online_part_numbers(class_name)
    local_set = set(local_numbers)
    meta = _class_meta_by_name(class_name) or {}
    class_num_parts = meta.get("num_parts")
    try:
        class_num_parts = int(class_num_parts) if class_num_parts is not None else None
    except Exception:
        class_num_parts = None

    online_set = set(online_numbers)
    inferred_online_set = set(online_set)
    inferred_from_catalog = False

    if online_numbers:
        start_num = min(online_numbers)
    elif local_numbers:
        start_num = min(local_numbers)
    else:
        start_num = 0

    catalog_expected_numbers = []
    if class_num_parts and class_num_parts > 0:
        catalog_expected_numbers = list(range(start_num, start_num + class_num_parts))
        catalog_set = set(catalog_expected_numbers)
        if not inferred_online_set:
            inferred_online_set = set(catalog_set)
            inferred_from_catalog = True
        elif len(inferred_online_set) < class_num_parts:
            # Online listing can be incomplete; complete the expected contiguous range using catalog count.
            inferred_online_set |= catalog_set
            inferred_from_catalog = True

    if inferred_online_set:
        downloaded_numbers = sorted(local_set & inferred_online_set)
    else:
        downloaded_numbers = list(local_numbers)
    not_downloaded_online_numbers = sorted(inferred_online_set - local_set)
    local_only_numbers = sorted(local_set - inferred_online_set) if inferred_online_set else []

    return {
        "class_name": class_name,
        "class_num_parts": class_num_parts,
        "class_size_human": meta.get("size_human"),
        "online_error": online_error,
        "online_available_count": len(inferred_online_set),
        "online_available_numbers": sorted(inferred_online_set),
        "online_available_numbers_text": _format_part_list(sorted(inferred_online_set)),
        "online_available_ranges": _format_part_ranges(sorted(inferred_online_set)),
        "online_discovered_count": len(online_numbers),
        "online_discovered_numbers": online_numbers,
        "online_discovered_numbers_text": _format_part_list(online_numbers),
        "online_discovered_ranges": _format_part_ranges(online_numbers),
        "online_inferred_from_catalog": inferred_from_catalog,
        "catalog_expected_numbers": catalog_expected_numbers,
        "catalog_expected_ranges": _format_part_ranges(catalog_expected_numbers),
        "downloaded_parts_count": len(downloaded_numbers),
        "downloaded_part_numbers": downloaded_numbers,
        "downloaded_part_numbers_text": _format_part_list(downloaded_numbers),
        "downloaded_part_ranges": _format_part_ranges(downloaded_numbers),
        "not_downloaded_online_parts_count": len(not_downloaded_online_numbers),
        "not_downloaded_online_part_numbers": not_downloaded_online_numbers,
        "not_downloaded_online_part_numbers_text": _format_part_list(not_downloaded_online_numbers),
        "not_downloaded_online_part_ranges": _format_part_ranges(not_downloaded_online_numbers),
        "local_only_parts_count": len(local_only_numbers),
        "local_only_part_numbers": local_only_numbers,
        "local_only_part_numbers_text": _format_part_list(local_only_numbers),
    }


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


def _safe_json_loads(raw: Optional[str]):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _looks_like_skipped_build_reason(text: Optional[str]) -> bool:
    msg = str(text or "").strip().lower()
    if not msg:
        return False
    return ("build skipped" in msg) or ("no alignments found" in msg)


def _build_dashboard_state(job_limit: int = 50, build_limit: int = 40):
    all_jobs = [dict(j) for j in db.list_jobs(limit=job_limit)]
    jobs_by_id = {j["id"]: j for j in all_jobs}
    # Always include truly active jobs even if they are outside the recency window.
    for st in ("running", "queued"):
        for row in db.list_jobs_by_status(st):
            jid = row["id"]
            if jid not in jobs_by_id:
                jobs_by_id[jid] = dict(row)
    all_jobs = sorted(jobs_by_id.values(), key=lambda r: int(r.get("id") or 0), reverse=True)
    active_jobs = [j for j in all_jobs if j["status"] in {"running", "queued"}]
    builds = _scan_builds(limit=build_limit)

    build_params = {}
    for j in all_jobs:
        rp = j.get("result_path")
        if not rp or rp in build_params:
            continue
        params = _safe_json_loads(j.get("params_json"))
        if params:
            build_params[rp] = params

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
    jobs_params = {}
    jobs_subjobs = {}
    for j in all_jobs:
        jid = j["id"]
        jobs_outputs[jid] = _job_outputs(j)
        jobs_times[jid] = {
            "created": _fmt_ts(j.get("created_at")),
            "started": _fmt_ts(j.get("started_at")),
            "ended": _fmt_ts(j.get("ended_at")),
        }
        jobs_params[jid] = _safe_json_loads(j.get("params_json"))
        jobs_subjobs[jid] = [dict(s) for s in db.list_subjobs(jid)]

    # Legacy safety: some old rows can be persisted as "done" even when build was skipped
    # due to 0 alignments. Normalize the state in dashboard payload to avoid misleading UI.
    for j in all_jobs:
        if j.get("status") != "done":
            continue
        jid = j["id"]
        if jobs_outputs.get(jid, {}).get("build_done"):
            continue
        build_row = next((s for s in jobs_subjobs.get(jid, []) if s.get("type") == "build"), None)
        build_step = str((build_row or {}).get("current_step") or "").strip().lower()
        build_msg = str((build_row or {}).get("progress_text") or "").strip()
        job_msg = str(j.get("progress_text") or "").strip()
        err_msg = str(j.get("error_message") or "").strip()
        skipped = (
            build_step == "skipped"
            or _looks_like_skipped_build_reason(build_msg)
            or _looks_like_skipped_build_reason(job_msg)
            or _looks_like_skipped_build_reason(err_msg)
        )
        if not skipped:
            continue
        reason = build_msg or job_msg or err_msg or "No alignments found (0); build skipped."
        j["status"] = "error"
        j["phase"] = j.get("phase") or "build"
        j["error_message"] = reason

    # Keep done jobs visible when there is no downloadable build output.
    jobs_for_panel = [
        j for j in all_jobs
        if j["status"] != "done" or not jobs_outputs.get(j["id"], {}).get("build_done")
    ]

    return {
        "all_jobs": all_jobs,
        "active_jobs": active_jobs,
        "jobs_for_panel": jobs_for_panel,
        "builds": builds,
        "jobs_outputs": jobs_outputs,
        "jobs_times": jobs_times,
        "jobs_params": jobs_params,
        "jobs_subjobs": jobs_subjobs,
    }


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
    selected_preset = ""
    if preset and preset in PRESETS:
        form.update(PRESETS[preset])
        selected_preset = preset

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

    class_parts_info = None
    if form.get("class_name"):
        class_parts_info = _build_class_parts_info(form["class_name"])

    recent_presets = _get_recent_presets()
    dashboard = _build_dashboard_state(job_limit=50, build_limit=40)
    jobs = dashboard["jobs_for_panel"]
    builds = dashboard["builds"]
    jobs_outputs = {j["id"]: dashboard["jobs_outputs"][j["id"]] for j in jobs}
    jobs_times = {j["id"]: dashboard["jobs_times"][j["id"]] for j in jobs}
    jobs_params = {j["id"]: dashboard["jobs_params"][j["id"]] for j in jobs}
    jobs_subjobs = {j["id"]: dashboard["jobs_subjobs"][j["id"]] for j in jobs}

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "form": form,
            "presets": PRESETS,
            "selected_preset": selected_preset,
            "recent_presets": recent_presets,
            "jobs": jobs,
            "jobs_outputs": jobs_outputs,
            "jobs_times": jobs_times,
            "jobs_params": jobs_params,
            "jobs_subjobs": jobs_subjobs,
            "builds": builds,
            "class_meta": class_meta,
            "class_parts_info": class_parts_info,
        },
    )


@app.get("/api/dashboard")
def dashboard_api(job_limit: int = 80, build_limit: int = 40):
    job_limit = max(1, min(int(job_limit), 200))
    build_limit = max(1, min(int(build_limit), 200))
    dashboard = _build_dashboard_state(job_limit=job_limit, build_limit=build_limit)

    jobs = []
    for j in dashboard["all_jobs"]:
        jid = j["id"]
        jobs.append(
            {
                **j,
                "times": dashboard["jobs_times"].get(jid, {}),
                "params": dashboard["jobs_params"].get(jid, {}),
                "outputs": dashboard["jobs_outputs"].get(jid, {}),
                "subjobs": dashboard["jobs_subjobs"].get(jid, []),
            }
        )

    builds = []
    for b in dashboard["builds"]:
        builds.append(
            {
                "class_name": b.get("class_name"),
                "build_name": b.get("build_name"),
                "path": b.get("path"),
                "done_at": b.get("done_at"),
                "with_link": b.get("with_link"),
                "without_link": b.get("without_link"),
                "variants_same": b.get("variants_same"),
                "config_groups": b.get("config_groups") or [],
            }
        )

    return {
        "server_ts": time.time(),
        "job_count": len(jobs),
        "active_job_count": len(dashboard["active_jobs"]),
        "visible_job_count": len(dashboard["jobs_for_panel"]),
        "build_count": len(builds),
        "active_job_ids": [j["id"] for j in dashboard["active_jobs"]],
        "visible_job_ids": [j["id"] for j in dashboard["jobs_for_panel"]],
        "jobs": jobs,
        "builds": builds,
    }


@app.get("/api/class_parts/{class_name}")
def class_parts_api(class_name: str):
    return _build_class_parts_info(class_name)


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
    max_depth: int = Form(0),
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
