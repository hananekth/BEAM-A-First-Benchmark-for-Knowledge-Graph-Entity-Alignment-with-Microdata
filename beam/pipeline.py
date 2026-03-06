import os
import json
import hashlib
import time
import errno
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
    if mode == "identifier":
        return "property"
    if mode in {"property", "sameas"}:
        return mode
    return "sameas" if bool(fallback_wdc_value_is_wikidata) else "property"


def _is_wikidata_url_mode(params):
    data = params if isinstance(params, dict) else {}
    return _normalize_matching_mode(
        data.get("matching_mode"),
        fallback_wdc_value_is_wikidata=bool(data.get("wdc_value_is_wikidata")),
    ) == "sameas"


def _align_params_from_job_params(params):
    data = params if isinstance(params, dict) else {}
    wdc_value_is_wikidata = _normalize_matching_mode(
        data.get("matching_mode"),
        fallback_wdc_value_is_wikidata=bool(data.get("wdc_value_is_wikidata")),
    ) == "sameas"
    return {
        "class_name": data.get("class_name"),
        "parts_spec": data.get("parts_spec") or "all",
        "pattern": data.get("wdc_predicate_pattern"),
        "wikidata_property": data.get("wikidata_property") or None,
        "wkd_class": data.get("wkd_class") or None,
        "ignore_chars": data.get("ignore_chars") or None,
        "wdc_value_is_wikidata": bool(wdc_value_is_wikidata),
    }


def _align_cache_dir_for_params(params):
    align_params = _align_params_from_job_params(params)
    class_name = str(align_params.get("class_name") or "").strip()
    if not class_name:
        return None, align_params
    cache_hash = _config_hash(align_params)
    cache_dir = Path("Download") / class_name / "align_cache" / cache_hash
    return cache_dir, align_params


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


def _canonical_link_value_for_dedup(value) -> str:
    return str(value or "").strip()


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


def _exact_wdc_subgraph_signature(root_subject, outgoing):
    """
    Canonical signature of a WDC subject outgoing subgraph.

    Includes all outgoing properties. Bnode IDs are ignored by recursively canonicalizing
    their outgoing subgraphs; IRIs/literals are kept as exact values (except angle brackets
    stripped for IRIs/properties for token-shape stability).
    """
    memo = {}
    stack = set()

    def _canon_node(node):
        if node in memo:
            return memo[node]
        if node in stack:
            # Defensive cycle sentinel (should be rare in WDC extracted snippets).
            return ("<cycle>",)
        stack.add(node)
        items = []
        for p, o in outgoing.get(node, []):
            p_key = _canonical_wdc_token(p)
            if isinstance(o, str) and o.startswith("_:"):
                obj_key = ("BNODE", _canon_node(o))
            elif isinstance(o, str) and o.startswith('"'):
                obj_key = ("LIT", o)
            else:
                obj_key = ("IRI", _canonical_wdc_token(o))
            items.append((p_key, obj_key))
        items.sort(key=repr)
        sig = tuple(items)
        stack.remove(node)
        memo[node] = sig
        return sig

    root_key = _canonical_wdc_token(root_subject)
    if root_key not in outgoing:
        return None
    return _canon_node(root_key)


