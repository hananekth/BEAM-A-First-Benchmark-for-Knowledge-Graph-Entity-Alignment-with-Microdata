#!/usr/bin/env python3
import argparse
import os
import re
import json
import sys
import time
import fcntl
from pathlib import Path
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, as_completed, wait
from typing import Iterable, List
from urllib.parse import parse_qs, unquote, urlparse

import requests


QUAD_RE = re.compile(
    r'^\s*(<[^>]+>|_:[^\s]+)\s+(<[^>]+>)\s+(".*?"(?:\^\^<[^>]+>|@[a-zA-Z-]+)?|<[^>]+>|_:[^\s]+)\s+(<[^>]+>)\s+\.\s*$'
)
TRIPLE_RE = re.compile(
    r'^\s*(<[^>]+>|_:[^\s]+)\s+(<[^>]+>)\s+(".*?"(?:\^\^<[^>]+>|@[a-zA-Z-]+)?|<[^>]+>|_:[^\s]+)\s+\.\s*$'
)


def parse_nq_or_nt(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    m = QUAD_RE.match(line)
    if m:
        s, p, o, _g = m.groups()
        return s, p, o
    m = TRIPLE_RE.match(line)
    if m:
        s, p, o = m.groups()
        return s, p, o
    return None


def _format_eta(seconds):
    if seconds is None or seconds < 0:
        return "ETA: N/A"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"ETA: {h}h{m:02d}m{s:02d}s"
    if m:
        return f"ETA: {m}m{s:02d}s"
    return f"ETA: {s}s"


def _normalize_prop_token(value):
    token = value.strip("<>")
    if token.startswith("http://www.wikidata.org/"):
        return token.lower()
    return token


def _eta_update(start_ts, done_bytes, total_bytes):
    if done_bytes <= 0:
        return "ETA: N/A"
    elapsed = time.time() - start_ts
    if elapsed <= 0:
        return "ETA: N/A"
    rate = done_bytes / elapsed
    remaining = max(0, total_bytes - done_bytes)
    return _format_eta(remaining / rate if rate > 0 else None)


def _progress_line(start_ts, done_bytes, total_bytes):
    pct = 0.0 if total_bytes <= 0 else (done_bytes / total_bytes) * 100
    return f"{pct:5.1f}% | {_eta_update(start_ts, done_bytes, total_bytes)}"


def _split_worker(args):
    (
        input_path,
        targets,
        lowercase_wd,
        mask_values,
        exclude_props,
        exclude_prop_patterns,
        replace_map,
        follow_iri_objects,
    ) = args
    tmp_attr = input_path + f".tmp_attr_{os.getpid()}"
    tmp_rel = input_path + f".tmp_rel_{os.getpid()}"
    new_subjects = set()
    line_count = 0
    kept_attr = 0
    kept_rel = 0
    exclude_props_norm = {_normalize_prop_token(p) for p in exclude_props} if exclude_props else None
    with open(input_path, "r", encoding="utf-8") as f, \
         open(tmp_attr, "w", encoding="utf-8") as attr_out, \
         open(tmp_rel, "w", encoding="utf-8") as rel_out:
        for line in f:
            line_count += 1
            parsed = parse_nq_or_nt(line)
            if not parsed:
                continue
            s, p, o = parsed
            p_norm = _normalize_prop_token(p)
            if s not in targets:
                continue
            if exclude_props_norm and p_norm in exclude_props_norm:
                continue
            if exclude_prop_patterns and any(pat in p_norm.lower() for pat in exclude_prop_patterns):
                continue
            if replace_map:
                s_key = s.strip("<>")
                if s_key in replace_map:
                    s = replace_map[s_key]
            if replace_map and (not o.startswith('"')):
                o_key = o.strip("<>")
                if o_key in replace_map:
                    o = replace_map[o_key]
            s_out, p_out, o_out = transform_triple(s, p, o, lowercase_wd)
            if o.startswith('"'):
                o_out = clean_literal(o_out)
                if mask_values:
                    lex = literal_lex(o_out)
                    if lex in mask_values:
                        continue
                attr_out.write(f"{s_out}\t{p_out}\t{o_out}\n")
                kept_attr += 1
            else:
                rel_out.write(f"{s_out}\t{p_out}\t{o_out}\n")
                kept_rel += 1
                if o.startswith("_:"):
                    new_subjects.add(o)
                elif follow_iri_objects and o.startswith("<"):
                    new_subjects.add(o)
    size = os.path.getsize(input_path)
    return tmp_attr, tmp_rel, new_subjects, line_count, kept_attr, kept_rel, size


def _count_worker(args):
    path, subjects, exclude_props, exclude_prop_patterns, mask_values = args
    local = {}
    exclude_props_norm = {_normalize_prop_token(p) for p in exclude_props} if exclude_props else None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parsed = parse_nq_or_nt(line)
            if not parsed:
                continue
            s, p, o = parsed
            p_norm = _normalize_prop_token(p)
            if s not in subjects:
                continue
            if exclude_props_norm and p_norm in exclude_props_norm:
                continue
            if exclude_prop_patterns and any(pat in p_norm.lower() for pat in exclude_prop_patterns):
                continue
            if mask_values and o.startswith('"'):
                lex = literal_lex(o)
                if lex in mask_values:
                    continue
            local[s] = local.get(s, 0) + 1
    size = os.path.getsize(path)
    return local, size


def _labels_worker(args):
    path, target_iris, label_preds = args
    tmp = path + f".tmp_wdc_labels_{os.getpid()}"
    written = 0
    with open(path, "r", encoding="utf-8") as f, open(tmp, "w", encoding="utf-8") as out:
        for line in f:
            parsed = parse_nq_or_nt(line)
            if not parsed:
                continue
            s, p, o = parsed
            s_norm = s.strip("<>")
            p_norm = p.strip("<>")
            if s_norm not in target_iris:
                continue
            if p_norm not in label_preds:
                continue
            if o.startswith('"'):
                o = clean_literal(o)
            out.write(f"{s}\t{p}\t{o}\n")
            written += 1
    size = os.path.getsize(path)
    return tmp, written, size


def _prop_label_worker(args):
    path, targets, label_preds = args
    local_labels = {}
    local_descs = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parsed = parse_nq_or_nt(line)
            if not parsed:
                continue
            s, p, o = parsed
            s_norm = s.strip("<>")
            p_norm = p.strip("<>")
            if s_norm not in targets:
                continue
            if p_norm not in label_preds:
                continue
            lex = literal_lex(o) or o.strip('"')
            if p_norm.endswith("#label") or p_norm.endswith("prefLabel"):
                if s not in local_labels:
                    local_labels[s] = lex
            elif p_norm.endswith("description"):
                if s not in local_descs:
                    local_descs[s] = lex
    size = os.path.getsize(path)
    return local_labels, local_descs, size


def _is_pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def compute_shared_workers(lock_path, share=0.8):
    cpu = os.cpu_count() or 1
    lock_path = os.path.abspath(lock_path)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        lines = [ln.strip() for ln in f.readlines() if ln.strip()]
        active_pids = []
        for ln in lines:
            try:
                pid_str, _ts = ln.split(",", 1)
                pid = int(pid_str)
            except Exception:
                continue
            if _is_pid_alive(pid):
                active_pids.append(pid)
        if os.getpid() not in active_pids:
            active_pids.append(os.getpid())
        f.seek(0)
        f.truncate()
        now = int(time.time())
        for pid in active_pids:
            f.write(f"{pid},{now}\n")
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)
    runs = max(1, len(active_pids))
    workers = max(1, int((cpu * share) / runs))
    return workers, runs, cpu


