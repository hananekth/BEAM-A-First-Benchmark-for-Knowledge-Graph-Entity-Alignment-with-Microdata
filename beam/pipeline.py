import os
import json
import hashlib
import time
import errno
import re
import unicodedata
import gzip
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from scripts import align
from scripts import build_beam_files as build


class PipelineError(RuntimeError):
    pass


def _discover_wdc_files(download_dir):
    candidates = []
    if os.path.isdir(download_dir):
        for name in sorted(os.listdir(download_dir)):
            if name.startswith("part_") and (
                name.endswith(".nq") or name.endswith(".nt") or "." not in name
            ):
                candidates.append(os.path.join(download_dir, name))
    return candidates


def _count_local_parts(download_dir):
    if not os.path.isdir(download_dir):
        return 0
    count = 0
    for name in os.listdir(download_dir):
        if name.startswith("part_") and (name.endswith(".nq") or name.endswith(".nt") or "." not in name):
            count += 1
    return count


def _select_local_part_files(download_dir, parts_spec):
    files = [Path(p) for p in _discover_wdc_files(download_dir)]
    if not files:
        return []
    if (parts_spec or "").lower() == "all":
        return files
    wanted_parts = set(align.parse_parts_spec(parts_spec, available_parts=None))
    selected = []
    for fp in files:
        name = fp.name
        if not name.startswith("part_"):
            continue
        base = name.split(".", 1)[0]
        if f"{base}.gz" in wanted_parts:
            selected.append(fp)
    return selected


def _fmt_size(num_bytes):
    units = ["B", "KB", "MB", "GB", "TB"]
    n = float(max(0, int(num_bytes)))
    for unit in units:
        if n < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{int(num_bytes)} B"


def _timestamp_tag():
    return time.strftime("%Y%m%d_%H%M%S")


def _looks_like_ent_links_header(line):
    text = str(line or "").strip().lower()
    if not text:
        return False
    parts = text.split("\t")
    if len(parts) < 2:
        return False
    left = parts[0].strip()
    right = parts[1].strip()
    return (
        (left in {"wdc", "wdc_iri", "wdc_entity"} and right in {"wikidata", "wikidata_uri", "target", "target_uri"})
        or (left == "subject" and right == "object")
    )


def _count_ent_links_rows(path):
    fp = Path(path)
    if not fp.exists() or not fp.is_file():
        return 0
    count = 0
    with fp.open("r", encoding="utf-8", errors="ignore") as f:
        header_checked = False
        for raw in f:
            line = raw.rstrip("\n")
            if not line:
                continue
            if not header_checked:
                header_checked = True
                if _looks_like_ent_links_header(line):
                    continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            count += 1
    return count