def _dedup_links_exact_wdc_subgraph_by_link_value(
    wdc_nq_paths,
    wdc_entities,
    wd_entities_raw,
    wdc_values=None,
    wd_values=None,
    should_cancel=None,
):
    """
    Deduplicate repeated WDC mentions *within the same linking value* by exact WDC subgraph.

    The comparison is strict:
    - all outgoing properties are considered
    - bnode IDs are ignored, but bnode contents are compared recursively
    - IRI/literal values are compared exactly (stable token shape for IRIs)

    To preserve Wikidata-side ambiguity for downstream filtering, duplicates are collapsed
    per (linking_value, WDC_signature, Wikidata_entity).
    """
    total = min(len(wdc_entities), len(wd_entities_raw))
    wdc_entities = list(wdc_entities[:total])
    wd_entities_raw = list(wd_entities_raw[:total])
    wdc_values = list(wdc_values or [])
    wd_values = list(wd_values or [])

    if total <= 0:
        empty_report = {
            "enabled": True,
            "links_before": 0,
            "links_after": 0,
            "filtered_out_links": 0,
            "reason": "no_links",
            "multi_link_value_groups": 0,
            "subjects_profiled": 0,
            "subjects_unprofiled": 0,
            "exact_duplicate_clusters": 0,
            "scan": {"scan_passes": 0, "parsed_lines": 0, "matched_subject_lines": 0},
            "examples_filtered_out": [],
        }
        return wdc_entities, wd_entities_raw, wdc_values, wd_values, empty_report

    if len(wdc_values) < total:
        report = {
            "enabled": True,
            "links_before": int(total),
            "links_after": int(total),
            "filtered_out_links": 0,
            "reason": "missing_wdc_values",
            "multi_link_value_groups": 0,
            "subjects_profiled": 0,
            "subjects_unprofiled": int(len({_canonical_wdc_link_entity(v) for v in wdc_entities})),
            "exact_duplicate_clusters": 0,
            "scan": {"scan_passes": 0, "parsed_lines": 0, "matched_subject_lines": 0},
            "examples_filtered_out": [],
        }
        return wdc_entities, wd_entities_raw, wdc_values, wd_values, report

    group_to_indices = defaultdict(list)
    wdc_keys = [_canonical_wdc_link_entity(v) for v in wdc_entities]
    wd_keys = [_canonical_wd_link_entity(v) for v in wd_entities_raw]
    link_value_keys = [_canonical_link_value_for_dedup(v) for v in wdc_values[:total]]
    for i, link_key in enumerate(link_value_keys):
        group_to_indices[link_key].append(i)

    multi_group_indices = [idxs for idxs in group_to_indices.values() if len(idxs) > 1]
    if not multi_group_indices:
        report = {
            "enabled": True,
            "links_before": int(total),
            "links_after": int(total),
            "filtered_out_links": 0,
            "reason": "no_duplicate_link_values",
            "multi_link_value_groups": 0,
            "subjects_profiled": 0,
            "subjects_unprofiled": 0,
            "exact_duplicate_clusters": 0,
            "scan": {"scan_passes": 0, "parsed_lines": 0, "matched_subject_lines": 0},
            "examples_filtered_out": [],
        }
        return wdc_entities, wd_entities_raw, wdc_values, wd_values, report

    subjects_to_profile = {wdc_keys[i] for idxs in multi_group_indices for i in idxs if wdc_keys[i]}
    print(
        "[WDC_DEDUP] "
        f"starting exact-subgraph dedup on {len(subjects_to_profile):,} WDC subject(s) "
        f"across {len(multi_group_indices):,} duplicate linking-value group(s)"
    )
    outgoing, scan_report = _collect_wdc_outgoing_subgraphs(wdc_nq_paths, subjects_to_profile, should_cancel=should_cancel)

    signature_cache = {}
    signature_hashes = {}
    subjects_unprofiled = set()

    for subject in subjects_to_profile:
        sig = _exact_wdc_subgraph_signature(subject, outgoing)
        if sig is None:
            subjects_unprofiled.add(subject)
            continue
        signature_cache[subject] = sig
        signature_hashes[subject] = hashlib.sha256(repr(sig).encode("utf-8")).hexdigest()[:16]

    keep_idx = []
    examples_filtered_out = []
    seen_cluster_keys = set()
    exact_duplicate_clusters = set()
    for i in range(total):
        link_key = link_value_keys[i] if i < len(link_value_keys) else ""
        # Singletons are always kept.
        if len(group_to_indices.get(link_key, ())) <= 1:
            keep_idx.append(i)
            continue
        subject_key = wdc_keys[i]
        sig_hash = signature_hashes.get(subject_key)
        if not sig_hash:
            keep_idx.append(i)
            continue
        cluster_key = (link_key, sig_hash, wd_keys[i])
        if cluster_key in seen_cluster_keys:
            exact_duplicate_clusters.add((link_key, sig_hash))
            if len(examples_filtered_out) < 20:
                rep_wdc = None
                for j in keep_idx:
                    if (
                        (link_value_keys[j] if j < len(link_value_keys) else "") == link_key
                        and signature_hashes.get(wdc_keys[j]) == sig_hash
                        and wd_keys[j] == wd_keys[i]
                    ):
                        rep_wdc = wdc_entities[j]
                        break
                examples_filtered_out.append(
                    {
                        "wdc_entity_filtered_out": wdc_entities[i],
                        "wikidata_entity": wd_entities_raw[i],
                        "wdc_value": wdc_values[i] if i < len(wdc_values) else "",
                        "wikidata_value": wd_values[i] if i < len(wd_values) else "",
                        "link_value_group": link_key,
                        "signature_hash": sig_hash,
                        "representative_wdc_entity": rep_wdc or "",
                    }
                )
            continue
        seen_cluster_keys.add(cluster_key)
        keep_idx.append(i)

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
        "filtered_out_links": int(total - len(filtered_wdc)),
        "reason": "ok",
        "multi_link_value_groups": int(sum(1 for idxs in group_to_indices.values() if len(idxs) > 1)),
        "subjects_profiled": int(len(signature_hashes)),
        "subjects_unprofiled": int(len(subjects_unprofiled)),
        "exact_duplicate_clusters": int(len(exact_duplicate_clusters)),
        "scan": scan_report,
        "examples_filtered_out": examples_filtered_out,
    }
    print(
        "[WDC_DEDUP] "
        f"profiled {report['subjects_profiled']:,}/{len(subjects_to_profile):,} subjects | "
        f"exact duplicate clusters={report['exact_duplicate_clusters']:,}"
    )
    return filtered_wdc, filtered_wd, filtered_wdc_values, filtered_wd_values, report


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
            "filtered_out_links": 0,
            "ambiguous_wdc_entities": 0,
            "ambiguous_wikidata_entities": 0,
            "max_links_per_wdc_entity": 0,
            "max_links_per_wikidata_entity": 0,
            "examples_filtered_out": [],
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
    filtered_out_examples = []
    for i, (wdc_key, wd_key) in enumerate(zip(wdc_keys, wd_keys)):
        keep = (wdc_counts.get(wdc_key, 0) == 1) and (wd_counts.get(wd_key, 0) == 1)
        if keep:
            keep_idx.append(i)
            continue
        if len(filtered_out_examples) < 20:
            filtered_out_examples.append(
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
        "filtered_out_links": int(total - len(filtered_wdc)),
        "ambiguous_wdc_entities": int(sum(1 for v in wdc_counts.values() if v > 1)),
        "ambiguous_wikidata_entities": int(sum(1 for v in wd_counts.values() if v > 1)),
        "max_links_per_wdc_entity": int(max(wdc_counts.values()) if wdc_counts else 0),
        "max_links_per_wikidata_entity": int(max(wd_counts.values()) if wd_counts else 0),
        "examples_filtered_out": filtered_out_examples,
    }
    return filtered_wdc, filtered_wd, filtered_wdc_values, filtered_wd_values, report


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
    wikidata_property = params.get("wikidata_property") or None
    wkd_class = params.get("wkd_class") or None
    wkd_prop_class = params.get("wkd_prop_class") or None
    ignore_chars = params.get("ignore_chars") or None
    wdc_value_is_wikidata = matching_mode == "sameas"
    # WDC traversal depth is fixed to "full traversal" for web builds.
    # Keep this internal and stop exposing/persisting it as a user parameter.
    max_depth = -1
    match_min_length = int(params.get("match_min_length", 1))
    force_align = bool(params.get("force_align"))
    use_local_only = bool(params.get("use_local_only"))
    force_one_to_one_links = bool(params.get("force_one_to_one_links"))
    dedup_wdc_exact_subgraph_by_link_value = bool(params.get("dedup_wdc_exact_subgraph_by_link_value"))
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
            "wikidata_property": wikidata_property,
            "wkd_class": wkd_class,
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
        print("✅ Alignment stage completed (cached).")
    else:
        print("✅ Alignment stage completed.")

    if dedup_wdc_exact_subgraph_by_link_value:
        print("ℹ️ Final entity links (exact) pending exact-subgraph dedup (can be long on large classes).")
    elif force_one_to_one_links:
        print("ℹ️ Final entity links (exact) pending 1-to-1 filtering.")
    else:
        # No build-side prefilter is enabled: align pairs are already the final links.
        _emit_final_links_count(
            align_pairs,
            source="align",
            exact=True,
            raw_links=align_pairs,
            links_after_dedup=align_pairs,
            links_after_one_to_one=align_pairs,
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
        "wikidata_property": wikidata_property,
        "wkd_class": wkd_class,
        "ignore_chars": ignore_chars,
        "force_align": force_align,
        "use_local_only": use_local_only,
        "force_one_to_one_links": force_one_to_one_links,
        "dedup_wdc_exact_subgraph_by_link_value": dedup_wdc_exact_subgraph_by_link_value,
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
    raw_links_before_filters = len(wdc_entities)
    links_after_dedup = raw_links_before_filters

    dedup_report = None
    if dedup_wdc_exact_subgraph_by_link_value:
        (
            wdc_entities,
            wd_entities_raw,
            wdc_values,
            wd_values,
            dedup_report,
        ) = _dedup_links_exact_wdc_subgraph_by_link_value(
            wdc_nq,
            wdc_entities,
            wd_entities_raw,
            wdc_values,
            wd_values,
            should_cancel=should_cancel,
        )
        print(
            "[INFO] WDC exact subgraph dedup (by linking value): "
            f"kept {dedup_report['links_after']:,}/{dedup_report['links_before']:,} "
            f"links (filtered out {dedup_report['filtered_out_links']:,})."
        )
        try:
            (out_dir / "WDC_DEDUP_EXACT_BY_LINK_VALUE.json").write_text(
                json.dumps(dedup_report, indent=2, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            pass
        links_after_dedup = len(wdc_entities)
        if not wdc_entities or not wd_entities_raw:
            reason = "No links left after exact WDC dedup by linking value; build skipped."
            print(f"[INFO] {reason}")
            _emit_final_links_count(
                0,
                source="build_prefilter",
                exact=True,
                raw_links=raw_links_before_filters,
                links_after_dedup=0,
                links_after_one_to_one=0,
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
            f"links (filtered out {one_to_one_report['filtered_out_links']:,})."
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
            _emit_final_links_count(
                0,
                source="build_prefilter",
                exact=True,
                raw_links=raw_links_before_filters,
                links_after_dedup=links_after_dedup,
                links_after_one_to_one=0,
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
    _emit_final_links_count(
        final_links_count,
        source="build_prefilter",
        exact=True,
        raw_links=raw_links_before_filters,
        links_after_dedup=links_after_dedup,
        links_after_one_to_one=final_links_count if force_one_to_one_links else None,
    )

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
        sparql_url="https://query.wikidata.org/sparql",
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