def _iter_input_paths(input_paths):
    if isinstance(input_paths, (list, tuple)):
        return list(input_paths)
    return [input_paths]


def normalize_header(value):
    return value.strip().lower().replace(" ", "")


def read_links(path, sep, wdc_col, wd_col, wdc_value_col, wd_value_col):
    wdc_entities = []
    wd_entities = []
    wdc_values = []
    wd_values = []
    with open(path, "r", encoding="utf-8") as f:
        first = f.readline()
        if not first:
            return wdc_entities, wd_entities, wdc_values, wd_values
        parts = [normalize_header(p) for p in first.rstrip("\n").split(sep)]
        header = "wdc_iri" in parts and "wikidata_uri" in parts
        header_map = {name: idx for idx, name in enumerate(parts)}

        if header:
            wdc_col = header_map.get("wdc_iri", wdc_col)
            wd_col = header_map.get("wikidata_uri", wd_col)
            if wdc_value_col is None:
                wdc_value_col = header_map.get("wdc_value")
            if wd_value_col is None:
                wd_value_col = header_map.get("wiki_value")
        else:
            parts = first.rstrip("\n").split(sep)
            if len(parts) > max(wdc_col, wd_col):
                wdc_entities.append(parts[wdc_col].strip())
                wd_entities.append(parts[wd_col].strip())
                if wdc_value_col is not None and len(parts) > wdc_value_col:
                    wdc_values.append(parts[wdc_value_col].strip())
                if wd_value_col is not None and len(parts) > wd_value_col:
                    wd_values.append(parts[wd_value_col].strip())

        for line in f:
            cols = line.rstrip("\n").split(sep)
            if len(cols) <= max(wdc_col, wd_col):
                continue
            wdc_entities.append(cols[wdc_col].strip())
            wd_entities.append(cols[wd_col].strip())
            if wdc_value_col is not None and len(cols) > wdc_value_col:
                wdc_values.append(cols[wdc_value_col].strip())
            if wd_value_col is not None and len(cols) > wd_value_col:
                wd_values.append(cols[wd_value_col].strip())
    return wdc_entities, wd_entities, wdc_values, wd_values


def normalize_wd_uri(value, lowercase):
    # Normalize URI token shape first so all downstream files use a stable form.
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    if lowercase and value.startswith("http://www.wikidata.org/"):
        return value.lower()
    return value


def transform_triple(s, p, o, lowercase):
    s = normalize_wd_uri(s, lowercase)
    p = normalize_wd_uri(p, lowercase)
    if not o.startswith('"'):
        o = normalize_wd_uri(o, lowercase)
    return s, p, o


def write_links(path, wdc_entities, wd_entities, dedupe):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    seen = set()
    with open(path, "w", encoding="utf-8") as out:
        for wdc, wd in zip(wdc_entities, wd_entities):
            wdc = wdc.strip().strip("<>")
            wd = canonical_wd_link_entity_uri(wd)
            if not wdc or not wd:
                continue
            if dedupe:
                key = (wdc, wd)
                if key in seen:
                    continue
                seen.add(key)
            out.write(f"{wdc}\t{wd}\n")


LITERAL_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"(?:(?:\^\^<[^>]+>)|@[a-zA-Z-]+)?$')


def literal_lex(value):
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


def clean_literal(value):
    if not value.startswith('"'):
        return value
    lex = literal_lex(value)
    if lex is None:
        match = LITERAL_RE.match(value)
        if not match:
            return value
        lex = match.group(1)
    return f"\"{lex}\""


def _cleanup_stale_temp_files(input_paths, stale_after_s=None):
    """
    Best-effort cleanup for orphaned worker temp files from interrupted runs.
    Uses an age threshold to avoid touching temp files from actively running workers.
    """
    if stale_after_s is None:
        try:
            stale_after_s = int(os.environ.get("BEAM_TMP_CLEANUP_STALE_S", "300"))
        except Exception:
            stale_after_s = 300
    now = time.time()
    seen = set()
    for raw in _iter_input_paths(input_paths):
        p = Path(raw)
        parent = p.parent
        stem = p.name
        for pat in (
            f"{stem}.tmp_attr_*",
            f"{stem}.tmp_rel_*",
            f"{stem}.tmp_wdc_labels_*",
        ):
            for cand in parent.glob(pat):
                key = str(cand.resolve())
                if key in seen:
                    continue
                seen.add(key)
                try:
                    age = now - cand.stat().st_mtime
                    if age < stale_after_s:
                        continue
                    cand.unlink(missing_ok=True)
                except Exception:
                    pass


def split_triples(
    input_path,
    out_attr_path,
    out_rel_path,
    seed_subjects,
    max_depth,
    lowercase_wd=False,
    mask_values=None,
    exclude_props=None,
    exclude_prop_patterns=None,
    replace_map=None,
    progress_every=0,
    follow_iri_objects=False,
):
    _cleanup_stale_temp_files(input_path)
    os.makedirs(os.path.dirname(out_attr_path), exist_ok=True)
    os.makedirs(os.path.dirname(out_rel_path), exist_ok=True)

    keep_subjects = set(s for s in seed_subjects if s)
    processed_subjects = set()

    input_paths = _iter_input_paths(input_path)
    with open(out_attr_path, "w", encoding="utf-8") as attr_out, \
         open(out_rel_path, "w", encoding="utf-8") as rel_out:
        depth = 0
        while True:
            if max_depth >= 0 and depth > max_depth:
                break
            targets = keep_subjects - processed_subjects
            if not targets:
                break
            new_subjects = set()
            line_count = 0
            kept_attr = 0
            kept_rel = 0
            lock_path = os.path.join("Download", ".workers.lock")
            n_workers, _runs, _cpu = compute_shared_workers(lock_path, share=0.8)
            total_bytes = sum(os.path.getsize(p) for p in input_paths)
            done_bytes = 0
            start_ts = time.time()
            if progress_every:
                prog = _progress_line(start_ts, done_bytes, total_bytes)
                print(f"[WDC] depth={depth} progress {done_bytes}/{total_bytes} bytes {prog}", file=sys.stderr)
            with ProcessPoolExecutor(max_workers=n_workers) as ex:
                futures = [
                    ex.submit(
                        _split_worker,
                        (
                            input_path,
                            targets,
                            lowercase_wd,
                            mask_values,
                            exclude_props,
                            exclude_prop_patterns,
                            replace_map,
                            follow_iri_objects,
                        ),
                    )
                    for input_path in input_paths
                ]
                for fut in as_completed(futures):
                    tmp_attr, tmp_rel, new_subs, lines, ka, kr, fsize = fut.result()
                    line_count += lines
                    kept_attr += ka
                    kept_rel += kr
                    new_subjects.update(new_subs)
                    done_bytes += fsize
                    prog = _progress_line(start_ts, done_bytes, total_bytes)
                    print(f"[WDC] depth={depth} progress {done_bytes}/{total_bytes} bytes {prog}", file=sys.stderr)
                    try:
                        with open(tmp_attr, "r", encoding="utf-8") as f_attr:
                            for line in f_attr:
                                attr_out.write(line)
                        with open(tmp_rel, "r", encoding="utf-8") as f_rel:
                            for line in f_rel:
                                rel_out.write(line)
                    finally:
                        try:
                            os.remove(tmp_attr)
                        except Exception:
                            pass
                        try:
                            os.remove(tmp_rel)
                        except Exception:
                            pass
            if progress_every:
                done_bytes = total_bytes
                prog = _progress_line(start_ts, done_bytes, total_bytes)
                print(f"[WDC] depth={depth} progress {done_bytes}/{total_bytes} bytes {prog}", file=sys.stderr)
            processed_subjects.update(targets)
            keep_subjects.update(new_subjects)
            print(
                f"[WDC] depth={depth} done lines={line_count} "
                f"attr={kept_attr} rel={kept_rel} new_bnodes={len(new_subjects)}",
                file=sys.stderr,
            )
            depth += 1
    _cleanup_stale_temp_files(input_path)


