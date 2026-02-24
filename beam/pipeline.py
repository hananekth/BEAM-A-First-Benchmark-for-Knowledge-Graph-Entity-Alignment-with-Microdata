import os
import json
import hashlib
import time
import errno
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


def _config_hash(align_params):
    payload = json.dumps(align_params, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


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


def _filter_links_one_to_one(wdc_entities, wd_entities_raw, wdc_values=None, wd_values=None):
    """
    Keep only links that are one-to-one on endpoints.

    Any link is removed if its WDC entity or Wikidata entity appears in more than one pair.
    Values are kept in sync when provided.
    """
    total = min(len(wdc_entities), len(wd_entities_raw))
    if total <= 0:
        return list(wdc_entities), list(wd_entities_raw), list(wdc_values or []), list(wd_values or []), {
            "enabled": True,
            "links_before": 0,
            "links_after": 0,
            "removed_links": 0,
            "ambiguous_wdc_entities": 0,
            "ambiguous_wikidata_entities": 0,
            "max_links_per_wdc_entity": 0,
            "max_links_per_wikidata_entity": 0,
            "examples_removed": [],
        }

    wdc_values = list(wdc_values or [])
    wd_values = list(wd_values or [])
    wdc_entities = list(wdc_entities[:total])
    wd_entities_raw = list(wd_entities_raw[:total])

    wdc_keys = [_canonical_wdc_link_entity(v) for v in wdc_entities]
    wd_keys = [_canonical_wd_link_entity(v) for v in wd_entities_raw]

    wdc_counts = {}
    wd_counts = {}
    for key in wdc_keys:
        wdc_counts[key] = wdc_counts.get(key, 0) + 1
    for key in wd_keys:
        wd_counts[key] = wd_counts.get(key, 0) + 1

    keep_idx = []
    removed_examples = []
    for i, (wdc_key, wd_key) in enumerate(zip(wdc_keys, wd_keys)):
        keep = (wdc_counts.get(wdc_key, 0) == 1) and (wd_counts.get(wd_key, 0) == 1)
        if keep:
            keep_idx.append(i)
            continue
        if len(removed_examples) < 20:
            removed_examples.append(
                {
                    "wdc_entity": wdc_entities[i],
                    "wikidata_entity": wd_entities_raw[i],
                    "wdc_entity_count": int(wdc_counts.get(wdc_key, 0)),
                    "wikidata_entity_count": int(wd_counts.get(wd_key, 0)),
                    "wdc_value": wdc_values[i] if i < len(wdc_values) else "",
                    "wikidata_value": wd_values[i] if i < len(wd_values) else "",
                }
            )

    def _pick(values):
        if not values:
            return []
        return [values[i] for i in keep_idx if i < len(values)]

    filtered_wdc = [wdc_entities[i] for i in keep_idx]
    filtered_wd = [wd_entities_raw[i] for i in keep_idx]
    filtered_wdc_values = _pick(wdc_values)
    filtered_wd_values = _pick(wd_values)

    report = {
        "enabled": True,
        "links_before": int(total),
        "links_after": int(len(filtered_wdc)),
        "removed_links": int(total - len(filtered_wdc)),
        "ambiguous_wdc_entities": int(sum(1 for v in wdc_counts.values() if v > 1)),
        "ambiguous_wikidata_entities": int(sum(1 for v in wd_counts.values() if v > 1)),
        "max_links_per_wdc_entity": int(max(wdc_counts.values()) if wdc_counts else 0),
        "max_links_per_wikidata_entity": int(max(wd_counts.values()) if wd_counts else 0),
        "examples_removed": removed_examples,
    }
    return filtered_wdc, filtered_wd, filtered_wdc_values, filtered_wd_values, report


def generate_benchmark(
    params,
    workers=None,
    should_cancel=None,
    set_phase=None,
    should_skip_build=None,
    on_checkpoint=None,
):
    start_ts = time.time()

    class_name = params.get("class_name")
    parts_spec = params.get("parts_spec") or "all"
    pattern = params.get("wdc_predicate_pattern")
    wikidata_property = params.get("wikidata_property") or None
    wkd_class = params.get("wkd_class") or None
    wkd_prop_class = params.get("wkd_prop_class") or None
    ignore_chars = params.get("ignore_chars") or None
    wdc_value_is_wikidata = bool(params.get("wdc_value_is_wikidata"))
    # WDC traversal depth is fixed to "full traversal" for web builds.
    # Keep this internal and stop exposing/persisting it as a user parameter.
    max_depth = -1
    match_min_length = int(params.get("match_min_length", 1))
    force_align = bool(params.get("force_align"))
    use_local_only = bool(params.get("use_local_only"))
    force_one_to_one_links = bool(params.get("force_one_to_one_links"))
    require_cached_align = bool(params.get("require_cached_align"))
    resume_build = bool(params.get("resume_build")) and require_cached_align
    resume_out_dir_raw = str(params.get("resume_out_dir") or "").strip()

    if not class_name:
        raise PipelineError("class_name is required")
    if not pattern:
        raise PipelineError("wdc_predicate_pattern is required")
    if not wdc_value_is_wikidata and not wikidata_property:
        raise PipelineError("wikidata_property is required")
    if wdc_value_is_wikidata and not wkd_class:
        raise PipelineError("wkd_class is required when wdc_value_is_wikidata is enabled")

    if ignore_chars:
        align.set_normalization(True)
        align.set_extra_strip_chars(align.parse_strip_list(ignore_chars))
    else:
        align.set_normalization(False)

    def _check_cancel():
        if should_cancel and should_cancel():
            raise PipelineError("Cancelled by user")

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

    align_params = {
        "class_name": class_name,
        "parts_spec": parts_spec,
        "pattern": pattern,
        "wikidata_property": wikidata_property,
        "wkd_class": wkd_class,
        "ignore_chars": ignore_chars,
        "wdc_value_is_wikidata": wdc_value_is_wikidata,
    }
    cache_hash = _config_hash(align_params)
    cache_dir = work_dir / "align_cache" / cache_hash
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "ALIGN_CONFIG.json").write_text(
        json.dumps(
            {
                "cache_hash": cache_hash,
                "align_params": align_params,
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
    type_filter_iris = align.default_type_filter_iris_for_class(class_name)

    if links_tsv.exists() and align_done.exists() and not force_align:
        reused_align = True
        align_pairs = _count_alignment_pairs(links_tsv)
    else:
        if require_cached_align:
            raise PipelineError("Cached align not found; build-only requested")
        if set_phase:
            set_phase("align")
        _check_cancel()

        try:
            wdc_map, matched_count = align.extract_unique_iris_from_files(
                decompressed_files,
                pattern,
                collect_top_props=False,
                parallel=True,
                workers=workers,
                lock_path=lock_path,
                progress_every=100,
                wdc_value_is_wd_iri=wdc_value_is_wikidata,
                type_filter_iris=type_filter_iris,
            )
        except Exception as e:
            if not _is_too_many_open_files(e):
                raise
            # Automatic degraded retry for low-FD environments.
            print("[WARN] Too many open files detected during align extraction; retrying in low-FD mode (parallel disabled).")
            wdc_map, matched_count = align.extract_unique_iris_from_files(
                decompressed_files,
                pattern,
                collect_top_props=False,
                parallel=False,
                workers=1,
                lock_path=lock_path,
                progress_every=100,
                wdc_value_is_wd_iri=wdc_value_is_wikidata,
                type_filter_iris=type_filter_iris,
            )
        if matched_count == 0:
            raise PipelineError("No WDC values matched the predicate pattern")

        _check_cancel()
        if wdc_value_is_wikidata:
            wd_entity_iris = set()
            for entries in wdc_map.values():
                for value, _iri in entries:
                    wd_iri = align.extract_wd_entity_iri(value)
                    if wd_iri:
                        wd_entity_iris.add(wd_iri)
            if not wd_entity_iris:
                raise PipelineError("No Wikidata URLs extracted from WDC values")
            wikidata_map = align.fetch_wikidata_values(
                wikidata_property=None,
                wkd_class=wkd_class,
                wkd_prop_class=None,
                entity_iris=sorted(wd_entity_iris),
            )
        else:
            wikidata_map = align.fetch_wikidata_values(
                wikidata_property,
                wkd_class,
                wkd_prop_class,
            )
        if not wikidata_map:
            if wdc_value_is_wikidata:
                raise PipelineError(
                    "No Wikidata entities matched class filter "
                    f"({wkd_class}) for extracted WDC Wikidata URLs "
                    f"({len(wd_entity_iris):,} entities)"
                )
            raise PipelineError("Failed to fetch Wikidata values")

        _check_cancel()
        matches, wdc_values_matched = align.fuzzy_link(
            wdc_map,
            wikidata_map,
            parallel=True,
            workers=workers,
            lock_path=lock_path,
            min_length=match_min_length,
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
            wikidata_property=wikidata_property,
            wkd_class=wkd_class,
            wkd_prop_class=wkd_prop_class,
            start_ts=start_ts,
        )

        if not links_tsv.exists():
            raise PipelineError("Links TSV not found after alignment")
        align_done.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")

    if reused_align:
        print(f"✅ Alignment done: {align_pairs:,} alignments found (cached).")
    else:
        print(f"✅ Alignment done: {align_pairs:,} alignments found.")

    if align_pairs == 0:
        reason = "No alignments found (0); build skipped."
        print(f"[INFO] {reason}")
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
        "class_name": class_name,
        "parts_spec": parts_spec,
        "wdc_predicate_pattern": pattern,
        "wikidata_property": wikidata_property,
        "wkd_class": wkd_class,
        "wdc_value_is_wikidata": wdc_value_is_wikidata,
        "ignore_chars": ignore_chars,
        "force_align": force_align,
        "use_local_only": use_local_only,
        "force_one_to_one_links": force_one_to_one_links,
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

    one_to_one_report = None
    if force_one_to_one_links:
        (
            wdc_entities,
            wd_entities_raw,
            wdc_values,
            wd_values,
            one_to_one_report,
        ) = _filter_links_one_to_one(
            wdc_entities,
            wd_entities_raw,
            wdc_values,
            wd_values,
        )
        print(
            "[INFO] 1-to-1 link filter: "
            f"kept {one_to_one_report['links_after']:,}/{one_to_one_report['links_before']:,} "
            f"links (removed {one_to_one_report['removed_links']:,})."
        )
        try:
            (out_dir / "LINK_FILTER_1TO1.json").write_text(
                json.dumps(one_to_one_report, indent=2, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            pass
        if not wdc_entities or not wd_entities_raw:
            reason = "No 1-to-1 links left after filtering; build skipped."
            print(f"[INFO] {reason}")
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

    wdc_mask_values = set(v for v in wdc_values if v)
    wd_mask_values = set(v for v in wd_values if v)

    wdc_exclude_props = set()
    wd_exclude_props = set()
    wd_link_prop_uris = set()
    wdc_link_prop_patterns = set()
    if pattern:
        wdc_link_prop_patterns.add(str(pattern).lower())
    if wikidata_property:
        norm_prop = build.normalize_wd_prop_id(str(wikidata_property))
        if norm_prop:
            wd_link_prop_uris = build.wikidata_prop_uris(norm_prop)

    replace_map = {}
    lowercase_wd = True
    add_wd_labels = True

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
        sparql_url="https://query.wikidata.org/sparql",
        lang="en",
        batch_size=int(params.get("wd_batch_size", 50)),
        sleep=float(params.get("wd_sleep", 0.2)),
        timeout=int(params.get("wd_timeout", 60)),
        retries=int(params.get("wd_retries", 3)),
        backoff=float(params.get("wd_backoff", 2.0)),
        no_lowercase_wd=False,
        resume=bool(resume_build),
        state_file=None,
        max_depth=max_depth,
        progress_every=10_000_000,
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
