import json
import time
import re
import os
from pathlib import Path

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from beam import db
from beam.wdc_classes import fetch_wdc_classes, load_wdc_classes_catalog, save_wdc_classes_catalog
from beam.pipeline import _count_local_parts


st.set_page_config(page_title="BEAM Benchmark", layout="wide")

db.init_db()

st.title("BEAM Benchmark Generator")


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


def _discover_local_class_rows(download_root: str = "Download"):
    root = Path(download_root)
    if not root.exists() or not root.is_dir():
        return []
    rows = []
    for class_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        parts = []
        full_graph = []
        for fp in class_dir.iterdir():
            if not fp.is_file():
                continue
            name = fp.name
            if name.startswith("part_") and (name.endswith(".nq") or name.endswith(".nt") or "." not in name):
                parts.append(fp)
            elif name.endswith("_full_graph.nq"):
                full_graph.append(fp)
        files = parts if parts else full_graph
        if not files:
            continue
        total_size = sum(fp.stat().st_size for fp in files if fp.exists())
        rows.append(
            {
                "class_name": class_dir.name,
                "num_parts": len(parts) if parts else len(full_graph),
                "size_human": _fmt_size(total_size),
            }
        )
    return rows


def _seed_wdc_classes_from_local_catalog():
    try:
        rows = load_wdc_classes_catalog()
    except Exception:
        return 0
    if not rows:
        return 0
    db.upsert_wdc_classes(rows)
    return len(rows)


def _refresh_wdc_classes_from_remote():
    rows = fetch_wdc_classes()
    if not rows:
        raise RuntimeError("WDC class refresh returned no rows")
    save_wdc_classes_catalog(rows)
    db.upsert_wdc_classes(rows)
    return len(rows)

# Live refresh controls
with st.sidebar:
    st.header("Live Refresh")
    live_refresh = st.checkbox("Auto-refresh", value=False, key="live_refresh")
    refresh_seconds = st.slider("Refresh interval (seconds)", 2, 30, 5, key="refresh_seconds")
    if live_refresh:
        st_autorefresh(interval=refresh_seconds * 1000, key="refresh_tick")

# Sidebar: refresh WDC classes
with st.sidebar:
    st.header("WDC Classes")
    if st.button("Reset cache"):
        db.clear_wdc_classes()
        st.success("Cache cleared")
    if st.button("Refresh classes"):
        try:
            with st.spinner("Fetching WDC classes..."):
                _refresh_wdc_classes_from_remote()
            st.success("Classes updated")
        except Exception as exc:
            st.error(f"Refresh failed; local cache/catalog kept unchanged. ({exc})")
    updated_at = db.latest_wdc_update()
    if updated_at:
        st.caption(time.strftime("Last update: %Y-%m-%d %H:%M:%S", time.localtime(updated_at)))

wdc_classes = db.list_wdc_classes()
if not wdc_classes:
    _seed_wdc_classes_from_local_catalog()
    wdc_classes = db.list_wdc_classes()
local_rows = _discover_local_class_rows("Download")
if local_rows:
    db.upsert_wdc_classes(local_rows)
    wdc_classes = db.list_wdc_classes()
class_options = [r["class_name"] for r in wdc_classes]
class_meta = {r["class_name"]: dict(r) for r in wdc_classes}

presets = {
    "Bigger local benchmark (via TestClassLarge / language label)": {
        "class_name": "TestClassLarge",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q34770",
        "ignore_chars": "",
        "wdc_value_is_wikidata": False,
    },
    "Quick local test (via TestClass / language label)": {
        "class_name": "TestClass",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q34770",
        "ignore_chars": "",
        "wdc_value_is_wikidata": False,
    },
    "TestClass label matching (via name -> rdfs:label)": {
        "class_name": "TestClassLabel",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q34770",
        "ignore_chars": "",
        "wdc_value_is_wikidata": False,
    },
    "TestClass identifier matching (via eidr -> P2704)": {
        "class_name": "TestClassIdentifier",
        "parts_spec": "all",
        "wdc_predicate_pattern": "eidr",
        "wikidata_property": "wdt:P2704",
        "wkd_class": "Q11424",
        "ignore_chars": "",
        "wdc_value_is_wikidata": False,
    },
    "TestClass Wikidata links (via url -> P31 city)": {
        "class_name": "TestClassWikidataUrl",
        "parts_spec": "all",
        "wdc_predicate_pattern": "url",
        "wikidata_property": "wdt:P31",
        "wkd_class": "Q515",
        "ignore_chars": "",
        "wdc_value_is_wikidata": True,
    },
    "TestClass Wikidata links (via sameAs -> P31 country)": {
        "class_name": "TestClassWikidataSameAs",
        "parts_spec": "all",
        "wdc_predicate_pattern": "sameas",
        "wikidata_property": "wdt:P31",
        "wkd_class": "Q6256",
        "ignore_chars": "",
        "wdc_value_is_wikidata": True,
    },
    "Match via property (Movie / EIDR)": {
        "class_name": "Movie",
        "parts_spec": "all",
        "wdc_predicate_pattern": "eidr",
        "wikidata_property": "wdt:P2704",
        "wkd_class": "Q11424",
        "ignore_chars": "",
        "wdc_value_is_wikidata": False,
    },
    "Match via label (Language / rdfs:label)": {
        "class_name": "Language",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q34770",
        "ignore_chars": "",
        "wdc_value_is_wikidata": False,
    },
    "Match via property (Country / ISO 3166-1 alpha-2)": {
        "class_name": "Country",
        "parts_spec": "all",
        "wdc_predicate_pattern": "iso",
        "wikidata_property": "wdt:P297",
        "wkd_class": "Q6256",
        "ignore_chars": "",
        "wdc_value_is_wikidata": False,
    },
    "Match via existing Wikidata link (City / url)": {
        "class_name": "City",
        "parts_spec": "all",
        "wdc_predicate_pattern": "url",
        "wikidata_property": "wdt:P31",
        "wkd_class": "Q515",
        "ignore_chars": "",
        "wdc_value_is_wikidata": True,
    },
}