def batch_iter(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def dedupe_preserve_order(items):
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _read_raw_wd_triples(path):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) != 3:
                continue
            yield parts[0], parts[1], parts[2]


def count_wdc_triples(input_path, subjects, exclude_props=None, exclude_prop_patterns=None, mask_values=None):
    counts = {s: 0 for s in subjects}
    input_paths = _iter_input_paths(input_path)

    # Parallel over parts
    lock_path = os.path.join("Download", ".workers.lock")
    n_workers, _runs, _cpu = compute_shared_workers(lock_path, share=0.8)
    total_bytes = sum(os.path.getsize(p) for p in input_paths)
    done_bytes = 0
    start_ts = time.time()
    prog = _progress_line(start_ts, done_bytes, total_bytes)
    print(f"[WDC] count_triples progress {done_bytes}/{total_bytes} bytes {prog}", file=sys.stderr)
    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futures = [
            ex.submit(_count_worker, (p, set(subjects), exclude_props, exclude_prop_patterns, mask_values))
            for p in input_paths
        ]
        for fut in as_completed(futures):
            local, fsize = fut.result()
            for s, c in local.items():
                counts[s] += c
            done_bytes += fsize
            prog = _progress_line(start_ts, done_bytes, total_bytes)
            print(f"[WDC] count_triples progress {done_bytes}/{total_bytes} bytes {prog}", file=sys.stderr)
    done_bytes = total_bytes
    prog = _progress_line(start_ts, done_bytes, total_bytes)
    print(f"[WDC] count_triples progress {done_bytes}/{total_bytes} bytes {prog}", file=sys.stderr)
    return counts


def filter_links_by_wdc(wdc_entities, wd_entities, wdc_values, wd_values, allowed_wdc):
    new_wdc = []
    new_wd = []
    new_wdc_vals = []
    new_wd_vals = []
    for wdc, wd, wv, wdv in zip(wdc_entities, wd_entities, wdc_values, wd_values):
        if wdc in allowed_wdc:
            new_wdc.append(wdc)
            new_wd.append(wd)
            new_wdc_vals.append(wv)
            new_wd_vals.append(wdv)
    return new_wdc, new_wd, new_wdc_vals, new_wd_vals


def build_wd_merge_map(wd_entities, wd_values):
    value_to_ents = {}
    for ent, val in zip(wd_entities, wd_values):
        if not val:
            continue
        value_to_ents.setdefault(val, set()).add(ent)
    replace_map = {}
    for ents in value_to_ents.values():
        if len(ents) <= 1:
            continue
        canonical = sorted(ents)[0]
        for ent in ents:
            if ent != canonical:
                replace_map[ent] = canonical
    return replace_map


def count_props_in_files(paths, exclude_props=None):
    counts = {}
    for path in paths:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue
                p = parts[1]
                if exclude_props and p in exclude_props:
                    continue
                counts[p] = counts.get(p, 0) + 1
    return counts


def filter_triples_by_prop_count(
    in_attr,
    in_rel,
    out_attr,
    out_rel,
    min_count,
    exclude_props=None,
):
    counts = count_props_in_files([in_attr, in_rel], exclude_props=exclude_props)
    os.makedirs(os.path.dirname(out_attr), exist_ok=True)
    os.makedirs(os.path.dirname(out_rel), exist_ok=True)
    with open(in_attr, "r", encoding="utf-8") as fin, \
         open(out_attr, "w", encoding="utf-8") as fout:
        for line in fin:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 3:
                continue
            p = parts[1]
            if exclude_props and p in exclude_props:
                continue
            if counts.get(p, 0) >= min_count:
                fout.write(line)


def wikidata_prop_uris(prop_id):
    prop_id = prop_id.lower()
    return {
        f"http://www.wikidata.org/prop/direct/{prop_id}",
        f"http://www.wikidata.org/prop/direct-normalized/{prop_id}",
        f"http://www.wikidata.org/prop/{prop_id}",
        f"http://www.wikidata.org/prop/statement/{prop_id}",
        f"http://www.wikidata.org/prop/statement/value/{prop_id}",
        f"http://www.wikidata.org/prop/statement/value-normalized/{prop_id}",
        f"http://www.wikidata.org/prop/qualifier/{prop_id}",
        f"http://www.wikidata.org/prop/qualifier/value/{prop_id}",
        f"http://www.wikidata.org/prop/qualifier/value-normalized/{prop_id}",
        f"http://www.wikidata.org/prop/reference/{prop_id}",
        f"http://www.wikidata.org/prop/reference/value/{prop_id}",
        f"http://www.wikidata.org/prop/reference/value-normalized/{prop_id}",
    }


def schema_org_prop_uris(prop_name):
    return {
        f"http://schema.org/{prop_name}",
        f"https://schema.org/{prop_name}",
    }


def normalize_wd_prop_id(value):
    match = re.search(r'P\d+', value, re.IGNORECASE)
    if not match:
        return None
    return match.group(0).lower()


def prop_uri_to_entity(uri):
    uri = uri.strip("<>")
    if "wikidata.org/prop/" not in uri:
        return None
    tail = uri.rstrip("/").split("/")[-1]
    if not (tail.startswith("P") or tail.startswith("p")):
        return None
    return f"http://www.wikidata.org/entity/{tail.upper()}"


def canonical_wd_entity_uri(uri):
    uri = (uri or "").strip().strip("<>")
    if not uri:
        return uri
    match = re.match(r"^https?://www\.wikidata\.org/entity/([pqPQ]\d+)$", uri)
    if match:
        return f"http://www.wikidata.org/entity/{match.group(1).upper()}"
    return uri


