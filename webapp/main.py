import json
import os
import shutil
import tempfile
import time
import zipfile
import asyncio
import difflib
import re
from collections import Counter
from pathlib import Path
from functools import lru_cache
from typing import Optional
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from beam import db
from beam.wdc_classes import fetch_wdc_classes
from scripts import align as align_script

app = FastAPI()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

WDC_PARTS_BASE_URL = "https://data.dws.informatik.uni-mannheim.de/structureddata/2024-12/quads/classspecific/"
_PART_HREF_RE = re.compile(r"^part_(\d+)\.gz$", re.IGNORECASE)
_PART_NAME_RE = re.compile(r"^part_(\d+)(?:\.[A-Za-z0-9]+)?$", re.IGNORECASE)
_QUAD_RE = re.compile(
    r'^\s*(<[^>]+>|_:[^\s]+)\s+(<[^>]+>)\s+(".*?"(?:\^\^<[^>]+>|@[a-zA-Z-]+)?|<[^>]+>|_:[^\s]+)\s+(<[^>]+>)\s+\.\s*$'
)
_TRIPLE_RE = re.compile(
    r'^\s*(<[^>]+>|_:[^\s]+)\s+(<[^>]+>)\s+(".*?"(?:\^\^<[^>]+>|@[a-zA-Z-]+)?|<[^>]+>|_:[^\s]+)\s+\.\s*$'
)


PRESETS = {
    "testclass_large_benchmark": {
        "label": "TestClassLarge - property (label)",
        "matching_mode": "property",
        "class_name": "TestClassLarge",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q34770",
        "ignore_chars": "spaces;-;.",
        "force_align": False,
        "use_local_only": False,
    },
    "testclass_quick": {
        "label": "TestClass - property (label)",
        "matching_mode": "property",
        "class_name": "TestClass",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q34770",
        "ignore_chars": "spaces;-;.",
        "force_align": False,
        "use_local_only": False,
    },
    "testclass_label": {
        "label": "TestClassLabel - property (label)",
        "matching_mode": "property",
        "class_name": "TestClassLabel",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q34770",
        "ignore_chars": "spaces;-;.",
        "force_align": False,
        "use_local_only": False,
    },
    "testclass_identifier": {
        "label": "TestClassIdentifier - identifier code",
        "matching_mode": "identifier",
        "class_name": "TestClassIdentifier",
        "parts_spec": "all",
        "wdc_predicate_pattern": "eidr",
        "wikidata_property": "wdt:P2704",
        "wkd_class": "Q11424",
        "ignore_chars": "spaces;-;.",
        "force_align": False,
        "use_local_only": False,
    },
    "testclass_wikidata_url": {
        "label": "TestClassWikidataUrl - sameAs",
        "matching_mode": "sameas",
        "class_name": "TestClassWikidataUrl",
        "parts_spec": "all",
        "wdc_predicate_pattern": "url",
        "wikidata_property": "wdt:P31",
        "wkd_class": "Q515",
        "ignore_chars": "spaces;-;.",
        "force_align": False,
        "use_local_only": False,
    },
    "testclass_wikidata_sameas": {
        "label": "TestClassWikidataSameAs - sameAs",
        "matching_mode": "sameas",
        "class_name": "TestClassWikidataSameAs",
        "parts_spec": "all",
        "wdc_predicate_pattern": "sameas",
        "wikidata_property": "wdt:P31",
        "wkd_class": "Q6256",
        "ignore_chars": "spaces;-;.",
        "force_align": False,
        "use_local_only": False,
    },
    "code_movie": {
        "label": "Movie - via code (EIDR)",
        "matching_mode": "identifier",
        "class_name": "Movie",
        "parts_spec": "all",
        "wdc_predicate_pattern": "eidr",
        "wikidata_property": "wdt:P2704",
        "wkd_class": "Q11424",
        "ignore_chars": "spaces;-;.",
        "force_align": False,
        "use_local_only": False,
    },
    "label_language": {
        "label": "Language - property (label)",
        "matching_mode": "property",
        "class_name": "Language",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q33742",
        "ignore_chars": "spaces;-;.",
        "force_align": False,
        "use_local_only": False,
    },
    "property_college_or_university_telephone": {
        "label": "CollegeOrUniversity - property (telephone)",
        "matching_mode": "property",
        "class_name": "CollegeOrUniversity",
        "parts_spec": "all",
        "wdc_predicate_pattern": "telephone",
        "wikidata_property": "P1329",
        "wkd_class": "Q38723",
        "ignore_chars": "spaces;-;.",
        "force_align": False,
        "use_local_only": False,
    },
    "wikidata_link_city": {
        "label": "City - sameAs",
        "matching_mode": "sameas",
        "class_name": "City",
        "parts_spec": "all",
        "wdc_predicate_pattern": "sameAs",
        "wikidata_property": "",
        "wkd_class": "Q486972",
        "ignore_chars": "spaces;-;.",
        "force_align": False,
        "use_local_only": False,
    },
}