def _config_hash(align_params):
    payload = json.dumps(align_params, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_RUNTIME_ONLY_PARAM_KEYS = {
    # Runtime/recovery controls (not user-visible benchmark config)
    "require_cached_align",
    "resume_build",
    "resume_out_dir",
    "resume_checkpoint_at",
    "resume_checkpoint_reason",
    "resume_checkpoint_step",
}


def _full_config_for_cache(params):
    data = params if isinstance(params, dict) else {}
    out = {}
    for k, v in data.items():
        if k in _RUNTIME_ONLY_PARAM_KEYS:
            continue
        out[k] = v
    return out


def _full_config_hash(params):
    payload = json.dumps(_full_config_for_cache(params), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _normalize_matching_mode(value, fallback_wdc_value_is_wikidata=False):
    mode = str(value or "").strip().lower()
    if mode in {"property", "sameas", "sameas_or_property"}:
        return mode
    return "sameas" if bool(fallback_wdc_value_is_wikidata) else "property"


def _mode_includes_sameas(mode):
    return _normalize_matching_mode(mode) in {"sameas", "sameas_or_property"}


def _mode_includes_property(mode):
    return _normalize_matching_mode(mode) in {"property", "sameas_or_property"}


def _normalize_wdc_pattern_search_in(value):
    mode = str(value or "predicate").strip().lower()
    if mode in {"value", "object"}:
        return "value"
    return "predicate"


def _is_wikidata_url_mode(params):
    data = params if isinstance(params, dict) else {}
    return _normalize_matching_mode(
        data.get("matching_mode"),
        fallback_wdc_value_is_wikidata=bool(data.get("wdc_value_is_wikidata")),
    ) == "sameas"


def _parse_property_mapping_rules(value):
    text = str(value or "").strip()
    if not text:
        return []
    rules = []
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        line = str(raw_line or "").strip()
        if not line:
            continue
        norm = ""
        mapping_text = line
        if "||" in line:
            mapping_text, norm = line.split("||", 1)
            mapping_text = str(mapping_text or "").strip()
            norm = str(norm or "").strip()
        if "=>" not in mapping_text:
            raise PipelineError(
                f"Invalid property mapping rule at line {line_no}: expected 'wdc_prop[,wdc_prop] => target_prop[,target_prop]'"
            )
        left_raw, right_raw = mapping_text.split("=>", 1)
        wdc_props = [tok.strip() for tok in left_raw.split(",") if tok.strip()]
        target_props = [tok.strip() for tok in right_raw.split(",") if tok.strip()]
        if not wdc_props:
            raise PipelineError(
                f"Invalid property mapping rule at line {line_no}: left side must contain at least one property"
            )
        pair_ignore_chars = []
        pair_search_in = []
        row_mode = "property"
        norm_text = str(norm or "").strip()
        if norm_text.startswith("["):
            try:
                decoded = json.loads(norm_text)
            except Exception:
                decoded = None
            if isinstance(decoded, list):
                pair_ignore_chars = [str(v or "").strip() for v in decoded]
        elif norm_text.startswith("{"):
            try:
                decoded = json.loads(norm_text)
            except Exception:
                decoded = None
            if isinstance(decoded, dict):
                raw_ignore = decoded.get("ignore_chars")
                if isinstance(raw_ignore, list):
                    pair_ignore_chars = [str(v or "").strip() for v in raw_ignore]
                raw_search = decoded.get("search_in")
                if isinstance(raw_search, list):
                    pair_search_in = [_normalize_wdc_pattern_search_in(v) for v in raw_search]
                raw_mode = str(decoded.get("mode") or "").strip().lower()
                if raw_mode in {"sameas", "property"}:
                    row_mode = raw_mode
        if row_mode == "property":
            if not target_props:
                raise PipelineError(
                    f"Invalid property mapping rule at line {line_no}: right side must contain at least one property"
                )
            if len(wdc_props) != len(target_props):
                raise PipelineError(
                    f"Invalid property mapping rule at line {line_no}: left/right property counts differ"
                )
        else:
            target_props = [""] * len(wdc_props)
        if pair_ignore_chars and len(pair_ignore_chars) != len(wdc_props):
            raise PipelineError(
                f"Invalid property mapping rule at line {line_no}: per-pair normalization count differs from pair count"
            )
        if pair_search_in and len(pair_search_in) != len(wdc_props):
            raise PipelineError(
                f"Invalid property mapping rule at line {line_no}: per-pair search mode count differs from pair count"
            )
        pairs = list(zip(wdc_props, target_props))
        rules.append(
            {
                "line_no": line_no,
                "pairs": pairs,
                "raw": line,
                "ignore_chars": norm,
                "pair_ignore_chars": pair_ignore_chars,
                "pair_search_in": pair_search_in,
                "mode": row_mode,
            }
        )
    return rules


def _split_target_property_alternatives(value):
    raw = str(value or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    return parts or [raw]


def _merge_value_maps(dst_map, src_map):
    if not isinstance(src_map, dict):
        return
    for norm, entries in src_map.items():
        if norm not in dst_map:
            dst_map[norm] = []
        dst_map[norm].extend(list(entries or []))


def _distinct_wdc_values(wdc_map):
    out = set()
    if not isinstance(wdc_map, dict):
        return []
    for entries in wdc_map.values():
        for pair in list(entries or []):
            if not isinstance(pair, (list, tuple)) or len(pair) < 1:
                continue
            value = str(pair[0] or "").strip()
            if value:
                out.add(value)
    return sorted(out)


def _normalize_link_source_key(raw):
    text = str(raw or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if "wikidata.org/wiki" in lowered or "wikidata.org/entity" in lowered:
        return "wikidata"
    if lowered.startswith("<") and lowered.endswith(">"):
        lowered = lowered[1:-1]
    for sep in ("#", "/"):
        if sep in lowered:
            lowered = lowered.rsplit(sep, 1)[-1]
    lowered = lowered.replace("http://", "").replace("https://", "").strip()
    return lowered or text


def _source_label_from_method(method, fallback_pattern=""):
    method_text = str(method or "").strip()
    fallback = _normalize_link_source_key(fallback_pattern)
    token = ""
    if method_text:
        token = method_text.split("|")[-1].strip()
    if token.lower().startswith("sameas:"):
        source_key = _normalize_link_source_key(token.split(":", 1)[1])
    elif "->" in token:
        source_key = _normalize_link_source_key(token.split("->", 1)[0])
    elif token and token.lower() not in {"exact", "fuzzy"}:
        source_key = _normalize_link_source_key(token)
    else:
        source_key = fallback or "unknown"
    if not source_key:
        source_key = "unknown"
    return f"via {source_key}"


def _build_pair_source_map(matches, fallback_pattern=""):
    out = {}
    for item in list(matches or []):
        wdc_iri = str((item or {}).get("wdc_iri") or "").strip()
        wd_iri = str((item or {}).get("wikidata_uri") or "").strip()
        if not wdc_iri or not wd_iri:
            continue
        key = (wdc_iri, wd_iri)
        if key in out:
            continue
        out[key] = _source_label_from_method((item or {}).get("method"), fallback_pattern=fallback_pattern)
    return out


def _count_sources_for_pairs(wdc_entities, wd_entities, pair_source_map):
    counts = {}
    total = min(len(wdc_entities or []), len(wd_entities or []))
    for i in range(total):
        pair = (str(wdc_entities[i] or "").strip(), str(wd_entities[i] or "").strip())
        if not pair[0] or not pair[1]:
            continue
        source = str((pair_source_map or {}).get(pair) or "via unknown")
        counts[source] = counts.get(source, 0) + 1
    rows = [{"source": src, "count": int(cnt)} for src, cnt in counts.items()]
    rows.sort(key=lambda x: (-x["count"], x["source"]))
    return rows


def _pair_source_map_from_links_tsv(path, fallback_pattern=""):
    out = {}
    tsv_path = Path(path)
    if not tsv_path.exists() or not tsv_path.is_file():
        return out
    try:
        with tsv_path.open("r", encoding="utf-8", errors="ignore") as f:
            header = f.readline().rstrip("\n").split("\t")
            idx_wdc = header.index("wdc_iri")
            idx_wd = header.index("wikidata_uri")
            idx_method = header.index("method") if "method" in header else -1
            for raw in f:
                parts = raw.rstrip("\n").split("\t")
                if idx_wdc >= len(parts) or idx_wd >= len(parts):
                    continue
                pair = (parts[idx_wdc].strip(), parts[idx_wd].strip())
                if not pair[0] or not pair[1] or pair in out:
                    continue
                method = parts[idx_method].strip() if idx_method >= 0 and idx_method < len(parts) else ""
                out[pair] = _source_label_from_method(method, fallback_pattern=fallback_pattern)
    except Exception:
        return {}
    return out


def _should_prefilter_wikidata_by_wdc_values(target_endpoint, target_property, target_class):
    if align.normalize_target_endpoint_key(target_endpoint) != "wikidata":
        return False
    if not str(target_class or "").strip():
        return False
    normalized_prop = align.normalize_wikidata_property(target_property)
    return str(normalized_prop or "").strip().lower() == "wdt:p297"


def _fetch_wikidata_values_for_alignment(target_property, target_class, wkd_prop_class, wdc_map=None):
    if _should_prefilter_wikidata_by_wdc_values("wikidata", target_property, target_class):
        candidate_values = _distinct_wdc_values(wdc_map)
        if candidate_values:
            print(
                "[INFO] Wikidata prefilter enabled for P297: "
                f"{len(candidate_values):,} WDC value(s) sent as VALUES batches."
            )
            try:
                return align.fetch_wikidata_values(
                    target_property,
                    target_class,
                    wkd_prop_class,
                    value_candidates=candidate_values,
                )
            except TypeError:
                # Backward compatibility for tests/stubs that do not accept the new kwarg.
                pass
    return align.fetch_wikidata_values(
        target_property,
        target_class,
        wkd_prop_class,
    )


def _fetch_target_values_for_alignment(
    target_property,
    target_class,
    target_prop_class,
    target_endpoint,
    target_endpoint_url,
    target_prefixes,
    wdc_map=None,
):
    candidate_values = _distinct_wdc_values(wdc_map)
    if candidate_values:
        try:
            return align.fetch_target_values(
                target_property,
                target_class,
                target_prop_class,
                value_candidates=candidate_values,
                target_endpoint=target_endpoint,
                target_endpoint_url=target_endpoint_url,
                target_prefixes=target_prefixes,
            )
        except TypeError:
            # Backward compatibility for tests/stubs that do not accept value_candidates.
            pass
    return align.fetch_target_values(
        target_property,
        target_class,
        target_prop_class,
        target_endpoint=target_endpoint,
        target_endpoint_url=target_endpoint_url,
        target_prefixes=target_prefixes,
    )


def _set_align_normalization(ignore_spec):
    spec = str(ignore_spec or "").strip()
    if spec:
        align.set_normalization(True)
        align.set_extra_strip_chars(align.parse_strip_list(spec))
    else:
        align.set_normalization(False)


def _align_params_from_job_params(params):
    data = params if isinstance(params, dict) else {}
    wdc_value_is_wikidata = _normalize_matching_mode(
        data.get("matching_mode"),
        fallback_wdc_value_is_wikidata=bool(data.get("wdc_value_is_wikidata")),
    ) == "sameas"
    target_property = data.get("target_property")
    if target_property in {None, ""}:
        target_property = data.get("wikidata_property")
    target_class = data.get("target_class")
    if target_class in {None, ""}:
        target_class = data.get("wkd_class")
    target_endpoint = data.get("target_endpoint") or "wikidata"
    target_endpoint_url = data.get("target_endpoint_url") or None
    target_prefixes = data.get("target_prefixes") or None
    property_mapping_rules = data.get("property_mapping_rules") or None
    out = {
        "class_name": data.get("class_name"),
        "parts_spec": data.get("parts_spec") or "all",
        "pattern": data.get("wdc_predicate_pattern"),
        "pattern_search_in": _normalize_wdc_pattern_search_in(data.get("wdc_pattern_search_in")),
        "wikidata_property": target_property or None,
        "wkd_class": target_class or None,
        "ignore_chars": data.get("ignore_chars") or None,
        "wdc_value_is_wikidata": bool(wdc_value_is_wikidata),
    }
    # Add endpoint-specific keys only when not using the historical default config.
    if (target_endpoint or "wikidata") != "wikidata" or (target_endpoint_url or ""):
        out["target_property"] = target_property or None
        out["target_class"] = target_class or None
        out["target_endpoint"] = target_endpoint or "wikidata"
        out["target_endpoint_url"] = target_endpoint_url or None
    if target_prefixes:
        out["target_prefixes"] = target_prefixes
    if property_mapping_rules:
        out["property_mapping_rules"] = property_mapping_rules
    return out


def _align_cache_dir_for_params(params):
    align_params = _align_params_from_job_params(params)
    class_name = str(align_params.get("class_name") or "").strip()
    if not class_name:
        return None, align_params
    cache_hash = _config_hash(align_params)
    cache_dir = Path("Download") / class_name / "align_cache" / cache_hash
    return cache_dir, align_params


def _wdc_extract_sources_manifest(paths):
    manifest = []
    for raw in list(paths or []):
        fp = Path(raw)
        try:
            st = fp.stat()
            size_b = int(st.st_size)
            mtime_ns = int(st.st_mtime_ns)
        except Exception:
            size_b = 0
            mtime_ns = 0
        manifest.append(
            {
                "path": str(fp),
                "name": fp.name,
                "size_bytes": size_b,
                "mtime_ns": mtime_ns,
            }
        )
    manifest.sort(key=lambda row: row.get("path") or "")
    return manifest


def _wdc_extract_cache_hash(
    class_name,
    parts_spec,
    pattern,
    search_in,
    wdc_value_is_wd_iri,
    type_filter_iris,
    ignore_chars,
    sources_manifest,
):
    payload = {
        "class_name": str(class_name or "").strip(),
        "parts_spec": str(parts_spec or "all"),
        "pattern": str(pattern or ""),
        "search_in": _normalize_wdc_pattern_search_in(search_in),
        "wdc_value_is_wd_iri": bool(wdc_value_is_wd_iri),
        "type_filter_iris": sorted({str(v or "").strip() for v in list(type_filter_iris or []) if str(v or "").strip()}),
        "ignore_chars": str(ignore_chars or "").strip(),
        "sources_manifest": list(sources_manifest or []),
    }
    return _config_hash(payload)


def _wdc_extract_cache_paths(work_dir, cache_hash):
    base = Path(work_dir) / "wdc_extract_cache" / str(cache_hash)
    return {
        "base": base,
        "meta": base / "WDC_EXTRACT_META.json",
        "data": base / "WDC_EXTRACT_MAP.jsonl.gz",
        "done": base / "WDC_EXTRACT_DONE",
    }


def _save_wdc_extract_cache(paths, meta_payload, wdc_map):
    base = paths["base"]
    base.mkdir(parents=True, exist_ok=True)
    tmp_data = paths["data"].with_suffix(paths["data"].suffix + ".tmp")
    tmp_meta = paths["meta"].with_suffix(paths["meta"].suffix + ".tmp")
    try:
        with gzip.open(tmp_data, "wt", encoding="utf-8") as f:
            for norm, entries in (wdc_map or {}).items():
                norm_text = str(norm or "")
                for pair in list(entries or []):
                    if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                        continue
                    raw_value = str(pair[0] or "")
                    iri = str(pair[1] or "")
                    f.write(json.dumps([norm_text, raw_value, iri], ensure_ascii=False) + "\n")
        tmp_meta.write_text(
            json.dumps(meta_payload, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        tmp_data.replace(paths["data"])
        tmp_meta.replace(paths["meta"])
        paths["done"].write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        return True
    except Exception:
        try:
            if tmp_data.exists():
                tmp_data.unlink()
        except Exception:
            pass
        try:
            if tmp_meta.exists():
                tmp_meta.unlink()
        except Exception:
            pass
        return False


def _load_wdc_extract_cache(paths):
    if not (paths["done"].exists() and paths["meta"].exists() and paths["data"].exists()):
        return None
    try:
        payload = json.loads(paths["meta"].read_text(encoding="utf-8") or "{}")
    except Exception:
        return None
    out = defaultdict(list)
    try:
        with gzip.open(paths["data"], "rt", encoding="utf-8") as f:
            for raw_line in f:
                line = str(raw_line or "").strip()
                if not line:
                    continue
                row = json.loads(line)
                if not isinstance(row, list) or len(row) < 3:
                    continue
                norm, raw_value, iri = row[0], row[1], row[2]
                out[str(norm or "")].append((str(raw_value or ""), str(iri or "")))
    except Exception:
        return None
    if not out:
        # Empty extract maps are valid, but not useful for alignment cache reuse.
        # Keep strict behavior and force recomputation for this edge case.
        return None
    matched_count = int(payload.get("matched_count", 0) or 0)
    return dict(out), matched_count, payload


def _extract_wdc_values_with_cache(
    work_dir,
    class_name,
    parts_spec,
    decompressed_files,
    pattern,
    search_in,
    wdc_value_is_wd_iri,
    type_filter_iris,
    ignore_chars,
    force_refresh,
    workers,
    lock_path,
    progress_every=100,
):
    sources_manifest = _wdc_extract_sources_manifest(decompressed_files)
    cache_hash = _wdc_extract_cache_hash(
        class_name=class_name,
        parts_spec=parts_spec,
        pattern=pattern,
        search_in=search_in,
        wdc_value_is_wd_iri=wdc_value_is_wd_iri,
        type_filter_iris=type_filter_iris,
        ignore_chars=ignore_chars,
        sources_manifest=sources_manifest,
    )
    paths = _wdc_extract_cache_paths(work_dir, cache_hash)
    if not force_refresh:
        cached = _load_wdc_extract_cache(paths)
        if cached is not None:
            wdc_map, matched_count, _meta = cached
            print(
                "[WDC_CACHE] reuse "
                f"{cache_hash} | values={len(wdc_map):,} | matched={int(matched_count):,}"
            )
            return wdc_map, matched_count, True

    try:
        wdc_map, matched_count = align.extract_unique_iris_from_files(
            decompressed_files,
            pattern,
            collect_top_props=False,
            parallel=True,
            workers=workers,
            lock_path=lock_path,
            progress_every=progress_every,
            wdc_value_is_wd_iri=wdc_value_is_wd_iri,
            type_filter_iris=type_filter_iris,
            search_in=search_in,
        )
    except Exception as e:
        if not _is_too_many_open_files(e):
            raise
        print(
            "[WARN] Too many open files detected during align extraction; "
            "retrying in low-FD mode (parallel disabled)."
        )
        wdc_map, matched_count = align.extract_unique_iris_from_files(
            decompressed_files,
            pattern,
            collect_top_props=False,
            parallel=False,
            workers=1,
            lock_path=lock_path,
            progress_every=progress_every,
            wdc_value_is_wd_iri=wdc_value_is_wd_iri,
            type_filter_iris=type_filter_iris,
            search_in=search_in,
        )

    entries_count = int(sum(len(list(v or [])) for v in (wdc_map or {}).values()))
    meta_payload = {
        "cache_hash": cache_hash,
        "class_name": str(class_name or "").strip(),
        "parts_spec": str(parts_spec or "all"),
        "pattern": str(pattern or ""),
        "search_in": _normalize_wdc_pattern_search_in(search_in),
        "wdc_value_is_wd_iri": bool(wdc_value_is_wd_iri),
        "ignore_chars": str(ignore_chars or "").strip(),
        "type_filter_iris": sorted(
            {
                str(v or "").strip()
                for v in list(type_filter_iris or [])
                if str(v or "").strip()
            }
        ),
        "sources_manifest": sources_manifest,
        "matched_count": int(matched_count or 0),
        "normalized_values_count": int(len(wdc_map or {})),
        "entries_count": entries_count,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if _save_wdc_extract_cache(paths, meta_payload, wdc_map or {}):
        print(
            "[WDC_CACHE] save "
            f"{cache_hash} | values={len(wdc_map or {}):,} | matched={int(matched_count or 0):,}"
        )
    return wdc_map, matched_count, False


def _align_cache_config_matches(cache_dir: Path, params: dict) -> bool:
    if not cache_dir:
        return False
    config_path = Path(cache_dir) / "ALIGN_CONFIG.json"
    if not config_path.exists():
        return False
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return False

    expected_hash = _full_config_hash(params)
    cached_hash = str(payload.get("full_config_hash") or "").strip()
    if cached_hash:
        return cached_hash == expected_hash

    cached_full = payload.get("full_config")
    if isinstance(cached_full, dict):
        return _full_config_for_cache(cached_full) == _full_config_for_cache(params)
    return False


def is_align_cache_reusable(params) -> bool:
    cache_dir, _align_params = _align_cache_dir_for_params(params)
    if not cache_dir:
        return False
    links_tsv = cache_dir / "wdc_wikidata_links.tsv"
    align_done = cache_dir / "ALIGN_DONE"
    if not (links_tsv.exists() and align_done.exists()):
        return False
    return _align_cache_config_matches(cache_dir, params)


def _is_too_many_open_files(exc: Exception) -> bool:
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.EMFILE:
        return True
    return "Too many open files" in str(exc)


def _count_alignment_pairs(links_tsv: Path) -> int:
    if not links_tsv.exists():
        return 0
    total = 0
    with links_tsv.open("r", encoding="utf-8", errors="ignore") as f:
        for _ in f:
            total += 1
    return max(0, total - 1)  # minus header


def _canonical_wdc_link_entity(value) -> str:
    return str(value or "").strip().strip("<>")


def _canonical_wd_link_entity(value) -> str:
    raw = str(value or "").strip()
    try:
        return build.canonical_wd_link_entity_uri(build.normalize_wd_uri(raw, True))
    except Exception:
        return raw


def _canonical_wdc_token(value) -> str:
    return str(value or "").strip().strip("<>")


def _fast_subject_key_from_nq_line(line):
    """
    Fast subject extraction without full N-Quad regex parsing.

    Returns canonical subject key:
    - IRI subject: without angle brackets
    - blank node subject: unchanged (e.g., _:b0)
    """
    if not line:
        return None
    first = line[0]
    if first.isspace():
        stripped = line.lstrip()
        if not stripped:
            return None
        line = stripped
        first = line[0]
    if first == "<":
        end = line.find(">")
        if end <= 1:
            return None
        if end + 1 >= len(line) or not line[end + 1].isspace():
            return None
        return line[1:end]
    if first == "_" and line.startswith("_:"):
        sep = line.find(" ")
        if sep == -1:
            sep = line.find("\t")
        if sep <= 2:
            return None
        return line[:sep]
    return None


def _collect_wdc_outgoing_subgraphs(wdc_nq_paths, root_subjects, should_cancel=None):
    """
    Collect outgoing triples for selected WDC subjects and their recursively referenced bnodes.

    Subjects are matched on canonical token shape (IRIs without angle brackets, bnodes as-is).
    The graph context column is ignored by parse_nq_or_nt.
    """
    roots = {_canonical_wdc_token(s) for s in (root_subjects or []) if str(s or "").strip()}
    if not roots:
        return {}, {
            "scan_passes": 0,
            "parsed_lines": 0,
            "matched_subject_lines": 0,
            "subjects_requested": 0,
            "subjects_collected": 0,
        }

    outgoing = defaultdict(list)
    seen_subjects = set()
    pending = set(roots)
    scan_passes = 0
    parsed_lines = 0
    matched_subject_lines = 0
    total_bytes_all_files = 0
    for fp in wdc_nq_paths or []:
        try:
            total_bytes_all_files += int(os.path.getsize(fp))
        except Exception:
            continue

    def _eta_text(done_bytes, total_bytes, start_ts):
        if done_bytes <= 0 or total_bytes <= 0:
            return "ETA: N/A"
        elapsed = max(0.001, time.time() - start_ts)
        rate = done_bytes / elapsed
        if rate <= 0:
            return "ETA: N/A"
        remaining = max(0.0, float(total_bytes) - float(done_bytes))
        secs = int(remaining / rate)
        m, s = divmod(secs, 60)
        h, m = divmod(m, 60)
        if h:
            return f"ETA: {h}h{m:02d}m{s:02d}s"
        if m:
            return f"ETA: {m}m{s:02d}s"
        return f"ETA: {s}s"

    while True:
        to_scan = pending - seen_subjects
        if not to_scan:
            break
        scan_passes += 1
        pass_start = time.time()
        pass_done_bytes = 0
        last_progress_log = 0.0
        print(
            "[WDC_DEDUP] "
            f"pass {scan_passes}: scanning for {len(to_scan):,} subject(s) "
            f"(roots={len(roots):,}, pending_total={len(pending):,})"
        )
        newly_discovered = set()
        for fp in wdc_nq_paths or []:
            if should_cancel and should_cancel():
                raise PipelineError("Cancelled by user")
            file_size = 0
            file_done_bytes = 0
            try:
                file_size = int(os.path.getsize(fp))
            except Exception:
                file_size = 0
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    progress_line_stride = 20000
                    for line_idx, line in enumerate(f, start=1):
                        if should_cancel and line_idx % 200000 == 0 and should_cancel():
                            raise PipelineError("Cancelled by user")
                        line_len = len(line)
                        pass_done_bytes += line_len
                        file_done_bytes += line_len

                        subject_key = _fast_subject_key_from_nq_line(line)
                        if not subject_key or subject_key not in to_scan:
                            if line_idx % progress_line_stride == 0:
                                now = time.time()
                                if (now - last_progress_log) >= 10.0:
                                    pct = (file_done_bytes / file_size * 100.0) if file_size > 0 else 0.0
                                    print(
                                        "[WDC_DEDUP] "
                                        f"pass {scan_passes} file={Path(fp).name} "
                                        f"{pct:5.1f}% | {_eta_text(pass_done_bytes, file_size, pass_start)} | "
                                        f"matched={matched_subject_lines:,}"
                                    )
                                    last_progress_log = now
                            continue

                        parsed = build.parse_nq_or_nt(line)
                        if not parsed:
                            if line_idx % progress_line_stride == 0:
                                now = time.time()
                                if (now - last_progress_log) >= 10.0:
                                    pct = (file_done_bytes / file_size * 100.0) if file_size > 0 else 0.0
                                    print(
                                        "[WDC_DEDUP] "
                                        f"pass {scan_passes} file={Path(fp).name} "
                                        f"{pct:5.1f}% | {_eta_text(pass_done_bytes, file_size, pass_start)} | "
                                        f"matched={matched_subject_lines:,}"
                                    )
                                    last_progress_log = now
                            continue

                        parsed_lines += 1
                        _s, p, o = parsed
                        matched_subject_lines += 1
                        outgoing[subject_key].append((p, o))
                        if isinstance(o, str) and o.startswith("_:") and o not in seen_subjects and o not in to_scan:
                            newly_discovered.add(o)

                        if line_idx % progress_line_stride == 0:
                            now = time.time()
                            if (now - last_progress_log) >= 10.0:
                                pct = (file_done_bytes / file_size * 100.0) if file_size > 0 else 0.0
                                print(
                                    "[WDC_DEDUP] "
                                    f"pass {scan_passes} file={Path(fp).name} "
                                    f"{pct:5.1f}% | {_eta_text(pass_done_bytes, file_size, pass_start)} | "
                                    f"matched={matched_subject_lines:,}"
                                )
                                last_progress_log = now
            except PipelineError:
                raise
            except FileNotFoundError:
                continue
            if file_size > 0:
                print(
                    "[WDC_DEDUP] "
                    f"pass {scan_passes} file={Path(fp).name} done "
                    f"(matched={matched_subject_lines:,})"
                )
        seen_subjects.update(to_scan)
        pending.update(newly_discovered)
        pass_elapsed = time.time() - pass_start
        print(
            "[WDC_DEDUP] "
            f"pass {scan_passes} done in {int(pass_elapsed)}s | "
            f"new_bnodes={len(newly_discovered):,} | collected_subjects={len(outgoing):,}"
        )

    return outgoing, {
        "scan_passes": int(scan_passes),
        "parsed_lines": int(parsed_lines),
        "matched_subject_lines": int(matched_subject_lines),
        "subjects_requested": int(len(roots)),
        "subjects_collected": int(len([s for s in roots if s in outgoing])),
        "total_bytes_all_files": int(total_bytes_all_files),
    }


_NOISY_PREDICATE_HINTS = (
    "datecreated",
    "datemodified",
    "datepublished",
    "crawl",
    "timestamp",
    "version",
    "lastupdated",
    "mainentityofpage",
    "sameas",
    "url",
    "image",
    "thumbnail",
)

_STRONG_PREDICATE_HINTS = (
    "name",
    "label",
    "title",
    "identifier",
    "code",
    "iata",
    "icao",
    "isrc",
    "isbn",
    "issn",
    "postalcode",
    "latitude",
    "longitude",
    "coord",
    "telephone",
    "phone",
    "email",
)


def _canonical_link_value_for_dedup(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        norm = align.normalize_value_for_matching(raw, phone_mode=False)
    except Exception:
        norm = raw
    norm = str(norm or "").strip().lower()
    if not norm:
        return ""
    norm = unicodedata.normalize("NFKD", norm)
    norm = "".join(ch for ch in norm if not unicodedata.combining(ch))
    norm = "".join(ch for ch in norm if not unicodedata.category(ch).startswith("C"))
    norm = re.sub(r"[\s\-\.,;:|/\\_(){}\[\]\"'`]+", " ", norm)
    return " ".join(norm.split())


def _literal_lex_token(value):
    if not isinstance(value, str) or not value.startswith('"'):
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


def _normalize_attr_literal_value(text):
    value = str(text or "").strip().lower()
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = "".join(ch for ch in value if not unicodedata.category(ch).startswith("C"))
    value = re.sub(r"[\s\-\.,;:|/\\_(){}\[\]\"'`]+", " ", value)
    return " ".join(value.split())


def _is_noisy_predicate(predicate_key):
    pred = str(predicate_key or "").lower()
    return any(hint in pred for hint in _NOISY_PREDICATE_HINTS)


def _is_strong_predicate(predicate_key):
    pred = str(predicate_key or "").lower()
    return any(hint in pred for hint in _STRONG_PREDICATE_HINTS)


def _collect_attr_signature_for_subject(root_subject, outgoing):
    root = _canonical_wdc_token(root_subject)
    if root not in outgoing:
        return None
    visited_bnodes = set()
    stack = [root]
    attr_pairs = set()
    strong_pairs = set()
    while stack:
        node = stack.pop()
        if node in visited_bnodes:
            continue
        visited_bnodes.add(node)
        for p, o in list(outgoing.get(node, [])):
            pred = _canonical_wdc_token(p).lower()
            if _is_noisy_predicate(pred):
                continue
            if isinstance(o, str) and o.startswith("_:"):
                stack.append(o)
                continue
            if not (isinstance(o, str) and o.startswith('"')):
                continue
            lex = _literal_lex_token(o)
            if lex is None:
                continue
            value_norm = _normalize_attr_literal_value(lex)
            if not value_norm:
                continue
            pair = (pred, value_norm)
            attr_pairs.add(pair)
            if _is_strong_predicate(pred):
                strong_pairs.add(pair)
    signature_hash = hashlib.sha256(
        json.dumps(sorted(attr_pairs), ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "attr_pairs": attr_pairs,
        "strong_pairs": strong_pairs,
        "signature_hash": signature_hash,
    }


def _jaccard_similarity(left, right):
    set_left = set(left or set())
    set_right = set(right or set())
    if not set_left and not set_right:
        return 1.0
    union = set_left | set_right
    if not union:
        return 1.0
    return len(set_left & set_right) / float(len(union))


def _compute_key_stats_after_filter(link_keys):
    counts = {}
    for key in list(link_keys or []):
        k = str(key or "").strip()
        if not k:
            continue
        counts[k] = counts.get(k, 0) + 1
    repeated = {k: c for k, c in counts.items() if c > 1}
    histogram = {}
    for freq in repeated.values():
        histogram[str(freq)] = histogram.get(str(freq), 0) + 1
    top_repeated = sorted(repeated.items(), key=lambda x: (-x[1], x[0]))[:30]
    return {
        "unique_key_count": int(sum(1 for c in counts.values() if c == 1)),
        "repeated_key_count": int(len(repeated)),
        "repeated_total_occurrences": int(sum(repeated.values())),
        "repetition_histogram": histogram,
        "top_repeated_keys": [{"key": key, "count": int(count)} for key, count in top_repeated],
    }


def _apply_strict_duplicate_key_filter(
    wdc_nq_paths,
    wdc_entities,
    wd_entities_raw,
    wdc_values=None,
    wd_values=None,
    should_cancel=None,
):
    total = min(len(wdc_entities), len(wd_entities_raw))
    wdc_entities = list(wdc_entities[:total])
    wd_entities_raw = list(wd_entities_raw[:total])
    wdc_values = list(wdc_values or [])
    wd_values = list(wd_values or [])
    similarity_threshold = float(os.environ.get("STRICT_DUPLICATE_KEY_SIMILARITY", "0.82"))

    if total <= 0:
        empty_report = {
            "summary": {
                "enabled": True,
                "links_before": 0,
                "links_after": 0,
                "filtered_out_links": 0,
                "repeated_key_groups": 0,
                "kept_groups_count": 0,
                "removed_groups_count": 0,
                "similarity_threshold": similarity_threshold,
                "reason": "no_links",
            },
            "kept_groups": [],
            "removed_groups": [],
            "entity_decisions": [],
            "examples": [],
            "scan": {"scan_passes": 0, "parsed_lines": 0, "matched_subject_lines": 0},
            "key_stats_after_filter": _compute_key_stats_after_filter([]),
        }
        return wdc_entities, wd_entities_raw, wdc_values, wd_values, empty_report, []

    wdc_keys = [_canonical_wdc_link_entity(v) for v in wdc_entities]
    wd_keys = [_canonical_wd_link_entity(v) for v in wd_entities_raw]
    link_keys = []
    for i in range(total):
        raw_value = wdc_values[i] if i < len(wdc_values) else ""
        key = _canonical_link_value_for_dedup(raw_value)
        if not key:
            key = f"__missing__::{i}"
        link_keys.append(key)

    group_to_indices = defaultdict(list)
    for idx, key in enumerate(link_keys):
        group_to_indices[key].append(idx)

    repeated_groups = []
    for key, indices in group_to_indices.items():
        unique_wdc = sorted({_canonical_wdc_link_entity(wdc_entities[i]) for i in indices if wdc_entities[i]})
        if len(unique_wdc) > 1:
            repeated_groups.append((key, sorted(indices), unique_wdc))
    repeated_groups.sort(key=lambda item: item[0])

    if not repeated_groups:
        all_decisions = []
        tsv_rows = []
        for i in range(total):
            row = {
                "index": i,
                "key": link_keys[i],
                "wdc_entity": wdc_entities[i],
                "wikidata_entity": wd_entities_raw[i],
                "decision": "keep",
                "reason": "unique_key_or_single_entity",
                "signature_hash": "",
            }
            all_decisions.append(row)
            tsv_rows.append(row)
        report = {
            "summary": {
                "enabled": True,
                "links_before": int(total),
                "links_after": int(total),
                "filtered_out_links": 0,
                "repeated_key_groups": 0,
                "kept_groups_count": 0,
                "removed_groups_count": 0,
                "similarity_threshold": similarity_threshold,
                "reason": "no_repeated_keys",
            },
            "kept_groups": [],
            "removed_groups": [],
            "entity_decisions": all_decisions,
            "examples": [],
            "scan": {"scan_passes": 0, "parsed_lines": 0, "matched_subject_lines": 0},
            "key_stats_after_filter": _compute_key_stats_after_filter(link_keys),
        }
        return wdc_entities, wd_entities_raw, wdc_values, wd_values, report, tsv_rows

    subjects_to_profile = {
        _canonical_wdc_link_entity(wdc_entities[i])
        for _, indices, _unique_wdc in repeated_groups
        for i in indices
        if _canonical_wdc_link_entity(wdc_entities[i])
    }
    print(
        "[WDC_DUPKEY] "
        f"profiling {len(subjects_to_profile):,} WDC subjects across {len(repeated_groups):,} repeated-key groups"
    )
    outgoing, scan_report = _collect_wdc_outgoing_subgraphs(
        wdc_nq_paths,
        subjects_to_profile,
        should_cancel=should_cancel,
    )

    signatures = {}
    for subject in sorted(subjects_to_profile):
        signatures[subject] = _collect_attr_signature_for_subject(subject, outgoing)

    keep_idx = set(range(total))
    removed_groups = []
    kept_groups = []
    all_decisions = []
    tsv_rows = []
    examples = []
    decisions_by_index = {}

    for key, indices, unique_wdc_entities in repeated_groups:
        if should_cancel and should_cancel():
            raise PipelineError("Cancelled by user")
        unique_entities = [str(v) for v in unique_wdc_entities]
        richness_rows = []
        for entity in unique_entities:
            sig = signatures.get(entity) or {}
            attr_pairs = set(sig.get("attr_pairs") or set())
            strong_pairs = set(sig.get("strong_pairs") or set())
            richness_rows.append(
                {
                    "entity": entity,
                    "attr_count": int(len(attr_pairs)),
                    "strong_count": int(len(strong_pairs)),
                    "signature_hash": str(sig.get("signature_hash") or ""),
                }
            )
        richness_rows.sort(
            key=lambda row: (
                -row["attr_count"],
                -row["strong_count"],
                row["entity"],
            )
        )
        selected_entity = richness_rows[0]["entity"] if richness_rows else unique_entities[0]
        selected_idx = [idx for idx in indices if _canonical_wdc_link_entity(wdc_entities[idx]) == selected_entity]
        removed_idx = [idx for idx in indices if idx not in selected_idx]
        for idx in removed_idx:
            keep_idx.discard(idx)
        group_payload = {
            "key": key,
            "occurrences": int(len(indices)),
            "unique_wdc_entities": unique_entities,
            "selection_mode": "richest_entity",
            "selected_wdc_entity": selected_entity,
            "selected_occurrences": int(len(selected_idx)),
            "removed_occurrences": int(len(removed_idx)),
            "richness": richness_rows,
            "decision": "keep_selected_only",
            "reason": "one_to_one_keep_richest_wdc_entity",
        }
        if removed_idx:
            removed_groups.append(group_payload)
            if len(examples) < 25:
                examples.append(group_payload)
        else:
            kept_groups.append(group_payload)

        for idx in indices:
            subject = _canonical_wdc_link_entity(wdc_entities[idx])
            sig = signatures.get(subject) or {}
            is_selected = idx in selected_idx
            decision = "keep" if is_selected else "remove"
            decision_reason = (
                "selected_richest_wdc_entity"
                if is_selected
                else f"removed_non_selected_wdc_entity:{selected_entity}"
            )
            decision_row = {
                "index": int(idx),
                "key": key,
                "wdc_entity": wdc_entities[idx],
                "wikidata_entity": wd_entities_raw[idx],
                "decision": decision,
                "reason": decision_reason,
                "signature_hash": str(sig.get("signature_hash") or ""),
            }
            decisions_by_index[idx] = decision_row
            tsv_rows.append(decision_row)

    for i in range(total):
        if i in decisions_by_index:
            all_decisions.append(decisions_by_index[i])
            continue
        row = {
            "index": int(i),
            "key": link_keys[i],
            "wdc_entity": wdc_entities[i],
            "wikidata_entity": wd_entities_raw[i],
            "decision": "keep",
            "reason": "unique_key_or_single_entity",
            "signature_hash": "",
        }
        all_decisions.append(row)
        tsv_rows.append(row)

    keep_order = sorted(keep_idx)
    filtered_wdc = [wdc_entities[i] for i in keep_order]
    filtered_wd = [wd_entities_raw[i] for i in keep_order]
    filtered_wdc_values = [wdc_values[i] for i in keep_order if i < len(wdc_values)]
    filtered_wd_values = [wd_values[i] for i in keep_order if i < len(wd_values)]
    kept_keys = [link_keys[i] for i in keep_order]

    report = {
        "summary": {
            "enabled": True,
            "links_before": int(total),
            "links_after": int(len(filtered_wdc)),
            "filtered_out_links": int(total - len(filtered_wdc)),
            "repeated_key_groups": int(len(repeated_groups)),
            "kept_groups_count": int(len(kept_groups)),
            "removed_groups_count": int(len(removed_groups)),
            "similarity_threshold": similarity_threshold,
            "reason": "ok",
        },
        "kept_groups": kept_groups,
        "removed_groups": removed_groups,
        "entity_decisions": all_decisions,
        "examples": examples,
        "scan": scan_report,
        "key_stats_after_filter": _compute_key_stats_after_filter(kept_keys),
    }
    print(
        "[WDC_DUPKEY] "
        f"done: kept_groups={len(kept_groups):,}, removed_groups={len(removed_groups):,}, "
        f"links_kept={len(filtered_wdc):,}/{total:,}"
    )
    return filtered_wdc, filtered_wd, filtered_wdc_values, filtered_wd_values, report, tsv_rows


def generate_benchmark(
    params,
    workers=None,
    should_cancel=None,
    set_phase=None,
    should_skip_build=None,
    on_checkpoint=None,
    on_final_links_count=None,
):
    start_ts = time.time()

    class_name = params.get("class_name")
    parts_spec = params.get("parts_spec") or "all"
    matching_mode = _normalize_matching_mode(
        params.get("matching_mode"),
        fallback_wdc_value_is_wikidata=bool(params.get("wdc_value_is_wikidata")),
    )
    pattern = params.get("wdc_predicate_pattern")
    wdc_pattern_search_in = _normalize_wdc_pattern_search_in(params.get("wdc_pattern_search_in"))
    target_property = params.get("target_property")
    if target_property in {None, ""}:
        target_property = params.get("wikidata_property")
    target_class = params.get("target_class")
    if target_class in {None, ""}:
        target_class = params.get("wkd_class")
    target_endpoint = params.get("target_endpoint") or "wikidata"
    target_endpoint_url = params.get("target_endpoint_url") or None
    target_prefixes = params.get("target_prefixes") or None
    property_mapping_rules = params.get("property_mapping_rules") or ""
    wkd_prop_class = params.get("wkd_prop_class") or None
    ignore_chars = params.get("ignore_chars") or None
    includes_sameas = _mode_includes_sameas(matching_mode)
    includes_property = _mode_includes_property(matching_mode)
    wdc_value_is_wikidata = matching_mode == "sameas"
    # WDC traversal depth is fixed to "full traversal" for web builds.
    # Keep this internal and stop exposing/persisting it as a user parameter.
    max_depth = -1
    match_min_length = int(params.get("match_min_length", 1))
    force_align = bool(params.get("force_align"))
    use_local_only = bool(params.get("use_local_only"))
    strict_duplicate_key_filter = bool(params.get("strict_duplicate_key_filter", True))
    require_cached_align = bool(params.get("require_cached_align"))
    resume_build = bool(params.get("resume_build")) and require_cached_align
    resume_out_dir_raw = str(params.get("resume_out_dir") or "").strip()

    if not class_name:
        raise PipelineError("class_name is required")
    parsed_rules = _parse_property_mapping_rules(property_mapping_rules) if property_mapping_rules else []
    has_rules = len(parsed_rules) > 0
    rules_include_sameas = any(str(r.get("mode") or "property").lower() == "sameas" for r in parsed_rules)
    rules_include_property = any(str(r.get("mode") or "property").lower() != "sameas" for r in parsed_rules)
    effective_includes_sameas = includes_sameas or rules_include_sameas
    effective_includes_property = includes_property if not has_rules else rules_include_property
    if effective_includes_property and not has_rules and not pattern:
        raise PipelineError("wdc_predicate_pattern is required")
    if effective_includes_property and not has_rules and not target_property:
        if (target_endpoint or "wikidata") == "wikidata":
            raise PipelineError("wikidata_property is required")
        raise PipelineError("target_property is required")
    if effective_includes_sameas and not target_class:
        if (target_endpoint or "wikidata") == "wikidata":
            raise PipelineError("wkd_class is required when wdc_value_is_wikidata is enabled")
        raise PipelineError("target_class is required when sameAs mode is enabled")

    _set_align_normalization(ignore_chars)

    def _check_cancel():
        if should_cancel and should_cancel():
            raise PipelineError("Cancelled by user")

    def _emit_final_links_count(count, **meta):
        if not on_final_links_count:
            return
        try:
            payload = {"final_links_count": int(count)}
            if meta:
                payload.update(meta)
            on_final_links_count(payload)
        except Exception:
            pass

    align.set_cancel_checker(should_cancel)

    work_dir = Path("Download") / class_name
    work_dir.mkdir(parents=True, exist_ok=True)

    lock_path = Path("Download") / ".workers.lock"

    # Always use part_* sources. Do not fallback to *_full_graph.nq files.
    decompressed_files = []
    available_parts = None
    if use_local_only:
        decompressed_files = _select_local_part_files(str(work_dir), parts_spec)
        if not decompressed_files:
            raise PipelineError(f"No local parts matched '{parts_spec}' in Download/; download is disabled.")
    else:
        if parts_spec.lower() == "all":
            available_parts = align.discover_parts(class_name)
            if not available_parts:
                raise PipelineError("No parts available for class")
        else:
            available_parts = align.discover_parts(class_name)
            if not available_parts:
                available_parts = None
        parts_to_download = align.parse_parts_spec(parts_spec, available_parts)
        if not parts_to_download:
            raise PipelineError(f"No valid parts for '{parts_spec}'")
        decompressed_files = align.download_and_decompress(
            class_name,
            parts_to_download,
            work_dir,
            parallel_decompress=True,
            workers=workers,
            lock_path=lock_path,
        )
        if len(decompressed_files) < len(parts_to_download):
            missing = len(parts_to_download) - len(decompressed_files)
            raise PipelineError(f"Missing {missing} part(s). Download/decompress incomplete.")

    if not decompressed_files:
        raise PipelineError("No decompressed files available")

    local_parts = _count_local_parts(str(work_dir))
    if local_parts <= 0:
        raise PipelineError("No local parts found after download.")

    align_params = _align_params_from_job_params(
        {
            "matching_mode": matching_mode,
            "class_name": class_name,
            "parts_spec": parts_spec,
            "wdc_predicate_pattern": pattern,
            "wdc_pattern_search_in": wdc_pattern_search_in,
            "target_property": target_property,
            "target_class": target_class,
            "target_endpoint": target_endpoint,
            "target_endpoint_url": target_endpoint_url,
            "target_prefixes": target_prefixes,
            "property_mapping_rules": property_mapping_rules,
            "ignore_chars": ignore_chars,
        }
    )
    cache_hash = _config_hash(align_params)
    cache_dir = work_dir / "align_cache" / cache_hash
    cache_dir.mkdir(parents=True, exist_ok=True)
    full_config = _full_config_for_cache(params)
    full_config_hash = _full_config_hash(params)
    (cache_dir / "ALIGN_CONFIG.json").write_text(
        json.dumps(
            {
                "cache_hash": cache_hash,
                "align_params": align_params,
                "full_config_hash": full_config_hash,
                "full_config": full_config,
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    reused_align = False
    links_tsv = cache_dir / "wdc_wikidata_links.tsv"
    align_done = cache_dir / "ALIGN_DONE"
    align_pairs = 0
    align_link_sources = []
    pair_source_map = {}
    type_filter_iris = align.default_type_filter_iris_for_class(class_name)

    cache_ready = links_tsv.exists() and align_done.exists()
    cache_config_ok = _align_cache_config_matches(cache_dir, params) if cache_ready else False

    if cache_ready and not force_align and cache_config_ok:
        reused_align = True
        align_pairs = _count_alignment_pairs(links_tsv)
    else:
        if require_cached_align:
            if cache_ready and not cache_config_ok:
                raise PipelineError("Cached align found but full config mismatch; build-only requested")
            raise PipelineError("Cached align not found; build-only requested")
        if cache_ready and (not cache_config_ok) and not force_align:
            print("[INFO] Align cache found but full config mismatch; recomputing align.")
        if set_phase:
            set_phase("align")
        _check_cancel()

        wdc_map = {}
        wikidata_map = {}
        matches = []
        wdc_values_matched = set()
        seen_pairs = set()
        component_errors = []

        def _merge_component(component_wdc_map, component_wikidata_map, component_matches, component_wdc_values):
            _merge_value_maps(wdc_map, component_wdc_map or {})
            _merge_value_maps(wikidata_map, component_wikidata_map or {})
            wdc_values_matched.update(component_wdc_values or set())
            for item in component_matches or []:
                pair = (item.get("wdc_iri"), item.get("wikidata_uri"))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                matches.append(item)

        if includes_sameas and not has_rules:
            try:
                sameas_wdc_map, matched_count, _wdc_cache_reused = _extract_wdc_values_with_cache(
                    work_dir=work_dir,
                    class_name=class_name,
                    parts_spec=parts_spec,
                    decompressed_files=decompressed_files,
                    pattern=pattern,
                    search_in=wdc_pattern_search_in,
                    wdc_value_is_wd_iri=True,
                    type_filter_iris=type_filter_iris,
                    ignore_chars=ignore_chars,
                    force_refresh=force_align,
                    workers=workers,
                    lock_path=lock_path,
                    progress_every=100,
                )
                if matched_count == 0:
                    raise PipelineError("No WDC values matched the predicate pattern")

                _check_cancel()
                wd_entity_iris = set()
                for entries in sameas_wdc_map.values():
                    for value, _iri in entries:
                        wd_iri = align.extract_wd_entity_iri(value)
                        if wd_iri:
                            wd_entity_iris.add(wd_iri)
                if not wd_entity_iris:
                    if (target_endpoint or "wikidata") == "wikidata":
                        raise PipelineError("No Wikidata URLs extracted from WDC values")
                    raise PipelineError("No target entity URLs extracted from WDC values")
                if (target_endpoint or "wikidata") == "wikidata":
                    sameas_wikidata_map = align.fetch_wikidata_values(
                        wikidata_property=None,
                        wkd_class=target_class,
                        wkd_prop_class=None,
                        entity_iris=sorted(wd_entity_iris),
                    )
                else:
                    sameas_value_candidates = set(wd_entity_iris)
                    for iri in list(wd_entity_iris):
                        if iri.startswith("http://www.wikidata.org/entity/"):
                            sameas_value_candidates.add("https://www.wikidata.org/entity/" + iri.rsplit("/", 1)[-1])
                        elif iri.startswith("https://www.wikidata.org/entity/"):
                            sameas_value_candidates.add("http://www.wikidata.org/entity/" + iri.rsplit("/", 1)[-1])
                    sameas_wikidata_map = align.fetch_target_values(
                        target_property="owl:sameAs",
                        target_class=target_class,
                        target_prop_class=None,
                        entity_iris=None,
                        value_candidates=sorted(sameas_value_candidates),
                        target_endpoint=target_endpoint,
                        target_endpoint_url=target_endpoint_url,
                        target_prefixes=target_prefixes,
                    )
                if not sameas_wikidata_map:
                    if (target_endpoint or "wikidata") == "wikidata":
                        raise PipelineError(
                            "No Wikidata entities matched class filter "
                            f"({target_class}) for extracted WDC Wikidata URLs "
                            f"({len(wd_entity_iris):,} entities)"
                        )
                    raise PipelineError(
                        "No target entities matched class filter "
                        f"({target_class}) for extracted WDC target URLs "
                        f"({len(wd_entity_iris):,} entities)"
                    )
                _check_cancel()
                sameas_matches, sameas_wdc_values_matched = align.fuzzy_link(
                    sameas_wdc_map,
                    sameas_wikidata_map,
                    parallel=True,
                    workers=workers,
                    lock_path=lock_path,
                    min_length=match_min_length,
                )
                _merge_component(sameas_wdc_map, sameas_wikidata_map, sameas_matches, sameas_wdc_values_matched)
            except PipelineError as exc:
                if matching_mode == "sameas":
                    raise
                print(f"[WARN] sameAs component skipped: {exc}")
                component_errors.append(f"sameAs: {exc}")

        if includes_property or has_rules:
            try:
                if has_rules:
                    print(f"[INFO] Property mapping rules enabled: {len(parsed_rules)} rule line(s).")
                    merged_wdc_map = {}
                    merged_wikidata_map = {}
                    prop_matches = []
                    prop_wdc_values_matched = set()
                    prop_seen_pairs = set()
                    matched_total = 0
                    target_fetch_any = False
                    target_fetch_error = False
                    for rule in parsed_rules:
                        rule_mode = str(rule.get("mode") or "property").strip().lower()
                        if rule_mode == "sameas":
                            for wdc_prop, _unused_target_prop in rule["pairs"]:
                                _set_align_normalization("")
                                rule_wdc_map, rule_matched_count, _rule_cache_reused = _extract_wdc_values_with_cache(
                                    work_dir=work_dir,
                                    class_name=class_name,
                                    parts_spec=parts_spec,
                                    decompressed_files=decompressed_files,
                                    pattern=wdc_prop,
                                    search_in="predicate",
                                    wdc_value_is_wd_iri=True,
                                    type_filter_iris=type_filter_iris,
                                    ignore_chars="",
                                    force_refresh=force_align,
                                    workers=workers,
                                    lock_path=lock_path,
                                    progress_every=100,
                                )
                                matched_total += int(rule_matched_count or 0)
                                if not rule_wdc_map:
                                    continue
                                wd_entity_iris = set()
                                for entries in rule_wdc_map.values():
                                    for value, _iri in entries:
                                        wd_iri = align.extract_wd_entity_iri(value)
                                        if wd_iri:
                                            wd_entity_iris.add(wd_iri)
                                if not wd_entity_iris:
                                    continue
                                if (target_endpoint or "wikidata") == "wikidata":
                                    rule_wikidata_map = align.fetch_wikidata_values(
                                        wikidata_property=None,
                                        wkd_class=target_class,
                                        wkd_prop_class=None,
                                        entity_iris=sorted(wd_entity_iris),
                                    )
                                else:
                                    sameas_value_candidates = set(wd_entity_iris)
                                    for iri in list(wd_entity_iris):
                                        if iri.startswith("http://www.wikidata.org/entity/"):
                                            sameas_value_candidates.add(
                                                "https://www.wikidata.org/entity/" + iri.rsplit("/", 1)[-1]
                                            )
                                        elif iri.startswith("https://www.wikidata.org/entity/"):
                                            sameas_value_candidates.add(
                                                "http://www.wikidata.org/entity/" + iri.rsplit("/", 1)[-1]
                                            )
                                    rule_wikidata_map = align.fetch_target_values(
                                        target_property="owl:sameAs",
                                        target_class=target_class,
                                        target_prop_class=None,
                                        entity_iris=None,
                                        value_candidates=sorted(sameas_value_candidates),
                                        target_endpoint=target_endpoint,
                                        target_endpoint_url=target_endpoint_url,
                                        target_prefixes=target_prefixes,
                                    )
                                if rule_wikidata_map is None:
                                    target_fetch_error = True
                                    continue
                                if rule_wikidata_map:
                                    target_fetch_any = True
                                if not rule_wikidata_map:
                                    continue
                                _merge_value_maps(merged_wdc_map, rule_wdc_map)
                                _merge_value_maps(merged_wikidata_map, rule_wikidata_map)
                                _check_cancel()
                                pair_matches, pair_wdc_values = align.fuzzy_link(
                                    rule_wdc_map,
                                    rule_wikidata_map,
                                    parallel=True,
                                    workers=workers,
                                    lock_path=lock_path,
                                    min_length=match_min_length,
                                )
                                prop_wdc_values_matched.update(pair_wdc_values or set())
                                for item in pair_matches or []:
                                    pair = (item.get("wdc_iri"), item.get("wikidata_uri"))
                                    if pair in prop_seen_pairs:
                                        continue
                                    prop_seen_pairs.add(pair)
                                    tagged = dict(item)
                                    prev_method = str(tagged.get("method") or "exact")
                                    tagged["method"] = f"{prev_method}|sameAs:{wdc_prop}"
                                    prop_matches.append(tagged)
                            continue
                        rule_ignore = str(rule.get("ignore_chars") or "").strip() or ignore_chars
                        pair_ignores = list(rule.get("pair_ignore_chars") or [])
                        pair_search_modes = list(rule.get("pair_search_in") or [])
                        for pair_idx, (wdc_prop, target_prop) in enumerate(rule["pairs"]):
                            pair_ignore = ""
                            if pair_idx < len(pair_ignores):
                                pair_ignore = str(pair_ignores[pair_idx] or "").strip()
                            pair_search_in = wdc_pattern_search_in
                            if pair_idx < len(pair_search_modes):
                                pair_search_in = _normalize_wdc_pattern_search_in(pair_search_modes[pair_idx])
                            _set_align_normalization(pair_ignore or rule_ignore)
                            rule_wdc_map, rule_matched_count, _rule_cache_reused = _extract_wdc_values_with_cache(
                                work_dir=work_dir,
                                class_name=class_name,
                                parts_spec=parts_spec,
                                decompressed_files=decompressed_files,
                                pattern=wdc_prop,
                                search_in=pair_search_in,
                                wdc_value_is_wd_iri=False,
                                type_filter_iris=type_filter_iris,
                                ignore_chars=(pair_ignore or rule_ignore),
                                force_refresh=force_align,
                                workers=workers,
                                lock_path=lock_path,
                                progress_every=100,
                            )
                            matched_total += int(rule_matched_count or 0)
                            if not rule_wdc_map:
                                continue
                            for target_prop_alt in _split_target_property_alternatives(target_prop):
                                if (target_endpoint or "wikidata") == "wikidata":
                                    rule_wikidata_map = _fetch_wikidata_values_for_alignment(
                                        target_prop_alt,
                                        target_class,
                                        wkd_prop_class,
                                        wdc_map=rule_wdc_map,
                                    )
                                else:
                                    rule_wikidata_map = _fetch_target_values_for_alignment(
                                        target_prop_alt,
                                        target_class,
                                        wkd_prop_class,
                                        target_endpoint=target_endpoint,
                                        target_endpoint_url=target_endpoint_url,
                                        target_prefixes=target_prefixes,
                                        wdc_map=rule_wdc_map,
                                    )
                                if rule_wikidata_map is None:
                                    target_fetch_error = True
                                    continue
                                if rule_wikidata_map:
                                    target_fetch_any = True
                                if not rule_wikidata_map:
                                    continue
                                _merge_value_maps(merged_wdc_map, rule_wdc_map)
                                _merge_value_maps(merged_wikidata_map, rule_wikidata_map)
                                _check_cancel()
                                pair_matches, pair_wdc_values = align.fuzzy_link(
                                    rule_wdc_map,
                                    rule_wikidata_map,
                                    parallel=True,
                                    workers=workers,
                                    lock_path=lock_path,
                                    min_length=match_min_length,
                                )
                                prop_wdc_values_matched.update(pair_wdc_values or set())
                                for item in pair_matches or []:
                                    pair = (item.get("wdc_iri"), item.get("wikidata_uri"))
                                    if pair in prop_seen_pairs:
                                        continue
                                    prop_seen_pairs.add(pair)
                                    tagged = dict(item)
                                    prev_method = str(tagged.get("method") or "exact")
                                    tagged["method"] = f"{prev_method}|{wdc_prop}->{target_prop_alt}"
                                    prop_matches.append(tagged)

                    _set_align_normalization(ignore_chars)
                    if matched_total <= 0:
                        raise PipelineError("No WDC values matched the property mapping rules")
                    if target_fetch_error and not target_fetch_any:
                        raise PipelineError("Failed to fetch target endpoint values for property mapping rules")
                    _merge_component(merged_wdc_map, merged_wikidata_map, prop_matches, prop_wdc_values_matched)
                else:
                    prop_wdc_map, matched_count, _wdc_cache_reused = _extract_wdc_values_with_cache(
                        work_dir=work_dir,
                        class_name=class_name,
                        parts_spec=parts_spec,
                        decompressed_files=decompressed_files,
                        pattern=pattern,
                        search_in=wdc_pattern_search_in,
                        wdc_value_is_wd_iri=False,
                        type_filter_iris=type_filter_iris,
                        ignore_chars=ignore_chars,
                        force_refresh=force_align,
                        workers=workers,
                        lock_path=lock_path,
                        progress_every=100,
                    )
                    if matched_count == 0:
                        raise PipelineError("No WDC values matched the predicate pattern")
                    _check_cancel()
                    target_prop_alts = _split_target_property_alternatives(target_property)
                    if not target_prop_alts:
                        raise PipelineError("target_property is required")
                    prop_wikidata_map = {}
                    prop_matches = []
                    prop_wdc_values_matched = set()
                    prop_seen_pairs = set()
                    fetched_any = False
                    fetch_error = False
                    for target_prop_alt in target_prop_alts:
                        if (target_endpoint or "wikidata") == "wikidata":
                            alt_map = _fetch_wikidata_values_for_alignment(
                                target_prop_alt,
                                target_class,
                                wkd_prop_class,
                                wdc_map=prop_wdc_map,
                            )
                        else:
                            alt_map = _fetch_target_values_for_alignment(
                                target_prop_alt,
                                target_class,
                                wkd_prop_class,
                                target_endpoint=target_endpoint,
                                target_endpoint_url=target_endpoint_url,
                                target_prefixes=target_prefixes,
                                wdc_map=prop_wdc_map,
                            )
                        if alt_map is None:
                            fetch_error = True
                            continue
                        if not alt_map:
                            continue
                        fetched_any = True
                        _merge_value_maps(prop_wikidata_map, alt_map)
                        _check_cancel()
                        pair_matches, pair_wdc_values = align.fuzzy_link(
                            prop_wdc_map,
                            alt_map,
                            parallel=True,
                            workers=workers,
                            lock_path=lock_path,
                            min_length=match_min_length,
                        )
                        prop_wdc_values_matched.update(pair_wdc_values or set())
                        for item in pair_matches or []:
                            pair = (item.get("wdc_iri"), item.get("wikidata_uri"))
                            if pair in prop_seen_pairs:
                                continue
                            prop_seen_pairs.add(pair)
                            tagged = dict(item)
                            prev_method = str(tagged.get("method") or "exact")
                            tagged["method"] = f"{prev_method}|{target_prop_alt}"
                            prop_matches.append(tagged)
                    if fetch_error and not fetched_any:
                        raise PipelineError("Failed to fetch target endpoint values")
                    _merge_component(prop_wdc_map, prop_wikidata_map, prop_matches, prop_wdc_values_matched)
            except PipelineError as exc:
                if matching_mode == "property":
                    raise
                print(f"[WARN] property component skipped: {exc}")
                component_errors.append(f"property: {exc}")

        if not matches:
            if component_errors:
                raise PipelineError("No links produced in combined mode. " + " | ".join(component_errors))
            raise PipelineError("No links produced")
        pair_source_map = _build_pair_source_map(matches, fallback_pattern=pattern)
        align_link_sources = _count_sources_for_pairs(
            [m.get("wdc_iri") for m in matches],
            [m.get("wikidata_uri") for m in matches],
            pair_source_map,
        )
        align_pairs = len(matches)

        _check_cancel()
        align.export_results(
            matches,
            wdc_values_matched,
            wdc_map,
            wikidata_map,
            cache_dir,
            key_name=pattern,
            class_name=class_name,
            parts_spec=parts_spec,
            pattern=pattern,
            wikidata_property=target_property,
            wkd_class=target_class,
            wkd_prop_class=wkd_prop_class,
            start_ts=start_ts,
        )

        if not links_tsv.exists():
            raise PipelineError("Links TSV not found after alignment")
        align_done.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")

    if reused_align:
        print("✅ Alignment stage completed (cached).")
    else:
        print("✅ Alignment stage completed.")

    if strict_duplicate_key_filter:
        print("ℹ️ Final entity links (exact) pending strict duplicate-key filtering.")
    else:
        _emit_final_links_count(
            align_pairs,
            source="align",
            exact=True,
            raw_links=align_pairs,
            links_after_strict_duplicate_key_filter=align_pairs,
        )
        print(f"✅ Final entity links (exact): {align_pairs:,}.")

    if align_pairs == 0:
        reason = "No alignments found (0); build skipped."
        print(f"[INFO] {reason}")
        _emit_final_links_count(0, source="align", exact=True, raw_links=0, links_after=0)
        return {
            "class_name": class_name,
            "links_tsv": str(links_tsv),
            "align_dir": str(cache_dir),
            "reused_align": reused_align,
            "out_dir": None,
            "build_cancelled": False,
            "build_skipped": True,
            "build_skip_reason": reason,
            "started_at": start_ts,
            "ended_at": time.time(),
        }

    data_dir = Path("data") / class_name
    data_dir.mkdir(parents=True, exist_ok=True)
    if resume_build and resume_out_dir_raw:
        out_dir = Path(resume_out_dir_raw)
    else:
        out_dir = data_dir / f"beam_{_timestamp_tag()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    wdc_nq = [str(Path(p)) for p in decompressed_files]
    if not wdc_nq:
        raise PipelineError(f"No WDC files found in {work_dir}")

    parts_manifest = []
    total_parts_size = 0
    for p in wdc_nq:
        fp = Path(p)
        try:
            size_b = fp.stat().st_size
        except Exception:
            size_b = 0
        total_parts_size += size_b
        parts_manifest.append(
            {
                "name": fp.name,
                "size_bytes": size_b,
                "size_human": _fmt_size(size_b),
            }
        )
    build_config = {
        "matching_mode": matching_mode,
        "class_name": class_name,
        "parts_spec": parts_spec,
        "wdc_predicate_pattern": pattern,
        "wdc_pattern_search_in": wdc_pattern_search_in,
        "property_mapping_rules": property_mapping_rules,
        "target_property": target_property,
        "target_class": target_class,
        "target_endpoint": target_endpoint,
        "target_endpoint_url": target_endpoint_url,
        "target_prefixes": target_prefixes,
        # Backward-compatible aliases for existing tools/views.
        "wikidata_property": target_property,
        "wkd_class": target_class,
        "ignore_chars": ignore_chars,
        "force_align": force_align,
        "use_local_only": use_local_only,
        "strict_duplicate_key_filter": strict_duplicate_key_filter,
        "linked_only_entities": True,
        "build_name": out_dir.name,
        "result_path": str(out_dir),
        "parts_count": len(parts_manifest),
        "parts_total_size_bytes": total_parts_size,
        "parts_total_size_human": _fmt_size(total_parts_size),
        "parts_manifest": parts_manifest,
    }
    (out_dir / "BUILD_CONFIG.json").write_text(
        json.dumps(build_config, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    if params.get("skip_build"):
        return {
            "class_name": class_name,
            "links_tsv": str(links_tsv),
            "align_dir": str(cache_dir),
            "reused_align": reused_align,
            "out_dir": None,
            "build_cancelled": False,
            "started_at": start_ts,
            "ended_at": time.time(),
        }

    if should_skip_build and should_skip_build():
        return {
            "class_name": class_name,
            "links_tsv": str(links_tsv),
            "align_dir": str(cache_dir),
            "reused_align": reused_align,
            "out_dir": None,
            "build_cancelled": True,
            "started_at": start_ts,
            "ended_at": time.time(),
        }

    if on_checkpoint:
        try:
            on_checkpoint(
                {
                    "kind": "build_started",
                    "phase": "build",
                    "out_dir": str(out_dir),
                    "align_dir": str(cache_dir),
                    "resume": bool(resume_build),
                    "ts": time.time(),
                }
            )
        except Exception:
            pass

    if set_phase:
        set_phase("build")
    _check_cancel()
    wdc_entities, wd_entities_raw, wdc_values, wd_values = build.read_links(
        str(links_tsv),
        "\t",
        0,
        1,
        None,
        None,
    )
    if not pair_source_map:
        pair_source_map = _pair_source_map_from_links_tsv(links_tsv, fallback_pattern=pattern)
    if not align_link_sources:
        align_link_sources = _count_sources_for_pairs(wdc_entities, wd_entities_raw, pair_source_map)
    raw_links_before_filters = len(wdc_entities)
    strict_filter_report = None
    if strict_duplicate_key_filter:
        (
            wdc_entities,
            wd_entities_raw,
            wdc_values,
            wd_values,
            strict_filter_report,
            strict_filter_decisions,
        ) = _apply_strict_duplicate_key_filter(
            wdc_nq,
            wdc_entities,
            wd_entities_raw,
            wdc_values,
            wd_values,
            should_cancel=should_cancel,
        )
        try:
            (out_dir / "WDC_DUPLICATE_KEY_FILTER_REPORT.json").write_text(
                json.dumps(strict_filter_report, indent=2, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            pass
        try:
            with (out_dir / "WDC_DUPLICATE_KEY_FILTER_DECISIONS.tsv").open("w", encoding="utf-8") as f:
                f.write("key\tdecision\treason\tsignature_hash\twdc_entity\twikidata_entity\n")
                for row in strict_filter_decisions:
                    f.write(
                        f"{row.get('key','')}\t{row.get('decision','')}\t{row.get('reason','')}\t"
                        f"{row.get('signature_hash','')}\t{row.get('wdc_entity','')}\t{row.get('wikidata_entity','')}\n"
                    )
        except Exception:
            pass
        if not wdc_entities or not wd_entities_raw:
            reason = "No links left after strict duplicate-key filtering; build skipped."
            print(f"[INFO] {reason}")
            _emit_final_links_count(
                0,
                source="build_prefilter",
                exact=True,
                raw_links=raw_links_before_filters,
                links_after_strict_duplicate_key_filter=0,
            )
            return {
                "class_name": class_name,
                "links_tsv": str(links_tsv),
                "align_dir": str(cache_dir),
                "reused_align": reused_align,
                "out_dir": None,
                "build_cancelled": False,
                "build_skipped": True,
                "build_skip_reason": reason,
                "started_at": start_ts,
                "ended_at": time.time(),
            }

    final_links_count = len(wdc_entities)
    links_by_source_after_filter = _count_sources_for_pairs(wdc_entities, wd_entities_raw, pair_source_map)
    _emit_final_links_count(
        final_links_count,
        source="build_prefilter",
        exact=True,
        raw_links=raw_links_before_filters,
        links_after_strict_duplicate_key_filter=final_links_count,
    )

    wdc_mask_values = set(v for v in wdc_values if v)
    wd_mask_values = set(v for v in wd_values if v)

    wdc_exclude_props = set()
    wd_exclude_props = set()
    wd_link_prop_uris = set()
    wdc_link_prop_patterns = set()
    if has_rules:
        for rule in parsed_rules:
            for wdc_prop, target_prop in rule["pairs"]:
                if wdc_prop:
                    wdc_link_prop_patterns.add(str(wdc_prop).lower())
                if (target_endpoint or "wikidata") == "wikidata" and target_prop:
                    norm_prop = build.normalize_wd_prop_id(str(target_prop))
                    if norm_prop:
                        wd_link_prop_uris.update(build.wikidata_prop_uris(norm_prop))
    elif pattern:
        wdc_link_prop_patterns.add(str(pattern).lower())
    if (not has_rules) and (target_endpoint or "wikidata") == "wikidata" and target_property:
        for target_prop_alt in _split_target_property_alternatives(target_property):
            norm_prop = build.normalize_wd_prop_id(str(target_prop_alt))
            if norm_prop:
                wd_link_prop_uris.update(build.wikidata_prop_uris(norm_prop))

    replace_map = {}
    lowercase_wd = True
    add_wd_labels = True
    endpoint_sparql_url = align.resolve_target_endpoint_url(target_endpoint, target_endpoint_url)
    if (target_endpoint or "wikidata") != "wikidata":
        add_wd_labels = False
    wd_batch_default = int(os.environ.get("BEAM_WD_BATCH_SIZE", "150"))
    wd_sleep_default = float(os.environ.get("BEAM_WD_SLEEP", "0.05"))

    args = SimpleNamespace(
        wdc_nq=wdc_nq,
        wd_nq=None,
        sep="\t",
        wdc_col=0,
        wd_col=1,
        wdc_value_col=None,
        wd_value_col=None,
        dedupe_links=False,
        keep_link_values=False,
        wdc_min_triples=0,
        wdc_exclude_prop=[],
        wd_exclude_prop=[],
        no_wd_labels=False,
        wd_prop_min_count=0,
        merge_wd_by_link_values=False,
        sparql_url=endpoint_sparql_url or "https://query.wikidata.org/sparql",
        lang="en",
        batch_size=int(params.get("wd_batch_size", wd_batch_default)),
        sleep=float(params.get("wd_sleep", wd_sleep_default)),
        timeout=int(params.get("wd_timeout", 60)),
        retries=int(params.get("wd_retries", 3)),
        backoff=float(params.get("wd_backoff", 2.0)),
        no_lowercase_wd=False,
        resume=bool(resume_build),
        state_file=None,
        max_depth=max_depth,
        progress_every=10_000_000,
        linked_only_entities=True,
    )

    out_without = str(out_dir / "without_link_code")
    out_with = str(out_dir / "with_link_code")
    shared_wd_raw_cache = str(out_dir / ".wd_raw_triples.tsv")

    build.run_pipeline(
        args,
        wdc_entities,
        wd_entities_raw,
        wdc_values,
        wd_values,
        out_without,
        wdc_mask_values,
        wd_mask_values,
        wdc_exclude_props,
        wdc_link_prop_patterns,
        wd_exclude_props | wd_link_prop_uris,
        replace_map,
        lowercase_wd,
        add_wd_labels,
        wd_raw_cache_path=shared_wd_raw_cache,
    )
    build.run_pipeline(
        args,
        wdc_entities,
        wd_entities_raw,
        wdc_values,
        wd_values,
        out_with,
        None,
        None,
        wdc_exclude_props,
        set(),
        wd_exclude_props,
        replace_map,
        lowercase_wd,
        add_wd_labels,
        wd_raw_cache_path=shared_wd_raw_cache,
    )

    with_ent_links = Path(out_with) / "ent_links"
    without_ent_links = Path(out_without) / "ent_links"
    with_links_count = _count_ent_links_rows(with_ent_links)
    without_links_count = _count_ent_links_rows(without_ent_links)
    links_after_strict_filter = int(final_links_count)
    strict_removed_groups_count = 0
    if isinstance(strict_filter_report, dict):
        summary = strict_filter_report.get("summary") or {}
        try:
            links_after_strict_filter = int(summary.get("links_after", final_links_count))
        except Exception:
            links_after_strict_filter = int(final_links_count)
        try:
            strict_removed_groups_count = int(summary.get("removed_groups_count", 0))
        except Exception:
            strict_removed_groups_count = 0
    build_stats = {
        "class_name": class_name,
        "build_name": out_dir.name,
        "target_endpoint": target_endpoint,
        "target_endpoint_url": target_endpoint_url,
        "strict_duplicate_key_filter": bool(strict_duplicate_key_filter),
        "links_before_filters": int(raw_links_before_filters),
        "links_after_strict_duplicate_key_filter": links_after_strict_filter,
        "strict_duplicate_key_removed_groups_count": strict_removed_groups_count,
        "links_by_source_align": align_link_sources,
        "links_by_source_after_filter": links_by_source_after_filter,
        "links_count_with_link_code": int(with_links_count),
        "links_count_without_link_code": int(without_links_count),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        (Path(out_dir) / "BUILD_STATS.json").write_text(
            json.dumps(build_stats, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except Exception:
        pass
    variant_stats_with = {
        "variant": "with_link_code",
        "links_count": int(with_links_count),
        "build_name": out_dir.name,
        "class_name": class_name,
        "target_endpoint": target_endpoint,
        "generated_at": build_stats["generated_at"],
    }
    variant_stats_without = {
        "variant": "without_link_code",
        "links_count": int(without_links_count),
        "build_name": out_dir.name,
        "class_name": class_name,
        "target_endpoint": target_endpoint,
        "generated_at": build_stats["generated_at"],
    }
    try:
        (Path(out_with) / "BUILD_STATS.json").write_text(
            json.dumps(variant_stats_with, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except Exception:
        pass
    try:
        (Path(out_without) / "BUILD_STATS.json").write_text(
            json.dumps(variant_stats_without, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except Exception:
        pass

    # mark build done
    (Path(out_dir) / "BUILD_DONE").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")

    return {
        "class_name": class_name,
        "links_tsv": str(links_tsv),
        "align_dir": str(cache_dir),
        "reused_align": reused_align,
        "out_dir": str(out_dir),
        "build_cancelled": False,
        "started_at": start_ts,
        "ended_at": time.time(),
    }