def _extract_wikidata_entity_id(uri):
    text = (uri or "").strip()
    if not text:
        return None
    text = text.strip("<>")
    m = re.fullmatch(r"[PpQq](\d+)", text)
    if m:
        return text[0].upper() + m.group(1)
    m = re.fullmatch(r"wd:([PpQq]\d+)", text, flags=re.IGNORECASE)
    if m:
        return m.group(1).upper()

    try:
        parsed = urlparse(unquote(text))
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    if host != "wikidata.org":
        return None

    parts = [p for p in (parsed.path or "").split("/") if p]
    for token in reversed(parts):
        m = re.fullmatch(r"[PpQq](\d+)", token.strip())
        if m:
            return token[0].upper() + m.group(1)

    query_map = parse_qs(parsed.query or "", keep_blank_values=False)
    for key in ("title", "entity", "id", "q"):
        for raw in query_map.get(key, []):
            m = re.fullmatch(r"[PpQq](\d+)", str(raw).strip())
            if m:
                v = str(raw).strip()
                return v[0].upper() + m.group(1)

    frag = (parsed.fragment or "").strip()
    m = re.fullmatch(r"[PpQq](\d+)", frag)
    if m:
        return frag[0].upper() + m.group(1)
    return None


def canonical_wd_link_entity_uri(uri):
    uri = (uri or "").strip()
    if not uri:
        return uri
    qid = _extract_wikidata_entity_id(uri)
    if not qid:
        return uri.strip("<>")
    return f"http://www.wikidata.org/entity/{qid}"