st.subheader("Presets")
preset_name = st.selectbox("Select a preset", ["(none)"] + list(presets.keys()), key="preset_select")
if "preset_applied" not in st.session_state:
    st.session_state["preset_applied"] = None
if preset_name != "(none)" and st.session_state["preset_applied"] != preset_name:
    preset = presets[preset_name]
    st.session_state["class_name"] = preset["class_name"]
    st.session_state["parts_spec"] = preset["parts_spec"]
    st.session_state["wdc_predicate_pattern"] = preset["wdc_predicate_pattern"]
    st.session_state["wikidata_property"] = preset["wikidata_property"]
    st.session_state["wkd_class"] = preset["wkd_class"]
    st.session_state["ignore_chars"] = preset["ignore_chars"]
    st.session_state["wdc_value_is_wikidata"] = preset["wdc_value_is_wikidata"]
    st.session_state["preset_applied"] = preset_name
if preset_name == "(none)":
    st.session_state["preset_applied"] = None

st.subheader("Recent runs")
jobs_for_presets = db.list_jobs(limit=50)
recent_presets = []
seen = set()
for job in jobs_for_presets:
    try:
        params = json.loads(job["params_json"])
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
    recent_presets.append((label, params))

recent_labels = [p[0] for p in recent_presets]
recent_choice = st.selectbox("Reopen previous config", ["(none)"] + recent_labels, key="recent_select")
if "recent_applied" not in st.session_state:
    st.session_state["recent_applied"] = None
if recent_choice != "(none)" and st.session_state["recent_applied"] != recent_choice:
    params = dict(recent_presets[recent_labels.index(recent_choice)][1])
    st.session_state["class_name"] = params.get("class_name", "")
    st.session_state["parts_spec"] = params.get("parts_spec", "")
    st.session_state["wdc_predicate_pattern"] = params.get("wdc_predicate_pattern", "")
    st.session_state["wikidata_property"] = params.get("wikidata_property", "")
    st.session_state["wkd_class"] = params.get("wkd_class", "")
    st.session_state["ignore_chars"] = params.get("ignore_chars", "spaces;-;.")
    st.session_state["wdc_value_is_wikidata"] = bool(params.get("wdc_value_is_wikidata", False))
    st.session_state["recent_applied"] = recent_choice
if recent_choice == "(none)":
    st.session_state["recent_applied"] = None

class_name = ""
if class_options:
    class_name = st.selectbox(
        "Class name",
        class_options,
        help="WDC class name (type to search).",
        key="class_name",
    )
else:
    class_name = st.text_input(
        "Class name",
        help="WDC class name (case-sensitive).",
        placeholder="AdministrativeArea",
        key="class_name",
    )

if class_name in class_meta:
    meta = class_meta[class_name]
    st.info(f"Parts: {meta['num_parts'] or 'N/A'} | Size: {meta['size_human'] or 'N/A'}")
    local_parts = _count_local_parts(str(Path("Download") / class_name))
    if local_parts > 0:
        st.success(f"Local parts downloaded: {local_parts}")
    else:
        st.warning("No local parts downloaded yet.")

if "ignore_chars" not in st.session_state:
    st.session_state["ignore_chars"] = "spaces;-;."
if "strict_matching" not in st.session_state:
    st.session_state["strict_matching"] = False

strict_matching = st.checkbox(
    "Strict matching (no normalization)",
    help="If enabled, no normalization is applied.",
    key="strict_matching",
)