LEGACY_PRESET_ALIASES = {
    "property_movie": "code_movie",
}


def _default_form():
    return {
        "matching_mode": "property",
        "class_name": "",
        "parts_spec": "all",
        "wdc_predicate_pattern": "",
        "wikidata_property": "",
        "wkd_class": "",
        "ignore_chars": "spaces;-;.",
        "force_align": False,
        "use_local_only": False,
        "force_one_to_one_links": False,
        "dedup_wdc_exact_subgraph_by_link_value": False,
    }


def _clean_text(value: Optional[str]) -> str:
    return (value or "").strip()


def _normalize_matching_mode(value: Optional[str], fallback_wdc_value_is_wikidata: bool = False) -> str:
    mode = _clean_text(value).lower()
    if mode in {"property", "identifier", "sameas"}:
        return mode
    return "sameas" if bool(fallback_wdc_value_is_wikidata) else "property"


def _is_wikidata_url_mode(params: dict) -> bool:
    return _normalize_matching_mode(
        (params or {}).get("matching_mode"),
        fallback_wdc_value_is_wikidata=bool((params or {}).get("wdc_value_is_wikidata")),
    ) == "sameas"


def _validate_and_normalize_job_params(raw_params: dict):
    params = dict(raw_params or {})
    params["matching_mode"] = _normalize_matching_mode(
        params.get("matching_mode"),
        fallback_wdc_value_is_wikidata=bool(params.get("wdc_value_is_wikidata")),
    )
    params.pop("wdc_value_is_wikidata", None)
    params["class_name"] = _clean_text(params.get("class_name"))
    params["parts_spec"] = _clean_text(params.get("parts_spec")) or "all"
    params["wdc_predicate_pattern"] = _clean_text(params.get("wdc_predicate_pattern"))
    params["wikidata_property"] = _clean_text(params.get("wikidata_property"))
    params["wkd_class"] = _clean_text(params.get("wkd_class"))
    params["ignore_chars"] = _clean_text(params.get("ignore_chars"))
    params["force_align"] = bool(params.get("force_align"))
    params["use_local_only"] = bool(params.get("use_local_only"))
    params["force_one_to_one_links"] = bool(params.get("force_one_to_one_links"))
    params["dedup_wdc_exact_subgraph_by_link_value"] = bool(
        params.get("dedup_wdc_exact_subgraph_by_link_value")
    )

    if not params["class_name"]:
        return params, "Class name is required."
    if not params["wdc_predicate_pattern"]:
        return params, "WDC property pattern is required."

    if _is_wikidata_url_mode(params):
        params["wikidata_property"] = ""
        params["ignore_chars"] = ""
        if not params["wkd_class"]:
            return params, "Wikidata class (QID) is required when WDC values are Wikidata URLs."
    else:
        if not params["ignore_chars"]:
            params["ignore_chars"] = "spaces;-;."
        if not params["wikidata_property"]:
            return params, "Equivalent Wikidata property is required when WDC values are not Wikidata URLs."

    return params, None


def _is_test_class_name(class_name: Optional[str]) -> bool:
    name = _clean_text(class_name)
    if not name:
        return False
    lowered = name.lower()
    return lowered.startswith("testclass") or lowered.startswith("uxcheckclass")


def _is_test_preset(preset: dict) -> bool:
    if not isinstance(preset, dict):
        return False
    return _is_test_class_name(preset.get("class_name"))


def _filter_presets_by_mode(test_mode: bool):
    desired = bool(test_mode)
    return {k: v for k, v in PRESETS.items() if _is_test_preset(v) == desired}