def collect_wikidata_uris(attr_path, rel_path):
    uris = set()
    prop_uri_map = {}
    for path in (attr_path, rel_path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue
                s, p, o = parts
                s_norm = s.strip("<>")
                p_norm = p.strip("<>")
                o_norm = o.strip("<>")
                if s_norm.startswith("http://www.wikidata.org/entity/"):
                    uris.add(canonical_wd_entity_uri(s_norm))
                elif s_norm.startswith("http://www.wikidata.org/prop/"):
                    ent = prop_uri_to_entity(s_norm)
                    if ent:
                        ent = canonical_wd_entity_uri(ent)
                        prop_uri_map[s_norm] = ent
                        uris.add(ent)
                if p_norm.startswith("http://www.wikidata.org/prop/"):
                    ent = prop_uri_to_entity(p_norm)
                    if ent:
                        ent = canonical_wd_entity_uri(ent)
                        prop_uri_map[p_norm] = ent
                        uris.add(ent)
                elif p_norm.startswith("http://www.wikidata.org/entity/"):
                    uris.add(canonical_wd_entity_uri(p_norm))
                if o_norm.startswith("http://www.wikidata.org/entity/"):
                    uris.add(canonical_wd_entity_uri(o_norm))
                elif o_norm.startswith("http://www.wikidata.org/prop/"):
                    ent = prop_uri_to_entity(o_norm)
                    if ent:
                        ent = canonical_wd_entity_uri(ent)
                        prop_uri_map[o_norm] = ent
                        uris.add(ent)
    return uris, prop_uri_map


def collect_wdc_iris(attr_path, rel_path):
    uris = set()
    prop_uris = set()
    for path in (attr_path, rel_path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue
                s, p, o = parts
                s_norm = s.strip("<>")
                p_norm = p.strip("<>")
                o_norm = o.strip("<>")
                if s_norm.startswith("http://") or s_norm.startswith("https://"):
                    uris.add(s_norm)
                if p_norm.startswith("http://") or p_norm.startswith("https://"):
                    prop_uris.add(p_norm)
                if o_norm.startswith("http://") or o_norm.startswith("https://"):
                    uris.add(o_norm)
    return uris, prop_uris


def fetch_wd_labels_descriptions(uris, endpoint, language, batch_size, sleep_s, timeout, retries, backoff):
    headers = {
        "Accept": "application/sparql-results+json",
        "User-Agent": "beam-builder/1.0",
    }
    results = []
    uris = dedupe_preserve_order(list(uris))
    if not uris:
        return results

    batch_size_eff = max(1, int(batch_size or 1))
    total_batches = max(1, (len(uris) + batch_size_eff - 1) // batch_size_eff)
    progress_started_at = time.time()

    def _emit_labels_progress(done_batches):
        done_i = max(0, min(int(done_batches), total_batches))
        pct = (done_i / total_batches) * 100.0 if total_batches > 0 else 100.0
        if done_i <= 0:
            eta_txt = "ETA: N/A"
        else:
            elapsed = max(0.001, time.time() - progress_started_at)
            remaining = max(0, total_batches - done_i)
            eta_txt = _format_eta((elapsed / done_i) * remaining)
        print(
            f"[WD] labels progress: batches {done_i}/{total_batches} | {pct:5.1f}% | {eta_txt}",
            file=sys.stderr,
        )

    _emit_labels_progress(0)
    session = requests.Session()
    try:
        for batch_idx, batch in enumerate(batch_iter(uris, batch_size_eff), start=1):
            values = " ".join(f"<{uri}>" for uri in batch)
            query = (
                "SELECT ?s "
                "(SAMPLE(?labelPref) AS ?label_pref) "
                "(SAMPLE(?labelAny) AS ?label_any) "
                "(SAMPLE(?descPref) AS ?desc_pref) "
                "(SAMPLE(?descAny) AS ?desc_any) "
                "WHERE { "
                f"VALUES ?s {{ {values} }} "
                "OPTIONAL { ?s rdfs:label ?labelPref FILTER(LANG(?labelPref) = \"" + language + "\" || LANG(?labelPref) = \"\") } "
                "OPTIONAL { ?s rdfs:label ?labelAny } "
                "OPTIONAL { ?s schema:description ?descPref FILTER(LANG(?descPref) = \"" + language + "\" || LANG(?descPref) = \"\") } "
                "OPTIONAL { ?s schema:description ?descAny } "
                "} GROUP BY ?s"
            )
            attempt = 0
            while True:
                try:
                    resp = session.post(
                        endpoint,
                        data={"query": query},
                        headers=headers,
                        timeout=timeout,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    for row in data.get("results", {}).get("bindings", []):
                        s = row["s"]["value"]
                        label_val = (
                            row.get("label_pref", {}).get("value")
                            or row.get("label_any", {}).get("value")
                        )
                        desc_val = (
                            row.get("desc_pref", {}).get("value")
                            or row.get("desc_any", {}).get("value")
                        )
                        if label_val and not desc_val:
                            desc_val = label_val
                        if desc_val and not label_val:
                            label_val = desc_val
                        if label_val:
                            results.append((s, "http://www.w3.org/2000/01/rdf-schema#label", f"\"{label_val}\""))
                        if desc_val:
                            results.append((s, "http://schema.org/description", f"\"{desc_val}\""))
                    _emit_labels_progress(batch_idx)
                    break
                except requests.RequestException as exc:
                    attempt += 1
                    if attempt > retries:
                        raise
                    wait_s = backoff ** attempt
                    print(f"[WD] label retry {attempt}/{retries} in {wait_s}s: {exc}", file=sys.stderr)
                    time.sleep(wait_s)
            if sleep_s > 0:
                time.sleep(sleep_s)
        _emit_labels_progress(total_batches)
    finally:
        session.close()
    return results


def append_labels_descriptions(
    attr_path,
    rel_path,
    endpoint,
    language,
    batch_size,
    sleep_s,
    timeout,
    retries,
    backoff,
    lowercase_wd,
):
    uris, prop_uri_map = collect_wikidata_uris(attr_path, rel_path)
    if not uris and not prop_uri_map:
        return
    ent_to_prop = {}
    for prop_uri, ent_uri in prop_uri_map.items():
        ent_to_prop.setdefault(ent_uri, []).append(prop_uri)
    triples = fetch_wd_labels_descriptions(
        uris,
        endpoint,
        language,
        batch_size,
        sleep_s,
        timeout,
        retries,
        backoff,
    )
    label_pred = "http://www.w3.org/2000/01/rdf-schema#label"
    desc_pred = "http://schema.org/description"
    by_subject = {}
    for s, p, o in triples:
        row = by_subject.setdefault(s, {"label": None, "desc": None})
        if p == label_pred and row["label"] is None:
            row["label"] = o
        elif p == desc_pred and row["desc"] is None:
            row["desc"] = o

    for uri in uris:
        row = by_subject.setdefault(uri, {"label": None, "desc": None})
        fallback = f"\"{uri.rstrip('/').split('/')[-1]}\""
        if row["label"] is None:
            row["label"] = fallback
        if row["desc"] is None:
            row["desc"] = row["label"]

    with open(attr_path, "a", encoding="utf-8") as out:
        for s, row in by_subject.items():
            for p, o in ((label_pred, row["label"]), (desc_pred, row["desc"])):
                s_out, p_out, o_out = transform_triple(s, p, o, lowercase_wd)
                o_out = clean_literal(o_out)
                out.write(f"{s_out}\t{p_out}\t{o_out}\n")
                for prop_uri in ent_to_prop.get(s, []):
                    s_prop, p_prop, o_prop = transform_triple(prop_uri, p, o, lowercase_wd)
                    o_prop = clean_literal(o_prop)
                    out.write(f"{s_prop}\t{p_prop}\t{o_prop}\n")


def append_wdc_labels_descriptions(attr_path, rel_path, wdc_input_paths):
    uris, prop_uris = collect_wdc_iris(attr_path, rel_path)
    if not uris and not prop_uris:
        return
    target_iris = uris | prop_uris
    label_preds = {
        "http://www.w3.org/2000/01/rdf-schema#label",
        "http://schema.org/description",
        "http://www.w3.org/2004/02/skos/core#prefLabel",
    }
    input_paths = _iter_input_paths(wdc_input_paths)

    lock_path = os.path.join("Download", ".workers.lock")
    n_workers, _runs, _cpu = compute_shared_workers(lock_path, share=0.8)
    total_written = 0
    total_bytes = sum(os.path.getsize(p) for p in input_paths)
    done_bytes = 0
    start_ts = time.time()
    prog = _progress_line(start_ts, done_bytes, total_bytes)
    print(f"[WDC] labels progress {done_bytes}/{total_bytes} bytes {prog}", file=sys.stderr)
    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futures = [
            ex.submit(_labels_worker, (p, target_iris, label_preds))
            for p in input_paths
        ]
        pending = set(futures)
        total_futures = len(futures)
        heartbeat_s = 10.0
        last_heartbeat = time.time()
        with open(attr_path, "a", encoding="utf-8") as out:
            while pending:
                done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                if not done:
                    now = time.time()
                    if (now - last_heartbeat) >= heartbeat_s:
                        prog = _progress_line(start_ts, done_bytes, total_bytes)
                        finished = total_futures - len(pending)
                        print(
                            f"[WDC] labels progress {done_bytes}/{total_bytes} bytes {prog} | workers {finished}/{total_futures}",
                            file=sys.stderr,
                        )
                        last_heartbeat = now
                    continue
                for fut in done:
                    tmp, written, fsize = fut.result()
                    total_written += written
                    done_bytes += fsize
                    prog = _progress_line(start_ts, done_bytes, total_bytes)
                    print(f"[WDC] labels progress {done_bytes}/{total_bytes} bytes {prog}", file=sys.stderr)
                    if written > 0:
                        with open(tmp, "r", encoding="utf-8") as f:
                            for line in f:
                                out.write(line)
                    os.remove(tmp)
    done_bytes = total_bytes
    prog = _progress_line(start_ts, done_bytes, total_bytes)
    print(f"[WDC] labels progress {done_bytes}/{total_bytes} bytes {prog}", file=sys.stderr)
    if total_written == 0:
        return


def fetch_wd_label_desc_map(uris, endpoint, language, batch_size, sleep_s, timeout, retries, backoff):
    triples = fetch_wd_labels_descriptions(
        uris,
        endpoint,
        language,
        batch_size,
        sleep_s,
        timeout,
        retries,
        backoff,
    )
    labels = {}
    for s, p, o in triples:
        entry = labels.setdefault(s, {"label": "", "desc": ""})
        if p.endswith("#label"):
            entry["label"] = literal_lex(o) or o.strip('"')
        elif p.endswith("description"):
            entry["desc"] = literal_lex(o) or o.strip('"')
    # Ensure map is complete even when Wikidata has sparse metadata.
    for uri in uris:
        entry = labels.setdefault(uri, {"label": "", "desc": ""})
        fallback = uri.rstrip("/").split("/")[-1]
        if not entry["label"]:
            entry["label"] = fallback
        if not entry["desc"]:
            entry["desc"] = entry["label"]
    return labels


def write_prop_stats(
    out_path,
    attr_path,
    rel_path,
    endpoint,
    language,
    batch_size,
    sleep_s,
    timeout,
    retries,
    backoff,
):
    counts = count_props_in_files([attr_path, rel_path])
    prop_entity_map = {}
    for prop in counts.keys():
        prop_norm = prop.strip("<>")
        if prop_norm.startswith("http://www.wikidata.org/prop/"):
            ent = prop_uri_to_entity(prop_norm)
            if ent:
                prop_entity_map[prop] = canonical_wd_entity_uri(ent)

    label_map = {}
    if prop_entity_map:
        label_map = fetch_wd_label_desc_map(
            set(prop_entity_map.values()),
            endpoint,
            language,
            batch_size,
            sleep_s,
            timeout,
            retries,
            backoff,
        )

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out:
        out.write("predicate\tcount\tlabel\tdescription\n")
        for prop, count in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            label = ""
            desc = ""
            ent = prop_entity_map.get(prop)
            if ent and ent in label_map:
                label = label_map[ent].get("label", "")
                desc = label_map[ent].get("desc", "")
            out.write(f"{prop}\t{count}\t{label}\t{desc}\n")


def write_prop_stats_simple(out_path, attr_path, rel_path):
    counts = count_props_in_files([attr_path, rel_path])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out:
        out.write("predicate\tcount\tlabel\tdescription\n")
        for prop, count in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            label = prop.rstrip("/").split("/")[-1]
            out.write(f"{prop}\t{count}\t{label}\t\n")


def write_prop_stats_wdc(out_path, attr_path, rel_path, wdc_input_paths):
    counts = count_props_in_files([attr_path, rel_path])
    label_preds = {
        "http://www.w3.org/2000/01/rdf-schema#label",
        "http://schema.org/description",
        "http://www.w3.org/2004/02/skos/core#prefLabel",
    }
    labels = {}
    descs = {}
    targets = {p.strip("<>") for p in counts.keys()}

    lock_path = os.path.join("Download", ".workers.lock")
    n_workers, _runs, _cpu = compute_shared_workers(lock_path, share=0.8)
    total_bytes = sum(os.path.getsize(p) for p in _iter_input_paths(wdc_input_paths))
    done_bytes = 0
    start_ts = time.time()
    prog = _progress_line(start_ts, done_bytes, total_bytes)
    print(f"[WDC] prop_stats progress {done_bytes}/{total_bytes} bytes {prog}", file=sys.stderr)
    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futures = [
            ex.submit(_prop_label_worker, (p, targets, label_preds))
            for p in _iter_input_paths(wdc_input_paths)
        ]
        pending = set(futures)
        total_futures = len(futures)
        heartbeat_s = 10.0
        last_heartbeat = time.time()
        while pending:
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            if not done:
                now = time.time()
                if (now - last_heartbeat) >= heartbeat_s:
                    prog = _progress_line(start_ts, done_bytes, total_bytes)
                    finished = total_futures - len(pending)
                    print(
                        f"[WDC] prop_stats progress {done_bytes}/{total_bytes} bytes {prog} | workers {finished}/{total_futures}",
                        file=sys.stderr,
                    )
                    last_heartbeat = now
                continue
            for fut in done:
                local_labels, local_descs, fsize = fut.result()
                labels.update(local_labels)
                descs.update(local_descs)
                done_bytes += fsize
                prog = _progress_line(start_ts, done_bytes, total_bytes)
                print(f"[WDC] prop_stats progress {done_bytes}/{total_bytes} bytes {prog}", file=sys.stderr)
    done_bytes = total_bytes
    prog = _progress_line(start_ts, done_bytes, total_bytes)
    print(f"[WDC] prop_stats progress {done_bytes}/{total_bytes} bytes {prog}", file=sys.stderr)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out:
        out.write("predicate\tcount\tlabel\tdescription\n")
        for prop, count in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            label = labels.get(prop, "")
            desc = descs.get(prop, "")
            out.write(f"{prop}\t{count}\t{label}\t{desc}\n")


def run_pipeline(
    args,
    wdc_entities,
    wd_entities_raw,
    wdc_values,
    wd_values,
    out_dir,
    wdc_mask_values,
    wd_mask_values,
    wdc_exclude_props,
    wdc_exclude_prop_patterns,
    wd_exclude_props,
    replace_map,
    lowercase_wd,
    add_wd_labels,
    wd_raw_cache_path=None,
):
    out_attr_1 = os.path.join(out_dir, "attr_triples_1")
    out_rel_1 = os.path.join(out_dir, "rel_triples_1")
    out_attr_2 = os.path.join(out_dir, "attr_triples_2")
    out_rel_2 = os.path.join(out_dir, "rel_triples_2")
    out_links = os.path.join(out_dir, "ent_links")
    out_prop_stats_wdc = os.path.join(out_dir, "prop_stats_wdc.tsv")
    out_prop_stats_wd = os.path.join(out_dir, "prop_stats_wd.tsv")

    wd_entities_out = [
        canonical_wd_link_entity_uri(
            normalize_wd_uri(replace_map.get(uri, uri), lowercase_wd)
        )
        for uri in wd_entities_raw
    ]
    write_links(out_links, wdc_entities, wd_entities_out, args.dedupe_links)

    split_triples(
        args.wdc_nq,
        out_attr_1,
        out_rel_1,
        seed_subjects=wdc_entities,
        max_depth=args.max_depth,
        mask_values=wdc_mask_values,
        exclude_props=wdc_exclude_props,
        exclude_prop_patterns=wdc_exclude_prop_patterns,
        progress_every=args.progress_every,
        follow_iri_objects=True,
    )
    # Add labels/descriptions for WDC IRIs and properties found in WDC triples
    append_wdc_labels_descriptions(out_attr_1, out_rel_1, args.wdc_nq)

    if args.wd_nq:
        wd_attr_tmp = out_attr_2
        wd_rel_tmp = out_rel_2
        if args.wd_prop_min_count > 0:
            wd_attr_tmp = out_attr_2 + ".tmp"
            wd_rel_tmp = out_rel_2 + ".tmp"
        split_triples(
            args.wd_nq,
            wd_attr_tmp,
            wd_rel_tmp,
            seed_subjects=wd_entities_raw,
            max_depth=args.max_depth,
            lowercase_wd=lowercase_wd,
            mask_values=wd_mask_values,
            exclude_props=wd_exclude_props,
            replace_map=replace_map,
        )
        if args.wd_prop_min_count > 0:
            filter_triples_by_prop_count(
                wd_attr_tmp,
                wd_rel_tmp,
                out_attr_2,
                out_rel_2,
                args.wd_prop_min_count,
                exclude_props=None,
            )
        if add_wd_labels:
            append_labels_descriptions(
                out_attr_2,
                out_rel_2,
                args.sparql_url,
                args.lang,
                args.batch_size,
                args.sleep,
                args.timeout,
                args.retries,
                args.backoff,
                lowercase_wd,
            )
    else:
        wd_attr_tmp = out_attr_2
        wd_rel_tmp = out_rel_2
        if args.wd_prop_min_count > 0:
            wd_attr_tmp = out_attr_2 + ".tmp"
            wd_rel_tmp = out_rel_2 + ".tmp"
        write_wikidata_from_sparql(
            args.sparql_url,
            wd_entities_raw,
            wd_attr_tmp,
            wd_rel_tmp,
            lowercase_wd=lowercase_wd,
            language=args.lang,
            batch_size=args.batch_size,
            sleep_s=args.sleep,
            timeout=args.timeout,
            retries=args.retries,
            backoff=args.backoff,
            mask_values=wd_mask_values,
            exclude_props=wd_exclude_props,
            replace_map=replace_map,
            state_path=args.state_file or os.path.join(out_dir, ".wd_state.json"),
            resume=args.resume,
            raw_triples_cache_path=wd_raw_cache_path,
        )
        if args.wd_prop_min_count > 0:
            filter_triples_by_prop_count(
                wd_attr_tmp,
                wd_rel_tmp,
                out_attr_2,
                out_rel_2,
                args.wd_prop_min_count,
                exclude_props=None,
            )
        if add_wd_labels:
            append_labels_descriptions(
                out_attr_2,
                out_rel_2,
                args.sparql_url,
                args.lang,
                args.batch_size,
                args.sleep,
                args.timeout,
                args.retries,
                args.backoff,
                lowercase_wd,
            )
    write_prop_stats_wdc(out_prop_stats_wdc, out_attr_1, out_rel_1, args.wdc_nq)
    write_prop_stats(
        out_prop_stats_wd,
        out_attr_2,
        out_rel_2,
        args.sparql_url,
        args.lang,
        args.batch_size,
        args.sleep,
        args.timeout,
        args.retries,
        args.backoff,
    )
def sparql_construct(
    endpoint,
    subjects,
    language,
    batch_size,
    sleep_s,
    timeout,
    retries,
    backoff,
    start_batch,
    session=None,
):
    headers = {
        "Accept": "application/n-triples",
        "User-Agent": "beam-builder/1.0",
    }
    close_session = False
    if session is None:
        session = requests.Session()
        close_session = True
    try:
        for batch_idx, batch in enumerate(batch_iter(subjects, batch_size), start=1):
            if batch_idx < start_batch:
                continue
            values = " ".join(f"<{uri}>" for uri in batch)
            query = (
                "CONSTRUCT { ?s ?p ?o . } WHERE { "
                f"VALUES ?s {{ {values} }} "
                "?s ?p ?o . "
                "FILTER(!isLiteral(?o) || lang(?o) = \"\" "
                f"|| langMatches(lang(?o), \"{language}\")) "
                "}"
            )
            print(f"[WD] batch {batch_idx} size={len(batch)}", file=sys.stderr)
            attempt = 0
            while True:
                try:
                    resp = session.post(
                        endpoint,
                        data={"query": query},
                        headers=headers,
                        timeout=timeout,
                    )
                    resp.raise_for_status()
                    for line in resp.text.splitlines():
                        parsed = parse_nq_or_nt(line)
                        if parsed:
                            yield batch_idx, parsed
                    yield batch_idx, None
                    break
                except requests.RequestException as exc:
                    attempt += 1
                    if attempt > retries:
                        raise
                    wait_s = backoff ** attempt
                    print(f"[WD] retry {attempt}/{retries} in {wait_s}s: {exc}", file=sys.stderr)
                    time.sleep(wait_s)
            if sleep_s > 0:
                time.sleep(sleep_s)
    finally:
        if close_session:
            session.close()


def write_wikidata_from_sparql(
    endpoint,
    subjects,
    out_attr_path,
    out_rel_path,
    lowercase_wd,
    language,
    batch_size,
    sleep_s,
    timeout,
    retries,
    backoff,
    mask_values=None,
    exclude_props=None,
    replace_map=None,
    state_path=None,
    resume=False,
    raw_triples_cache_path=None,
):
    os.makedirs(os.path.dirname(out_attr_path), exist_ok=True)
    os.makedirs(os.path.dirname(out_rel_path), exist_ok=True)
    subjects_in = [s for s in subjects if s]
    total_subjects_in = len(subjects_in)
    subjects = dedupe_preserve_order(subjects_in)
    total_subjects_unique = len(subjects)
    if not subjects:
        Path(out_attr_path).write_text("", encoding="utf-8")
        Path(out_rel_path).write_text("", encoding="utf-8")
        return

    if total_subjects_unique < total_subjects_in:
        print(
            f"[WD] dedup subjects {total_subjects_unique}/{total_subjects_in}",
            file=sys.stderr,
        )

    batch_size_eff = max(1, int(batch_size or 1))
    total_batches = max(1, (total_subjects_unique + batch_size_eff - 1) // batch_size_eff)
    progress_started_at = time.time()

    start_batch = 1
    state = {
        "done_batch": 0,
        "batch_size": batch_size,
        "lang": language,
        "endpoint": endpoint,
        "total_subjects": total_subjects_unique,
    }
    if resume and state_path and os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            prev = json.load(f)
        start_batch = int(prev.get("done_batch", 0)) + 1
        print(f"[WD] resuming from batch {start_batch}", file=sys.stderr)

    done_batches = max(0, start_batch - 1)

    def _emit_batch_progress(done):
        done_i = max(0, min(int(done), total_batches))
        pct = (done_i / total_batches) * 100.0 if total_batches > 0 else 100.0
        if done_i <= 0:
            eta_txt = "ETA: N/A"
        else:
            elapsed = max(0.001, time.time() - progress_started_at)
            remaining_batches = max(0, total_batches - done_i)
            eta_txt = _format_eta((elapsed / done_i) * remaining_batches)
        print(
            f"[WD] Progress: batches {done_i}/{total_batches} | {pct:5.1f}% | {eta_txt}",
            file=sys.stderr,
        )

    _emit_batch_progress(done_batches)

    attr_mode = "a" if resume else "w"
    rel_mode = "a" if resume else "w"
    exclude_props_norm = {_normalize_prop_token(p) for p in exclude_props} if exclude_props else None
    load_from_cache = bool(raw_triples_cache_path and os.path.exists(raw_triples_cache_path))
    cache_tmp_path = None
    cache_writer = None
    cache_completed = False
    if raw_triples_cache_path and not load_from_cache:
        cache_tmp_path = f"{raw_triples_cache_path}.tmp.{os.getpid()}"
        cache_dir = os.path.dirname(raw_triples_cache_path)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        cache_writer = open(cache_tmp_path, "w", encoding="utf-8")

    def _write_processed_triple(attr_out, rel_out, triple, counters):
        kept_attr, kept_rel = counters
        s, p, o = triple
        p_norm = _normalize_prop_token(p)
        if exclude_props_norm and p_norm in exclude_props_norm:
            return kept_attr, kept_rel
        if replace_map:
            s_key = s.strip("<>")
            if s_key in replace_map:
                s = replace_map[s_key]
        if replace_map and (not o.startswith('"')):
            o_key = o.strip("<>")
            if o_key in replace_map:
                o = replace_map[o_key]
        s_out, p_out, o_out = transform_triple(s, p, o, lowercase_wd)
        if o.startswith('"'):
            o_out = clean_literal(o_out)
            if mask_values:
                lex = literal_lex(o_out)
                if lex in mask_values:
                    return kept_attr, kept_rel
            attr_out.write(f"{s_out}\t{p_out}\t{o_out}\n")
            kept_attr += 1
        else:
            rel_out.write(f"{s_out}\t{p_out}\t{o_out}\n")
            kept_rel += 1
        return kept_attr, kept_rel

    session = requests.Session()
    with open(out_attr_path, attr_mode, encoding="utf-8") as attr_out, \
         open(out_rel_path, rel_mode, encoding="utf-8") as rel_out:
        kept_attr = 0
        kept_rel = 0
        try:
            if load_from_cache:
                print(f"[WD] using cached triples: {raw_triples_cache_path}", file=sys.stderr)
                for triple in _read_raw_wd_triples(raw_triples_cache_path):
                    kept_attr, kept_rel = _write_processed_triple(
                        attr_out, rel_out, triple, (kept_attr, kept_rel)
                    )
                done_batches = total_batches
                _emit_batch_progress(done_batches)
                cache_completed = True
            else:
                for batch_idx, item in sparql_construct(
                    endpoint,
                    subjects,
                    language,
                    batch_size,
                    sleep_s,
                    timeout,
                    retries,
                    backoff,
                    start_batch,
                    session=session,
                ):
                    if item is None:
                        done_batches = max(done_batches, batch_idx)
                        _emit_batch_progress(done_batches)
                        if state_path:
                            state["done_batch"] = batch_idx
                            with open(state_path, "w", encoding="utf-8") as f:
                                json.dump(state, f, indent=2)
                        continue
                    if cache_writer:
                        cache_writer.write(f"{item[0]}\t{item[1]}\t{item[2]}\n")
                    kept_attr, kept_rel = _write_processed_triple(
                        attr_out, rel_out, item, (kept_attr, kept_rel)
                    )
                done_batches = total_batches
                _emit_batch_progress(done_batches)
                cache_completed = True
        finally:
            session.close()
            if cache_writer:
                cache_writer.close()
                try:
                    if cache_completed:
                        os.replace(cache_tmp_path, raw_triples_cache_path)
                    elif cache_tmp_path and os.path.exists(cache_tmp_path):
                        os.remove(cache_tmp_path)
                except Exception:
                    pass
        print(f"[WD] done attr={kept_attr} rel={kept_rel}", file=sys.stderr)


def main():
    start_ts = time.time()
    parser = argparse.ArgumentParser(
        description="Generate BEAM-style files from N-Quads/N-Triples and a link TSV."
    )
    parser.add_argument("class_name", help="Class name to use default paths (data/<class> and Download/<class>).")
    parser.add_argument("--wd-link-prop-id", action="append", default=[], help="Wikidata property id to drop (e.g., P1243).")
    parser.add_argument("--wdc-link-prop-name", action="append", default=[], help="Pattern to drop WDC predicates (e.g., isrc).")
    parser.add_argument("--max-depth", type=int, default=-1, help="Depth for following bnodes (default: -1 until no new bnodes).")
    parser.add_argument("--progress-every", type=int, default=10000000, help="Print progress every N lines (WDC scan).")

    args = parser.parse_args()

    # Defaults from class_name
    class_name = args.class_name
    data_dir = os.path.join("data", class_name)
    download_dir = os.path.join("Download", class_name)

    # Defaults
    candidates = []
    if os.path.isdir(download_dir):
        for name in sorted(os.listdir(download_dir)):
            if name.startswith("part_") and (
                name.endswith(".nq") or name.endswith(".nt") or "." not in name
            ):
                candidates.append(os.path.join(download_dir, name))
        if not candidates:
            for name in sorted(os.listdir(download_dir)):
                if name.endswith("_full_graph.nq"):
                    candidates.append(os.path.join(download_dir, name))
                    break
        if not candidates:
            for name in sorted(os.listdir(download_dir)):
                if name.endswith(".nq") or name.endswith(".nt"):
                    candidates.append(os.path.join(download_dir, name))
                    break

    args.wdc_nq = candidates
    args.links_tsv = os.path.join(download_dir, "wdc_wikidata_links.tsv")
    base_out_dir = os.path.join(data_dir, "beam")
    out_dir = base_out_dir
    suffix = 1
    while os.path.exists(out_dir):
        out_dir = base_out_dir + str(suffix)
        suffix += 1
    args.out_dir = out_dir

    # Fixed defaults (removed flags)
    args.wd_nq = None
    args.sep = "\t"
    args.wdc_col = 0
    args.wd_col = 1
    args.wdc_value_col = None
    args.wd_value_col = None
    args.dedupe_links = False
    args.keep_link_values = False
    args.wdc_min_triples = 0
    args.wdc_exclude_prop = []
    args.wd_exclude_prop = []
    args.no_wd_labels = False
    args.wd_prop_min_count = 0
    args.merge_wd_by_link_values = False
    args.sparql_url = "https://query.wikidata.org/sparql"
    args.lang = "en"
    args.batch_size = 50
    args.sleep = 1.0
    args.timeout = 60
    args.retries = 3
    args.backoff = 2.0
    args.no_lowercase_wd = False
    args.resume = False
    args.state_file = None

    if not args.wdc_nq:
        print(f"[ERR] No WDC files found in {download_dir}", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(args.links_tsv):
        print(f"[ERR] Missing links TSV: {args.links_tsv}", file=sys.stderr)
        sys.exit(1)

    wdc_entities, wd_entities_raw, wdc_values, wd_values = read_links(
        args.links_tsv,
        args.sep,
        args.wdc_col,
        args.wd_col,
        args.wdc_value_col,
        args.wd_value_col,
    )
    print(
        f"[Links] wdc={len(wdc_entities)} wd={len(wd_entities_raw)} "
        f"wdc_values={len(wdc_values)} wd_values={len(wd_values)}",
        file=sys.stderr,
    )
    lowercase_wd = not args.no_lowercase_wd
    wdc_mask_values = set(v for v in wdc_values if v)
    wd_mask_values = set(v for v in wd_values if v)
    if args.keep_link_values:
        wdc_mask_values = None
        wd_mask_values = None

    wdc_exclude_props = set(p for p in args.wdc_exclude_prop if p)
    wd_exclude_props = set(p for p in args.wd_exclude_prop if p)
    wd_link_prop_uris = set()
    for prop_id in args.wd_link_prop_id:
        norm = normalize_wd_prop_id(prop_id) if prop_id else None
        if norm:
            wd_link_prop_uris.update(wikidata_prop_uris(norm))
    wdc_link_prop_patterns = {p.lower() for p in args.wdc_link_prop_name if p}

    if args.wdc_min_triples > 0:
        counts = count_wdc_triples(
            args.wdc_nq,
            set(wdc_entities),
            exclude_props=wdc_exclude_props,
            exclude_prop_patterns=wdc_link_prop_patterns,
            mask_values=wdc_mask_values,
        )
        allowed_wdc = {s for s, c in counts.items() if c >= args.wdc_min_triples}
        wdc_entities, wd_entities_raw, wdc_values, wd_values = filter_links_by_wdc(
            wdc_entities, wd_entities_raw, wdc_values, wd_values, allowed_wdc
        )
        print(f"[Links] kept wdc after min_triples={len(wdc_entities)}", file=sys.stderr)

    replace_map = {}
    if args.merge_wd_by_link_values and wd_values:
        replace_map = build_wd_merge_map(wd_entities_raw, wd_values)
        if replace_map:
            print(f"[WD] merge map size={len(replace_map)}", file=sys.stderr)

    add_wd_labels = True

    out_without = os.path.join(args.out_dir, "without_link_code")
    out_with = os.path.join(args.out_dir, "with_link_code")

    run_pipeline(
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
    )
    run_pipeline(
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
    )
    elapsed = time.time() - start_ts
    took = _format_eta(elapsed).replace("ETA: ", "")
    print(f"[DONE] total time: {took}", file=sys.stderr)
    try:
        with open(os.path.join(out_dir, "stats.txt"), "a", encoding="utf-8") as f:
            f.write(f"build_beam took {took}\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