with st.form("job_form"):
    col1, col2 = st.columns(2)
    with col1:
        parts_spec = st.text_input(
            "Parts spec",
            value="",
            help='Which WDC parts to use: "all" or a range like "1-5" or a list like "1,2,3".',
            placeholder="all / 1-5 / 1,2,3",
            key="parts_spec",
        )
        wdc_predicate_pattern = st.text_input(
            "WDC predicate pattern",
            value="",
            help="Substring matched against WDC predicates (e.g., eidr, name, telephone, isrc).",
            placeholder="eidr / name / telephone / isrc",
            key="wdc_predicate_pattern",
        )
        wdc_pattern_search_in = st.selectbox(
            "Pattern search scope",
            ["predicate", "value"],
            help="Choose whether pattern is matched in WDC predicate names or in WDC values.",
            key="wdc_pattern_search_in",
        )
        wikidata_property = st.text_input(
            "Wikidata property",
            value="",
            help="Wikidata property (e.g., P2704 or wdt:P2704, or rdfs:label). Leave empty if using 'WDC values are Wikidata URLs'.",
            placeholder="P2704 / wdt:P2704 / rdfs:label",
            key="wikidata_property",
        )

    with col2:
        wkd_class = st.text_input(
            "Wikidata class (QID)",
            value="",
            help="Optional class filter, e.g., Q33742. Required if WDC values are Wikidata URLs.",
            placeholder="Q33742",
            key="wkd_class",
        )
        max_depth = st.selectbox(
            "Max depth (bnodes)",
            [-1, 0, 1, 2, 3, 4, 5],
            help="Depth for following blank nodes when building BEAM. -1 means unlimited.",
            key="max_depth",
        )
        ignore_chars = st.text_input(
            "Normalization: extra chars to strip",
            help='Default: "spaces;-;." (enable normalization). You can list ASCII characters (e.g., ";" "." "-") or namespaces: "spaces" (all whitespace), "special-chars" (all non-alphanumeric). Separate with ";".',
            placeholder="spaces;-;.;special-chars",
            key="ignore_chars",
            disabled=st.session_state.get("strict_matching", False),
        )
        wdc_value_is_wikidata = st.checkbox(
            "WDC values are Wikidata URLs",
            help="Use when WDC values already contain Wikidata URLs (sameAs/url). Requires Wikidata class.",
            key="wdc_value_is_wikidata",
        )

    submit = st.form_submit_button("Generate benchmark")

if submit:
    params = {
        "class_name": class_name,
        "parts_spec": parts_spec,
        "wdc_predicate_pattern": wdc_predicate_pattern,
        "wdc_pattern_search_in": wdc_pattern_search_in,
        "wikidata_property": wikidata_property,
        "wkd_class": wkd_class,
        "ignore_chars": "" if strict_matching else ignore_chars,
        "wdc_value_is_wikidata": wdc_value_is_wikidata,
        "max_depth": max_depth,
    }
    job_id = db.insert_job(params)
    st.success(f"Job {job_id} queued")

st.divider()

st.subheader("Jobs")

jobs = db.list_jobs(limit=20)
for job in jobs:
    status = job["status"]
    with st.expander(f"Job {job['id']} - {status}", expanded=False):
        st.json(json.loads(job["params_json"]))
        st.write(f"Created: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(job['created_at']))}")
        if job["started_at"]:
            st.write(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(job['started_at']))}")
        if job["ended_at"]:
            st.write(f"Ended: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(job['ended_at']))}")
        if status == "running" and job["started_at"]:
            elapsed = int(time.time() - job["started_at"])
            st.write(f"Elapsed: {elapsed}s")
        if job["error_message"]:
            st.error(job["error_message"])
        log_path = job["log_path"]
        if log_path and Path(log_path).exists():
            log_file = Path(log_path)
            log_text = log_file.read_text(encoding="utf-8")
            st.write(f"Log file: {log_path}")
            st.write(f"Log size: {log_file.stat().st_size} bytes | Updated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(log_file.stat().st_mtime))}")
            # Try to extract latest percentage from logs
            pct = None
            for m in re.finditer(r"(\\d{1,3}\\.\\d)%", log_text[-20000:]):
                pct = float(m.group(1))
            if pct is not None:
                st.progress(min(100, max(0, int(pct))))
            # Show last lines for quick view
            lines = log_text.splitlines()
            tail_lines = lines[-50:] if len(lines) > 50 else lines
            st.text_area("Logs (tail)", "\n".join(tail_lines), height=220, key=f"logs_tail_{job['id']}")
            st.text_area("Logs (last 20k chars)", log_text[-20000:], height=200, key=f"logs_full_{job['id']}")
        result_path = job["result_path"]
        if result_path and Path(result_path).exists():
            st.success(f"Result: {result_path}")