def _get_recent_presets(limit=50, test_mode: Optional[bool] = None):
    rows = db.list_jobs(limit=limit)
    recent = []
    seen = set()
    for r in rows:
        try:
            params = json.loads(r["params_json"])
        except Exception:
            continue
        mode = _normalize_matching_mode(
            params.get("matching_mode"),
            fallback_wdc_value_is_wikidata=bool(params.get("wdc_value_is_wikidata")),
        )
        if test_mode is not None and _is_test_class_name(params.get("class_name")) != bool(test_mode):
            continue
        key = (
            mode,
            params.get("class_name", ""),
            params.get("parts_spec", ""),
            params.get("wdc_predicate_pattern", ""),
            params.get("wikidata_property", ""),
            params.get("wkd_class", ""),
            params.get("ignore_chars", ""),
            params.get("force_one_to_one_links", ""),
            params.get("dedup_wdc_exact_subgraph_by_link_value", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        label = (
            f"{params.get('class_name','')} | {params.get('parts_spec','')} | "
            f"{params.get('wdc_predicate_pattern','')} -> "
            f"{params.get('wikidata_property','') or ('Wikidata URL' if _is_wikidata_url_mode(params) else '')}"
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


def _looks_like_ent_links_header(line: str) -> bool:
    line = (line or "").strip()
    if not line:
        return False
    parts = line.split("\t")
    if len(parts) < 2:
        return False
    left = parts[0].strip().lower()
    right = parts[1].strip().lower()
    return left in {"wdc_iri", "wdc", "wdc_entity"} and right in {"wikidata_uri", "wikidata", "wikidata_entity"}


def _count_ent_links_rows(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    total = _count_lines(path)
    if total <= 0:
        return 0
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            first = f.readline()
        if _looks_like_ent_links_header(first):
            return max(0, total - 1)
    except Exception:
        pass
    return total


def _parse_nq_or_nt(line: str):
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    m = _QUAD_RE.match(line)
    if m:
        s, p, o, _g = m.groups()
        return s, p, o
    m = _TRIPLE_RE.match(line)
    if m:
        s, p, o = m.groups()
        return s, p, o
    return None


def _literal_lex(value: str):
    value = value or ""
    if not value.startswith('"'):
        return None
    escape = False
    for i in range(1, len(value)):
        ch = value[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            return value[1:i]
    return None


def _normalize_preflight_value(raw_value: str, ignore_chars_text: str):
    v = align_script.normalize_for_matching(raw_value or "")
    if not v:
        return ""
    try:
        extra = align_script.parse_strip_list(ignore_chars_text or "")
    except Exception:
        extra = set()
    if " " in extra:
        v = v.replace(" ", "")
    for ch in extra:
        if ch and ch != " ":
            v = v.replace(ch, "")
    return v


def _parse_parts_spec_numbers(parts_spec: str):
    spec = _clean_text(parts_spec) or "all"
    if spec.lower() == "all":
        return None, None
    wanted = set()
    try:
        if "," in spec:
            for token in spec.split(","):
                token = token.strip()
                if not token:
                    continue
                wanted.add(int(token))
        elif "-" in spec:
            left, right = spec.split("-", 1)
            start = int(left.strip())
            end = int(right.strip())
            if end < start:
                start, end = end, start
            for n in range(start, end + 1):
                wanted.add(n)
        else:
            wanted.add(int(spec.strip()))
    except Exception:
        return None, f"Invalid parts spec: '{parts_spec}'. Use all, 1-10, or 1,2,4."
    return sorted(wanted), None


def _discover_local_part_files(class_name: str):
    class_dir = Path("Download") / (class_name or "")
    if not class_dir.exists() or not class_dir.is_dir():
        return []
    files = []
    for fp in sorted(class_dir.iterdir()):
        if not fp.is_file():
            continue
        if not fp.name.startswith("part_"):
            continue
        if not (fp.name.endswith(".nq") or fp.name.endswith(".nt") or "." not in fp.name):
            continue
        files.append(fp)
    return files


def _select_local_part_files(class_name: str, parts_spec: str):
    files = _discover_local_part_files(class_name)
    if not files:
        return [], []
    wanted_numbers, parse_error = _parse_parts_spec_numbers(parts_spec)
    if parse_error:
        return [], [parse_error]
    if wanted_numbers is None:
        return files, []

    files_by_num = {}
    for fp in files:
        num = _part_number_from_name(fp.name)
        if num is None:
            continue
        files_by_num.setdefault(num, []).append(fp)

    selected = []
    missing = []
    for num in wanted_numbers:
        if num in files_by_num:
            selected.extend(files_by_num[num])
        else:
            missing.append(num)
    selected.sort(key=lambda p: p.name)

    warnings = []
    if missing:
        warnings.append(f"Requested local parts not found: {_format_part_ranges(missing)}.")
    if not selected:
        warnings.append("No local part file matches this parts spec.")
    return selected, warnings


def _read_top_props(path: Path, limit: int = 5):
    rows = []
    if not path.exists() or not path.is_file():
        return rows
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        header_skipped = False
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if not header_skipped:
                header_skipped = True
                first = parts[0].strip().lower() if parts else ""
                if first in {"predicate", "property"}:
                    continue
            prop = parts[0].strip() if parts else ""
            count_raw = parts[1].strip() if len(parts) > 1 else "0"
            try:
                count = int(count_raw)
            except Exception:
                count = 0
            label = parts[2].strip() if len(parts) > 2 else ""
            description = parts[3].strip() if len(parts) > 3 else ""
            rows.append(
                {
                    "property": prop,
                    "count": count,
                    "label": label,
                    "description": description,
                }
            )
            if len(rows) >= limit:
                break
    return rows


def _read_ent_links_samples(path: Path, limit: int = 5):
    rows = []
    if not path.exists() or not path.is_file():
        return rows
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            left = parts[0].strip()
            right = parts[1].strip()
            if _looks_like_ent_links_header(f"{left}\t{right}"):
                continue
            rows.append({"wdc_iri": left, "wikidata_uri": right})
            if len(rows) >= limit:
                break
    return rows


def _fetch_wikidata_preview_values(
    wikidata_property: str,
    wkd_class: str,
    ignore_chars: str,
    limit: int = 1200,
):
    prop = align_script.normalize_wikidata_property(wikidata_property)
    if not prop:
        return []
    wkd_class_norm = align_script.normalize_wkd_class(wkd_class)
    class_filter = ""
    if wkd_class_norm:
        class_filter = f"""
      ?entity wdt:P31 ?type .
      ?type wdt:P279* {wkd_class_norm} .
    """
    q_limit = max(100, min(int(limit), 5000))
    query = f"""
    PREFIX wd: <http://www.wikidata.org/entity/>
    PREFIX wdt: <http://www.wikidata.org/prop/direct/>
    SELECT DISTINCT ?value WHERE {{
      ?entity {prop} ?value .
      {class_filter}
    }}
    LIMIT {q_limit}
    """
    headers = {
        "Accept": "application/sparql-results+json",
        "User-Agent": "beam-preflight/1.0",
    }
    timeout_s = max(5, int(os.environ.get("PREFLIGHT_WIKIDATA_TIMEOUT", "25")))
    try:
        response = requests.post(
            align_script.WIKIDATA_ENDPOINT,
            data={"query": query, "format": "json"},
            headers=headers,
            timeout=timeout_s,
        )
        response.raise_for_status()
        loader = getattr(align_script, "_load_sparql_json_payload", None)
        if callable(loader):
            payload = loader(response.text)
        else:
            payload = json.loads(response.text)
    except Exception:
        return []

    rows = []
    seen_norm = set()
    bindings = (((payload or {}).get("results") or {}).get("bindings")) or []
    for item in bindings:
        value = str((((item or {}).get("value") or {}).get("value")) or "").strip()
        if not value:
            continue
        normalized = _normalize_preflight_value(value, ignore_chars)
        if not normalized or normalized in seen_norm:
            continue
        seen_norm.add(normalized)
        rows.append({"value": value[:180], "normalized": normalized})
        if len(rows) >= q_limit:
            break
    return rows


def _build_preflight_report(
    class_name: str,
    parts_spec: str,
    wdc_predicate_pattern: str,
    ignore_chars: str,
    matching_mode: str,
    use_local_only: bool,
    wikidata_property: str = "",
    wkd_class: str = "",
    include_wikidata_preview: bool = True,
    scan_limit_lines: int = 30000,
):
    class_name = _clean_text(class_name)
    parts_spec = _clean_text(parts_spec) or "all"
    pattern = _clean_text(wdc_predicate_pattern)
    ignore_chars = _clean_text(ignore_chars)
    mode_norm = _normalize_matching_mode(matching_mode)
    wdc_value_is_wikidata = mode_norm == "sameas"
    report = {
        "ok": False,
        "class_name": class_name,
        "parts_spec": parts_spec,
        "pattern": pattern,
        "matching_mode": mode_norm,
        "wdc_value_is_wikidata": bool(wdc_value_is_wikidata),
        "scan_limit_lines": int(max(1000, scan_limit_lines)),
        "selected_files_count": 0,
        "selected_files": [],
        "scanned_lines": 0,
        "matched_triples": 0,
        "distinct_values": 0,
        "wikidata_url_like": 0,
        "sample_values": [],
        "top_unmatched_wdc_values": [],
        "close_wikidata_examples": [],
        "top_predicates": [],
        "invalid_wikidata_samples": [],
        "wikidata_preview_count": 0,
        "risk": "high",
        "confidence": "low",
        "warnings": [],
        "summary": "",
    }

    if not class_name:
        report["summary"] = "Class name is required."
        return report
    if not pattern:
        report["summary"] = "WDC property pattern is required."
        return report

    selected_files, select_warnings = _select_local_part_files(class_name, parts_spec)
    report["warnings"].extend(select_warnings)
    if not selected_files:
        report["summary"] = "No local files available for preflight."
        return report

    selected_names = [fp.name for fp in selected_files]
    report["selected_files"] = selected_names[:20]
    report["selected_files_count"] = len(selected_files)
    if len(selected_names) > 20:
        report["warnings"].append(f"Preflight uses first 20 listed files out of {len(selected_names)} selected.")

    if not use_local_only:
        parts_info = _build_class_parts_info(class_name)
        missing_online = int(parts_info.get("not_downloaded_online_parts_count") or 0)
        if missing_online > 0:
            report["warnings"].append(
                "Preflight scans local files only; some online parts are not downloaded yet."
            )

    prepared_patterns = align_script.prepare_predicate_patterns(pattern)
    distinct_norm = set()
    value_counts = Counter()
    value_examples = {}
    predicate_counts = Counter()
    invalid_wikidata_samples = []
    sample_values = []
    wikidata_like_values = 0
    matched = 0
    scanned = 0
    scan_limit = int(max(1000, scan_limit_lines))

    for fp in selected_files:
        with fp.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if scanned >= scan_limit:
                    break
                scanned += 1
                parsed = _parse_nq_or_nt(line)
                if not parsed:
                    continue
                _s, p_tok, o_tok = parsed
                predicate = p_tok.strip("<>")
                predicate_counts[predicate] += 1
                if not align_script.predicate_matches_prepared_patterns(predicate, prepared_patterns):
                    continue

                matched += 1
                if o_tok.startswith('"'):
                    raw_value = _literal_lex(o_tok) or o_tok.strip('"')
                else:
                    raw_value = o_tok.strip("<>")
                if raw_value:
                    normalized = _normalize_preflight_value(raw_value, ignore_chars)
                    if normalized:
                        if normalized not in distinct_norm and len(sample_values) < 5:
                            sample_values.append(raw_value[:120])
                        value_counts[normalized] += 1
                        if normalized not in value_examples:
                            value_examples[normalized] = raw_value[:180]
                        distinct_norm.add(normalized)
                    if wdc_value_is_wikidata:
                        if align_script.extract_wd_entity_iri(raw_value):
                            wikidata_like_values += 1
                        elif len(invalid_wikidata_samples) < 5:
                            invalid_wikidata_samples.append(raw_value[:160])
            if scanned >= scan_limit:
                break

    report["scanned_lines"] = scanned
    report["matched_triples"] = matched
    report["distinct_values"] = len(distinct_norm)
    report["wikidata_url_like"] = wikidata_like_values
    report["sample_values"] = sample_values
    report["invalid_wikidata_samples"] = invalid_wikidata_samples
    report["top_unmatched_wdc_values"] = [
        {
            "normalized": norm,
            "value": value_examples.get(norm, norm),
            "count": int(cnt),
        }
        for norm, cnt in value_counts.most_common(8)
    ]

    if scanned >= scan_limit:
        report["warnings"].append(f"Sample limit reached ({scan_limit:,} lines).")
    if matched == 0:
        report["top_predicates"] = [
            {"predicate": pred, "count": int(cnt)}
            for pred, cnt in predicate_counts.most_common(8)
        ]
        report["risk"] = "high"
        report["summary"] = "No triple matched this WDC property pattern in sampled local data."
    elif wdc_value_is_wikidata and wikidata_like_values == 0:
        report["risk"] = "high"
        report["summary"] = "Pattern matched, but no Wikidata URL-like values were found."
    elif len(distinct_norm) < 5:
        report["risk"] = "medium"
        report["summary"] = "Very few distinct values found; alignment risk is moderate."
    else:
        report["risk"] = "low"
        report["summary"] = "Signal looks good in sampled local data."

    if scanned >= 20000:
        report["confidence"] = "high"
    elif scanned >= 5000:
        report["confidence"] = "medium"
    else:
        report["confidence"] = "low"

    if (
        include_wikidata_preview
        and not wdc_value_is_wikidata
        and _clean_text(wikidata_property)
        and report["top_unmatched_wdc_values"]
    ):
        preview_rows = _fetch_wikidata_preview_values(
            wikidata_property=_clean_text(wikidata_property),
            wkd_class=_clean_text(wkd_class),
            ignore_chars=ignore_chars,
            limit=1200,
        )
        report["wikidata_preview_count"] = len(preview_rows)
        if preview_rows:
            wd_norm_to_value = {}
            wd_norm_keys = []
            for row in preview_rows:
                norm = row.get("normalized")
                raw_value = row.get("value")
                if not norm:
                    continue
                if norm not in wd_norm_to_value:
                    wd_norm_to_value[norm] = raw_value
                    wd_norm_keys.append(norm)
            for row in report["top_unmatched_wdc_values"][:5]:
                norm = row.get("normalized")
                if not norm:
                    continue
                close_norms = difflib.get_close_matches(norm, wd_norm_keys, n=3, cutoff=0.72)
                if not close_norms:
                    continue
                report["close_wikidata_examples"].append(
                    {
                        "wdc_value": row.get("value"),
                        "wdc_count": row.get("count"),
                        "wikidata_candidates": [wd_norm_to_value[n] for n in close_norms],
                    }
                )
        else:
            report["warnings"].append("Could not fetch Wikidata preview values for preflight diagnostics.")

    report["ok"] = True
    return report


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
    links_count = _count_ent_links_rows(files["ent_links"])
    wd_props = max(0, _count_lines(files["prop_stats_wd"]) - 1)
    wdc_props = max(0, _count_lines(files["prop_stats_wdc"]) - 1)
    top_wdc_props = _read_top_props(files["prop_stats_wdc"], limit=5)
    top_wd_props = _read_top_props(files["prop_stats_wd"], limit=5)
    sample_links = _read_ent_links_samples(files["ent_links"], limit=5)
    qa_warnings = []
    if links_count == 0:
        qa_warnings.append("No entity links generated.")
    if wdc_props == 0:
        qa_warnings.append("No WDC property stats found.")
    if wd_props == 0:
        qa_warnings.append("No Wikidata property stats found.")
    if links_count > 0 and not sample_links:
        qa_warnings.append("Could not read ent_links samples.")
    return {
        "name": variant,
        "path": str(p),
        "size_total_b": size_total,
        "size_total_h": _fmt_size(size_total),
        "links_count": links_count,
        "wd_props": wd_props,
        "wdc_props": wdc_props,
        "sample_links": sample_links,
        "top_wdc_props": top_wdc_props,
        "top_wd_props": top_wd_props,
        "qa_warnings": qa_warnings,
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
        ("Matching", ["matching_mode", "wdc_predicate_pattern", "wikidata_property", "wkd_class", "ignore_chars"]),
        (
            "Build",
            [
                "force_align",
                "use_local_only",
                "force_one_to_one_links",
                "dedup_wdc_exact_subgraph_by_link_value",
                "build_name",
                "result_path",
            ],
        ),
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


def _normalized_path_text(value: str) -> str:
    raw = _clean_text(value)
    if not raw:
        return ""
    try:
        # Normalize both relative and absolute paths to the same canonical form.
        return str(Path(raw).expanduser().resolve())
    except Exception:
        return os.path.normpath(raw)


def _build_result_path_aliases(build_dir: Path):
    aliases = set()
    try:
        resolved = build_dir.resolve()
    except Exception:
        resolved = build_dir

    for candidate in (build_dir, resolved):
        txt = _clean_text(str(candidate))
        if not txt:
            continue
        aliases.add(txt)
        aliases.add(os.path.normpath(txt))

    try:
        cwd_resolved = Path.cwd().resolve()
        rel = resolved.relative_to(cwd_resolved)
        rel_txt = str(rel)
        aliases.add(rel_txt)
        aliases.add(os.path.normpath(rel_txt))
        aliases.add(f"./{rel_txt}")
    except Exception:
        pass

    normalized = {_clean_text(a.rstrip("/\\")) for a in aliases if _clean_text(a)}
    return {a for a in normalized if a}


def _delete_jobs_for_build_dir(build_dir: Path, scan_limit: int = 50000) -> int:
    aliases = _build_result_path_aliases(build_dir)
    target_norm = _normalized_path_text(str(build_dir))
    to_delete_ids = set()

    # Delete exact-path variants without relying on recency limits.
    for alias in aliases:
        try:
            db.delete_jobs_by_result_path(alias)
        except Exception:
            continue

    # Fallback for unusual historical path spellings that still point to the same directory.
    for row in db.list_jobs(limit=scan_limit):
        try:
            rp = _clean_text(row["result_path"])
        except Exception:
            rp = ""
        if not rp:
            continue
        if rp in aliases or os.path.normpath(rp) in aliases or _normalized_path_text(rp) == target_norm:
            try:
                to_delete_ids.add(int(row["id"]))
            except Exception:
                continue

    for jid in to_delete_ids:
        try:
            db.delete_job(jid)
        except Exception:
            continue
    return len(to_delete_ids)


def _bool_from_any(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _find_job_params_by_result_path(result_path: str, limit: int = 4000):
    target = str(result_path or "").strip()
    if not target:
        return None
    for row in db.list_jobs(limit=limit):
        rp = str(row["result_path"] or "").strip()
        if rp != target:
            continue
        params = _safe_json_loads(row["params_json"])
        if isinstance(params, dict) and params:
            return params
    return None


def _rerun_params_from_build_config(build_dir: Path, class_name: str):
    cfg_path = build_dir / "BUILD_CONFIG.json"
    cfg = {}
    if cfg_path.exists() and cfg_path.is_file():
        cfg = _safe_json_loads(cfg_path.read_text(encoding="utf-8"))
    cfg = cfg if isinstance(cfg, dict) else {}
    fallback = _find_job_params_by_result_path(str(build_dir))
    fallback = fallback if isinstance(fallback, dict) else {}

    def _pick(key, default=""):
        v = cfg.get(key, None)
        if v is None and fallback:
            v = fallback.get(key, None)
        if v is None:
            v = default
        return v

    raw_params = {
        "matching_mode": _normalize_matching_mode(
            _clean_text(str(_pick("matching_mode", ""))),
            fallback_wdc_value_is_wikidata=_bool_from_any(_pick("wdc_value_is_wikidata", False)),
        ),
        "class_name": _clean_text(str(_pick("class_name", class_name))),
        "parts_spec": _clean_text(str(_pick("parts_spec", "all"))),
        "wdc_predicate_pattern": _clean_text(str(_pick("wdc_predicate_pattern", ""))),
        "wikidata_property": _clean_text(str(_pick("wikidata_property", ""))),
        "wkd_class": _clean_text(str(_pick("wkd_class", ""))),
        "ignore_chars": _clean_text(str(_pick("ignore_chars", "spaces;-;."))),
        "force_align": _bool_from_any(_pick("force_align", False)),
        "use_local_only": _bool_from_any(_pick("use_local_only", False)),
        "force_one_to_one_links": _bool_from_any(_pick("force_one_to_one_links", False)),
        "dedup_wdc_exact_subgraph_by_link_value": _bool_from_any(
            _pick("dedup_wdc_exact_subgraph_by_link_value", False)
        ),
    }
    return _validate_and_normalize_job_params(raw_params)


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


def _build_dashboard_state(job_limit: int = 50, build_limit: int = 40, test_mode: Optional[bool] = None):
    all_jobs = [dict(j) for j in db.list_jobs(limit=job_limit)]
    jobs_by_id = {j["id"]: j for j in all_jobs}
    # Always include truly active jobs even if they are outside the recency window.
    for st in ("running", "queued"):
        for row in db.list_jobs_by_status(st):
            jid = row["id"]
            if jid not in jobs_by_id:
                jobs_by_id[jid] = dict(row)
    all_jobs = sorted(jobs_by_id.values(), key=lambda r: int(r.get("id") or 0), reverse=True)
    all_jobs_params = {j["id"]: _safe_json_loads(j.get("params_json")) for j in all_jobs}
    if test_mode is not None:
        desired = bool(test_mode)
        all_jobs = [
            j for j in all_jobs
            if _is_test_class_name(all_jobs_params.get(j["id"], {}).get("class_name")) == desired
        ]
    active_jobs = [j for j in all_jobs if j["status"] in {"running", "queued"}]
    builds = _scan_builds(limit=build_limit)
    if test_mode is not None:
        desired = bool(test_mode)
        builds = [b for b in builds if _is_test_class_name(b.get("class_name")) == desired]

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
        jobs_params[jid] = all_jobs_params.get(jid, {})
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

    # Keep done jobs visible when there is no downloadable build output,
    # except dangling rows where result_path points to a deleted/non-existent build dir.
    jobs_for_panel = []
    for j in all_jobs:
        if j["status"] != "done":
            jobs_for_panel.append(j)
            continue
        out = jobs_outputs.get(j["id"], {})
        if out.get("build_done"):
            continue
        result_path = _clean_text(j.get("result_path"))
        if result_path:
            try:
                if not Path(result_path).exists():
                    continue
            except Exception:
                pass
        jobs_for_panel.append(j)

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
def index(
    request: Request,
    preset: Optional[str] = None,
    recent: Optional[int] = None,
    form_error: Optional[str] = None,
    test_mode: Optional[str] = None,
):
    is_test_mode = _bool_from_any(test_mode)
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
    visible_presets = _filter_presets_by_mode(is_test_mode)
    selected_preset = ""
    if preset:
        canonical_preset = LEGACY_PRESET_ALIASES.get(preset, preset)
        if canonical_preset in visible_presets:
            form.update(visible_presets[canonical_preset])
            selected_preset = canonical_preset

    if recent:
        job = db.get_job(recent)
        if job:
            try:
                params = json.loads(job["params_json"])
                if _is_test_class_name(params.get("class_name")) == is_test_mode:
                    form.update(params)
            except Exception:
                pass

    form["matching_mode"] = _normalize_matching_mode(
        form.get("matching_mode"),
        fallback_wdc_value_is_wikidata=bool(form.get("wdc_value_is_wikidata")),
    )

    wdc_classes = [dict(r) for r in db.list_wdc_classes()]
    wdc_classes = [r for r in wdc_classes if _is_test_class_name(r.get("class_name")) == is_test_mode]
    class_meta = {r["class_name"]: r for r in wdc_classes}

    class_parts_info = None
    if form.get("class_name") and form.get("class_name") in class_meta:
        class_parts_info = _build_class_parts_info(form["class_name"])

    recent_presets = _get_recent_presets(test_mode=is_test_mode)
    dashboard = _build_dashboard_state(job_limit=50, build_limit=40, test_mode=is_test_mode)
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
            "presets": visible_presets,
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
            "form_error": _clean_text(form_error),
            "is_test_mode": is_test_mode,
        },
    )


@app.get("/api/dashboard")
def dashboard_api(job_limit: int = 80, build_limit: int = 40, test_mode: Optional[bool] = None):
    job_limit = max(1, min(int(job_limit), 200))
    build_limit = max(1, min(int(build_limit), 200))
    dashboard = _build_dashboard_state(job_limit=job_limit, build_limit=build_limit, test_mode=test_mode)

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


@app.get("/api/preflight")
def preflight_api(
    class_name: str,
    parts_spec: str = "all",
    matching_mode: str = "property",
    wdc_predicate_pattern: str = "",
    wikidata_property: str = "",
    wkd_class: str = "",
    ignore_chars: str = "",
    use_local_only: bool = False,
    include_wikidata_preview: bool = True,
    scan_limit_lines: int = 30000,
):
    return _build_preflight_report(
        class_name=class_name,
        parts_spec=parts_spec,
        matching_mode=matching_mode,
        wdc_predicate_pattern=wdc_predicate_pattern,
        wikidata_property=wikidata_property,
        wkd_class=wkd_class,
        ignore_chars=ignore_chars,
        use_local_only=bool(use_local_only),
        include_wikidata_preview=bool(include_wikidata_preview),
        scan_limit_lines=int(scan_limit_lines),
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
    matching_mode: str = Form("property"),
    class_name: str = Form(...),
    parts_spec: str = Form(""),
    wdc_predicate_pattern: str = Form(""),
    wikidata_property: str = Form(""),
    wkd_class: str = Form(""),
    ignore_chars: str = Form(""),
    force_align: Optional[str] = Form(None),
    use_local_only: Optional[str] = Form(None),
    force_one_to_one_links: Optional[str] = Form(None),
    dedup_wdc_exact_subgraph_by_link_value: Optional[str] = Form(None),
):
    raw_params = {
        "matching_mode": _clean_text(matching_mode),
        "class_name": _clean_text(class_name),
        "parts_spec": _clean_text(parts_spec),
        "wdc_predicate_pattern": _clean_text(wdc_predicate_pattern),
        "wikidata_property": _clean_text(wikidata_property),
        "wkd_class": _clean_text(wkd_class),
        "ignore_chars": _clean_text(ignore_chars),
        "force_align": bool(force_align),
        "use_local_only": bool(use_local_only),
        "force_one_to_one_links": bool(force_one_to_one_links),
        "dedup_wdc_exact_subgraph_by_link_value": bool(dedup_wdc_exact_subgraph_by_link_value),
    }
    params, validation_error = _validate_and_normalize_job_params(raw_params)
    if validation_error:
        return RedirectResponse(url=f"/?form_error={quote_plus(validation_error)}", status_code=303)
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
        _delete_jobs_for_build_dir(build_dir)
    except Exception:
        pass
    shutil.rmtree(build_dir, ignore_errors=True)
    return RedirectResponse(url="/", status_code=303)


@app.post("/builds/purge_low_links")
def purge_low_link_builds(max_links: int = Form(10)):
    try:
        threshold = int(max_links)
    except Exception:
        threshold = 10
    threshold = max(0, threshold)

    purged = 0
    # Use a high scan limit so this action can clean the full history.
    for build in _scan_builds(limit=100000):
        class_name = str(build.get("class_name") or "").strip()
        build_name = str(build.get("build_name") or "").strip()
        if not class_name or not build_name:
            continue
        variant = build.get("with_link") or build.get("without_link")
        if not isinstance(variant, dict):
            continue
        try:
            links_count = int(variant.get("links_count") or 0)
        except Exception:
            links_count = 0
        if links_count >= threshold:
            continue
        build_dir = _resolve_build_dir(class_name, build_name)
        if not build_dir:
            continue
        try:
            _delete_jobs_for_build_dir(build_dir)
        except Exception:
            pass
        shutil.rmtree(build_dir, ignore_errors=True)
        purged += 1
    return RedirectResponse(url=f"/?purged={purged}", status_code=303)


@app.post("/builds/{class_name}/{build_name}/rerun")
def rerun_build_from_build_card(class_name: str, build_name: str):
    build_dir = _resolve_build_dir(class_name, build_name)
    if not build_dir:
        return RedirectResponse(url="/", status_code=303)
    try:
        params, validation_error = _rerun_params_from_build_config(build_dir, class_name)
        if validation_error:
            msg = f"Cannot rerun build: {validation_error}"
            return RedirectResponse(url=f"/?form_error={quote_plus(msg)}", status_code=303)
        db.insert_job(params)
    except Exception as exc:
        msg = f"Cannot rerun build: {exc}"
        return RedirectResponse(url=f"/?form_error={quote_plus(msg)}", status_code=303)
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
                    "final_links_count": job["final_links_count"],
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
