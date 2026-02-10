import json
import time
from pathlib import Path

import streamlit as st

from beam import db
from beam.wdc_classes import fetch_wdc_classes
from beam.pipeline import _count_local_parts


st.set_page_config(page_title="BEAM Benchmark", layout="wide")

db.init_db()

st.title("BEAM Benchmark Generator")

# Sidebar: refresh WDC classes
with st.sidebar:
    st.header("WDC Classes")
    if st.button("Reset cache"):
        db.clear_wdc_classes()
        st.success("Cache cleared")
    if st.button("Refresh classes"):
        with st.spinner("Fetching WDC classes..."):
            rows = fetch_wdc_classes()
            db.upsert_wdc_classes(rows)
        st.success("Classes updated")
    updated_at = db.latest_wdc_update()
    if updated_at:
        st.caption(time.strftime("Last update: %Y-%m-%d %H:%M:%S", time.localtime(updated_at)))

wdc_classes = db.list_wdc_classes()
if not wdc_classes:
    with st.spinner("Fetching WDC classes..."):
        rows = fetch_wdc_classes()
        if rows:
            db.upsert_wdc_classes(rows)
            wdc_classes = db.list_wdc_classes()
class_options = [r["class_name"] for r in wdc_classes]
class_meta = {r["class_name"]: dict(r) for r in wdc_classes}

class_name = ""
if class_options:
    class_label_map = {}
    for name in class_options:
        meta = class_meta.get(name, {})
        num_parts = meta.get("num_parts")
        if num_parts:
            label = f"{name} ({num_parts} parts)"
        else:
            label = f"{name} (parts N/A)"
        class_label_map[name] = label
    class_label_list = [class_label_map[name] for name in class_options]
    class_label = st.selectbox("Class (number of parts)", class_label_list)
    class_name = class_label.split(" (", 1)[0]
else:
    class_name = st.text_input("Class")

if class_name in class_meta:
    meta = class_meta[class_name]
    st.info(f"Parts: {meta['num_parts'] or 'N/A'} | Size: {meta['size_human'] or 'N/A'}")
    local_parts = _count_local_parts(str(Path("Download") / class_name))
    if local_parts > 0:
        st.success(f"Local parts downloaded: {local_parts}")
    else:
        st.warning("No local parts downloaded yet.")

with st.form("job_form"):
    col1, col2 = st.columns(2)
    with col1:
        parts_spec = st.text_input("Parts spec", value="all")
        match_mode = st.selectbox("Match mode", ["By Property", "By Label"])
        wdc_predicate_pattern = st.text_input("WDC predicate pattern", value="")
        wikidata_property = st.text_input("Wikidata property", value="")

    with col2:
        wkd_class = st.text_input("Wikidata class (QID)", value="")
        wkd_prop_class = st.text_input("Wikidata property class (QID)", value="")
        ignore_chars = st.text_input("Ignore chars (e.g. spaces;-;.)", value="")
        wdc_value_is_wikidata = st.checkbox("WDC values are Wikidata URLs")

    submit = st.form_submit_button("Generate benchmark")

if submit:
    params = {
        "class_name": class_name,
        "parts_spec": parts_spec,
        "wdc_predicate_pattern": wdc_predicate_pattern,
        "wikidata_property": wikidata_property,
        "wkd_class": wkd_class,
        "wkd_prop_class": wkd_prop_class,
        "ignore_chars": ignore_chars,
        "wdc_value_is_wikidata": wdc_value_is_wikidata,
        "match_mode": match_mode,
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
        if job["error_message"]:
            st.error(job["error_message"])
        log_path = job["log_path"]
        if log_path and Path(log_path).exists():
            st.text_area("Logs", Path(log_path).read_text(encoding="utf-8")[-5000:], height=200)
        result_path = job["result_path"]
        if result_path and Path(result_path).exists():
            st.success(f"Result: {result_path}")
