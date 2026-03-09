#!/usr/bin/env python3
"""
WDC Entity Linker - Download, Filter & Link to Wikidata

Usage:
    python app.py MusicRecording "isrc" "all" "wdt:P1243"
    python app.py Organization "vat" "0-2" "wdt:P1648"
"""

import sys
import argparse
import os
import re
import gzip
import hashlib
import shutil
import json
import requests
import unicodedata
import random
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
import time
import fcntl
from pathlib import Path
from collections import defaultdict
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from bs4 import BeautifulSoup

# Configuration
WDC_BASE_URL = "https://data.dws.informatik.uni-mannheim.de/structureddata/2024-12/quads/classspecific/"
WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
TARGET_ENDPOINTS = {
    "wikidata": {
        "label": "Wikidata",
        "sparql_url": WIKIDATA_ENDPOINT,
        "supports_qid": True,
    },
    "dbpedia": {
        "label": "DBpedia",
        "sparql_url": "https://dbpedia.org/sparql",
        "supports_qid": False,
    },
    "yago": {
        "label": "YAGO",
        "sparql_url": "https://yago-knowledge.org/sparql/query",
        "supports_qid": False,
    },
    "custom": {
        "label": "Custom",
        "sparql_url": "",
        "supports_qid": False,
    },
}
_PREFIX_DECL_RE = re.compile(r"^PREFIX\s+([A-Za-z][A-Za-z0-9_-]*)\s*:\s*<([^>\s]+)>\s*$", re.IGNORECASE)
_CPU_COUNT = max(1, os.cpu_count() or 1)
MAX_PARALLEL_WORKERS = int(os.environ.get("ALIGN_MAX_WORKERS", str(_CPU_COUNT)))
ALIGN_CPU_SHARE = float(os.environ.get("ALIGN_CPU_SHARE", "0.95"))

# Colors
class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    CYAN = '\033[0;36m'
    RESET = '\033[0m'

def print_color(text, color):
    print(f"{color}{text}{Colors.RESET}")

_EXTRA_STRIP_CHARS = set()
_NORMALIZATION_ENABLED = True
_CANCEL_CHECK = None

def set_extra_strip_chars(strip_chars):
    global _EXTRA_STRIP_CHARS
    _EXTRA_STRIP_CHARS = set(strip_chars or [])
    return _EXTRA_STRIP_CHARS

def set_normalization(enabled: bool):
    global _NORMALIZATION_ENABLED
    _NORMALIZATION_ENABLED = bool(enabled)


def set_cancel_checker(fn):
    global _CANCEL_CHECK
    _CANCEL_CHECK = fn

def parse_strip_list(spec):
    if not spec:
        return []
    parts = [p for p in spec.split(";") if p != ""]
    chars = []
    named_chars = {
        "dot": ".",
        "period": ".",
        "hyphen": "-",
        "dash": "-",
        "semicolon": ";",
        "semi": ";",
        "comma": ",",
        "slash": "/",
        "underscore": "_",
        "colon": ":",
    }
    for p in parts:
        p = p.strip()
        if not p:
            continue
        p_lower = p.lower()
        if p_lower == "spaces":
            chars.extend([" ", "\t", "\n", "\r"])
            continue
        if p_lower == "special-chars":
            # Placeholder token handled in normalize_for_matching
            chars.append("__SPECIAL_CHARS__")
            continue
        if p_lower in named_chars:
            chars.append(named_chars[p_lower])
            continue
        # unescape common sequences
        p = p.replace("\\;", ";").replace("\\/", "/").replace("\\\\", "\\")
        p = p.replace("\\t", "\t").replace("\\n", "\n").replace("\\r", "\r")
        chars.append(p)
    return chars

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

def _eta_update(start_ts, done_bytes, total_bytes):
    if done_bytes <= 0 or total_bytes <= 0:
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

def _truncate_sample(text, max_len=120):
    text = text.strip()
    return text if len(text) <= max_len else text[: max_len - 3] + "..."

def _literal_lex(value):
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

def _extract_object_token(line):
    # Extract object (literal/IRI/blank node) token from N-Quads/N-Triples
    m = re.match(r'^\s*\S+\s+<[^>]+>\s+(".*?"(?:\^\^<[^>]+>|@[a-zA-Z-]+)?|<[^>]+>|_:[^\s]+)', line)
    if not m:
        return None
    return m.group(1)

def _extract_spo_tokens(line):
    # Extract subject, predicate, object tokens from N-Quads/N-Triples
    m = re.match(r'^\s*(\S+)\s+(<[^>]+>)\s+(".*?"(?:\^\^<[^>]+>|@[a-zA-Z-]+)?|<[^>]+>|_:[^\s]+)', line)
    if not m:
        return None, None, None
    return m.group(1), m.group(2), m.group(3)


def default_type_filter_iris_for_class(class_name):
    class_name = str(class_name or "").strip()
    if not class_name:
        return []
    return [
        f"<http://schema.org/{class_name}>",
        f"<https://schema.org/{class_name}>",
    ]


def collect_allowed_subjects_by_type(files, type_filter_iris=None, progress_every=100):
    if not type_filter_iris:
        return None
    files = [Path(p) for p in files]
    type_pred = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
    type_set = set(type_filter_iris)
    allowed_subjects = set()
    total_bytes = sum(Path(p).stat().st_size for p in files)
    done_bytes = 0
    start_ts = time.time()

    print_color("\n🔎 Filtrage des sujets par rdf:type...", Colors.BLUE)
    for file_path in files:
        print(f"\n  📄 Type scan: {file_path.name}")
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                done_bytes += len(line)
                s, p, o = _extract_spo_tokens(line)
                if not s:
                    continue
                if p == type_pred and o in type_set:
                    allowed_subjects.add(s)
                if progress_every and done_bytes % (progress_every * 50) == 0:
                    prog = _progress_line(start_ts, done_bytes, total_bytes)
                    print(f"\r  Sujets retenus: {len(allowed_subjects):,} | {prog}", end="", flush=True)

    print(f"\n  ✅ Sujets retenus: {len(allowed_subjects):,}")
    return allowed_subjects

def _update_reservoir(samples_map, counts_map, key, sample, k=5):
    count = counts_map.get(key, 0) + 1
    counts_map[key] = count
    bucket = samples_map.setdefault(key, [])
    if len(bucket) < k:
        bucket.append(sample)
    else:
        # Reservoir sampling
        j = random.randint(1, count)
        if j <= k:
            bucket[j - 1] = sample

def print_top_props(predicates_found, top_n=10, title=None, output_file=None, samples_map=None, min_count=1, write_samples=False, fallback_map=None):
    if not predicates_found:
        return
    lines = []
    if title:
        lines.append(title)
    for pred, count in sorted(predicates_found.items(), key=lambda x: -x[1])[:top_n]:
        if count < min_count:
            continue
        lines.append(f"   {count:>8} × {pred}")
    for line in lines:
        print(line)
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
            if write_samples and samples_map:
                for pred, count in sorted(predicates_found.items(), key=lambda x: -x[1])[:top_n]:
                    if count < min_count:
                        continue
                    samples = list(samples_map.get(pred, []))
                    if fallback_map is not None and len(samples) < 5:
                        needed = 5 - len(samples)
                        samples.extend(fallback_map.get(pred, [])[:needed])
                    samples = ", ".join(_truncate_sample(s) for s in samples)
                    f.write(f"   {count:>8} × {pred}; {samples}\n")

def _count_predicates_batch(lines):
    predicates_found = defaultdict(int)
    line_count = 0
    for line in lines:
        line_count += 1
        predicates = re.findall(r'<([^>]+)>', line)
        if len(predicates) >= 1:
            predicate = predicates[0]
            predicates_found[predicate] += 1
    return predicates_found, line_count

def scan_top_props_from_files(files, top_n=1000, parallel=True, workers=None, batch_size=500000, lock_path=None, progress_every=100, output_file=None, type_filter_iris=None):
    print_color(f"\n📊 Scan top-props (sans filtrage)...", Colors.BLUE)
    predicates_found = defaultdict(int)
    samples_map = {}
    fallback_map = {}
    sample_counts = {}
    fallback_counts = {}
    iri_labels = {}
    iri_literals = {}
    iri_literals_is_id = {}
    total_lines = 0
    allowed_subjects = None
    files = [Path(p) for p in files]
    total_bytes = sum(p.stat().st_size for p in files)
    done_bytes = 0
    start_ts = time.time()

    if type_filter_iris:
        type_pred = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
        type_set = set(type_filter_iris)
        allowed_subjects = set()
        print_color(f"\n🔎 Filtrage des sujets par rdf:type...", Colors.BLUE)
        bytes_read = 0
        for file_path in files:
            print(f"\n  📄 Type scan: {file_path.name}")
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    bytes_read += len(line)
                    s, p, o = _extract_spo_tokens(line)
                    if not s:
                        continue
                    if p == type_pred and o in type_set:
                        allowed_subjects.add(s)
                    if progress_every and bytes_read % (progress_every * 50) == 0:
                        done_bytes = bytes_read
                        prog = _progress_line(start_ts, done_bytes, total_bytes)
                        print(f"\r  Sujets retenus: {len(allowed_subjects):,} | {prog}", end='', flush=True)
        print(f"\n  ✅ Sujets retenus: {len(allowed_subjects):,}")

    if parallel:
        buffer = []
        window_batches = []
        n_workers = workers or 1
        lines_since_workers_update = 0
        for file_path in files:
            file_base = done_bytes
            bytes_read = 0
            print(f"\n  📄 Scan: {file_path.name}")
            if progress_every:
                prog = _progress_line(start_ts, done_bytes, total_bytes)
                print(f"  ⏳ Progress: Lignes lues: {total_lines:,} | {prog}", flush=True)
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    bytes_read += len(line)
                    total_lines += 1
                    if allowed_subjects is not None:
                        subj, _, _ = _extract_spo_tokens(line)
                        if not subj or subj not in allowed_subjects:
                            continue
                    lines_since_workers_update += 1
                    buffer.append(line)
                    # Échantillonnage des exemples (main thread)
                    predicates = re.findall(r'<([^>]+)>', line)
                    if len(predicates) >= 1:
                        predicate = predicates[0]
                        obj_tok = _extract_object_token(line)
                        if obj_tok is not None:
                            # Cache labels/descriptions for IRIs to improve samples
                            if predicate in (
                                "http://www.w3.org/2000/01/rdf-schema#label",
                                "http://www.w3.org/2004/02/skos/core#prefLabel",
                                "http://schema.org/name",
                            ):
                                subj = line.split(None, 1)[0]
                                lex = _literal_lex(obj_tok)
                                if subj and lex:
                                    iri_labels[subj] = lex
                            if obj_tok.startswith('"'):
                                lex = _literal_lex(obj_tok)
                                if lex:
                                    # cache literal values for IRI subjects (prefer identifier-like predicates)
                                    subj = line.split(None, 1)[0]
                                    if subj and subj.startswith("<http"):
                                        pred_l = predicate.lower()
                                        is_id = any(tag in pred_l for tag in ("identifier", "isrc", "isbn", "issn", "imdb", "viaf", "gnd", "id"))
                                        if predicate != "http://schema.org/description":
                                            prev_is_id = iri_literals_is_id.get(subj, False)
                                            if is_id or not subj in iri_literals or not prev_is_id:
                                                iri_literals[subj] = lex
                                                iri_literals_is_id[subj] = is_id
                                    _update_reservoir(samples_map, sample_counts, predicate, lex)
                            else:
                                # IRI or blank node: try to resolve to label
                                if not obj_tok.startswith("_:"):
                                    resolved = None
                                    if obj_tok in iri_literals and iri_literals_is_id.get(obj_tok, False):
                                        resolved = iri_literals.get(obj_tok)
                                    elif obj_tok in iri_labels:
                                        resolved = iri_labels.get(obj_tok)
                                    elif obj_tok in iri_literals:
                                        resolved = iri_literals.get(obj_tok)
                                    if resolved:
                                        _update_reservoir(samples_map, sample_counts, predicate, resolved)
                                    else:
                                        tail = obj_tok.strip("<>").rstrip("/").split("/")[-1]
                                        _update_reservoir(fallback_map, fallback_counts, predicate, tail)
                    if len(buffer) >= batch_size:
                        window_batches.append(buffer)
                        buffer = []

                    if lines_since_workers_update >= 10000:
                        if lock_path:
                            n_workers, _runs, _cpu = get_shared_workers(
                                lock_path, share=ALIGN_CPU_SHARE, override=workers
                            )
                        else:
                            n_workers = workers or 1
                        lines_since_workers_update = 0

                    window_size = max(1, n_workers * 6)
                    if progress_every and total_lines % progress_every == 0:
                        done_bytes = file_base + bytes_read
                        prog = _progress_line(start_ts, done_bytes, total_bytes)
                        print(f"\r  Lignes lues: {total_lines:,} | {prog}", end='', flush=True)

                    if len(window_batches) >= window_size:
                        with ProcessPoolExecutor(max_workers=n_workers) as ex:
                            futures = [ex.submit(_count_predicates_batch, b) for b in window_batches]
                            for fut in as_completed(futures):
                                preds, lines = fut.result()
                                for pred, cnt in preds.items():
                                    predicates_found[pred] += cnt
                        window_batches = []
            print_top_props(
                predicates_found,
                top_n=top_n,
                title=f"\n  📋 Top {top_n} prédicats (après {file_path.name}):",
                output_file=None,
                samples_map=None,
                min_count=1000,
            )
            done_bytes = file_base + file_path.stat().st_size

        if buffer:
            window_batches.append(buffer)
        if window_batches:
            if lock_path:
                n_workers, _runs, _cpu = get_shared_workers(
                    lock_path, share=ALIGN_CPU_SHARE, override=workers
                )
            else:
                n_workers = workers or 1
            with ProcessPoolExecutor(max_workers=n_workers) as ex:
                futures = [ex.submit(_count_predicates_batch, b) for b in window_batches]
                for fut in as_completed(futures):
                    preds, lines = fut.result()
                    for pred, cnt in preds.items():
                        predicates_found[pred] += cnt
        if progress_every:
            done_bytes = total_bytes
            prog = _progress_line(start_ts, done_bytes, total_bytes)
            print(f"\r  Lignes lues: {total_lines:,} | {prog}", end='', flush=True)
    else:
        for file_path in files:
            file_base = done_bytes
            bytes_read = 0
            print(f"\n  📄 Scan: {file_path.name}")
            if progress_every:
                prog = _progress_line(start_ts, done_bytes, total_bytes)
                print(f"  ⏳ Progress: Lignes lues: {total_lines:,} | {prog}", flush=True)
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    bytes_read += len(line)
                    total_lines += 1
                    if allowed_subjects is not None:
                        subj, _, _ = _extract_spo_tokens(line)
                        if not subj or subj not in allowed_subjects:
                            continue
                    predicates = re.findall(r'<([^>]+)>', line)
                    if len(predicates) >= 1:
                        predicate = predicates[0]
                        predicates_found[predicate] += 1
                        obj_tok = _extract_object_token(line)
                        if obj_tok is not None:
                            if predicate in (
                                "http://www.w3.org/2000/01/rdf-schema#label",
                                "http://www.w3.org/2004/02/skos/core#prefLabel",
                                "http://schema.org/name",
                            ):
                                subj = line.split(None, 1)[0]
                                lex = _literal_lex(obj_tok)
                                if subj and lex:
                                    iri_labels[subj] = lex
                            if obj_tok.startswith('"'):
                                lex = _literal_lex(obj_tok)
                                if lex:
                                    subj = line.split(None, 1)[0]
                                    if subj and subj.startswith("<http"):
                                        pred_l = predicate.lower()
                                        is_id = any(tag in pred_l for tag in ("identifier", "isrc", "isbn", "issn", "imdb", "viaf", "gnd", "id"))
                                        if predicate != "http://schema.org/description":
                                            prev_is_id = iri_literals_is_id.get(subj, False)
                                            if is_id or not subj in iri_literals or not prev_is_id:
                                                iri_literals[subj] = lex
                                                iri_literals_is_id[subj] = is_id
                                    _update_reservoir(samples_map, sample_counts, predicate, lex)
                            else:
                                if not obj_tok.startswith("_:"):
                                    resolved = None
                                    if obj_tok in iri_literals and iri_literals_is_id.get(obj_tok, False):
                                        resolved = iri_literals.get(obj_tok)
                                    elif obj_tok in iri_labels:
                                        resolved = iri_labels.get(obj_tok)
                                    elif obj_tok in iri_literals:
                                        resolved = iri_literals.get(obj_tok)
                                    if resolved:
                                        _update_reservoir(samples_map, sample_counts, predicate, resolved)
                                    else:
                                        tail = obj_tok.strip("<>").rstrip("/").split("/")[-1]
                                        _update_reservoir(fallback_map, fallback_counts, predicate, tail)
                    if progress_every and total_lines % progress_every == 0:
                        done_bytes = file_base + bytes_read
                        prog = _progress_line(start_ts, done_bytes, total_bytes)
                        print(f"\r  Lignes lues: {total_lines:,} | {prog}", end='', flush=True)
            print_top_props(
                predicates_found,
                top_n=top_n,
                title=f"\n  📋 Top {top_n} prédicats (après {file_path.name}):",
                output_file=None,
                samples_map=None,
                min_count=1000,
            )
            done_bytes = file_base + file_path.stat().st_size
        if progress_every:
            done_bytes = total_bytes
            prog = _progress_line(start_ts, done_bytes, total_bytes)
            print(f"\r  Lignes lues: {total_lines:,} | {prog}", end='', flush=True)

    # Final write to file (once)
    if output_file:
        print_top_props(
            predicates_found,
            top_n=top_n,
            title=None,
            output_file=output_file,
            samples_map=samples_map,
            min_count=1000,
            write_samples=True,
            fallback_map=fallback_map,
        )
    return predicates_found

def _is_pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def _normalize_worker_share(value, default=0.95):
    try:
        share = float(value)
    except Exception:
        share = float(default)
    if share <= 0 or share > 1.0:
        return float(default)
    return share


def compute_shared_workers(lock_path, share=None):
    share = _normalize_worker_share(ALIGN_CPU_SHARE if share is None else share)
    cpu = _CPU_COUNT
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    
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
        
        # Ajouter le pid courant s'il n'est pas déjà là
        if os.getpid() not in active_pids:
            active_pids.append(os.getpid())
        
        # Réécrire la liste nettoyée
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

def get_shared_workers(lock_path, share=None, override=None):
    if override:
        return min(max(1, int(override)), MAX_PARALLEL_WORKERS), None, None
    workers, runs, cpu = compute_shared_workers(lock_path, share=share)
    return max(1, min(workers, MAX_PARALLEL_WORKERS)), runs, cpu

def normalize_for_matching(text):
    """
    Normalisation pour matching:
    - Lowercase
    - Suppression accents/diacritiques
    - Suppression uniquement des tokens configurés via --ignore-chars
    - Si "special-chars" est demandé: garde seulement [a-z0-9]
    """
    if not text:
        return ""
    if not _NORMALIZATION_ENABLED:
        return text
    special_chars = "__SPECIAL_CHARS__" in _EXTRA_STRIP_CHARS
    if _EXTRA_STRIP_CHARS:
        # Remove only configured tokens (single chars and optional multi-char tokens).
        tokens = [tok for tok in _EXTRA_STRIP_CHARS if tok and tok != "__SPECIAL_CHARS__"]
        for tok in sorted(tokens, key=len, reverse=True):
            text = text.replace(tok, "")
    # 1) Lowercase
    text = text.lower()
    # 2) Remove accents/diacritics (NFKD)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # Drop control/format characters that may break downstream parsing.
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("C"))
    # 3) Optional aggressive mode
    if special_chars:
        return re.sub(r'[^a-z0-9]', '', text)
    return text

def _looks_like_phone_mode(value):
    for p in split_predicate_patterns(value):
        low = p.lower()
        if (
            "telephone" in low
            or "phone" in low
            or "phonenumber" in low
            or "p1329" in low
        ):
            return True
    return False

def normalize_for_phone_matching(text):
    """
    Phone normalization:
    - Apply configured token stripping and base normalization
    - Keep only '+' and digits
    """
    base = normalize_for_matching(text)
    if not base:
        return ""
    return "".join(ch for ch in base if ch == "+" or ch.isdigit())

def normalize_value_for_matching(text, phone_mode=False):
    if phone_mode:
        return normalize_for_phone_matching(text)
    return normalize_for_matching(text)

def prepare_predicate_pattern(pattern):
    """
    Prépare un pattern de prédicat:
    - Matching de noms de propriétés/prédicats: toujours case-insensitive via lowercase.
    - La normalisation configurable des valeurs (ignore chars, etc.) ne doit pas impacter
      le matching des prédicats.
    Retourne (pattern_normalized, use_raw) pour compatibilité; use_raw reste True.
    """
    if pattern is None:
        return "", False
    p = str(pattern).strip()
    if not p:
        return "", False
    return p.lower(), True


def split_predicate_patterns(pattern):
    """
    Split a user predicate pattern string into multiple OR-patterns.

    Supported separators:
    - comma (,)
    - semicolon (;)
    - newlines

    Example:
      "sameAs, url" -> ["sameAs", "url"]
    """
    if pattern is None:
        return []
    text = str(pattern).strip()
    if not text:
        return []
    parts = re.split(r"[\n,;]+", text)
    cleaned = []
    seen = set()
    for raw in parts:
        token = str(raw or "").strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(token)
    return cleaned


def prepare_predicate_patterns(pattern):
    """
    Prepare multiple predicate patterns for OR matching.

    Returns a list of tuples: [(pattern_normalized, use_raw, original_token), ...]
    """
    prepared = []
    seen = set()
    for token in split_predicate_patterns(pattern):
        norm, use_raw = prepare_predicate_pattern(token)
        if not norm:
            continue
        key = (norm, bool(use_raw))
        if key in seen:
            continue
        seen.add(key)
        prepared.append((norm, bool(use_raw), token))
    return prepared


def predicate_matches_prepared_patterns(predicate, prepared_patterns):
    """
    True if predicate matches any prepared pattern (OR semantics).
    """
    if not prepared_patterns:
        return False
    predicate_raw = str(predicate or "").lower()
    for pattern_normalized, use_raw, _original in prepared_patterns:
        # Predicate/property-name matching is always case-insensitive only.
        haystack = predicate_raw
        if pattern_normalized in haystack:
            return True
    return False

def normalize_predicate_for_match(predicate, use_raw):
    return str(predicate or "").lower()

def normalize_country_code(isrc_normalized):
    """
    Normalise les codes pays non-standards dans les ISRC
    - GX → GB (code non-standard utilisé par certains)
    - UK → GB (UK n'est pas le code ISO, c'est GB)
    - GE → (Géorgie, garde tel quel pour l'instant)
    
    Note: Cette fonction prend un ISRC déjà normalisé (lowercase, alphanumeriques)
    """
    if not isrc_normalized or len(isrc_normalized) < 2:
        return isrc_normalized
    
    # Extraire les 2 premiers caractères (code pays)
    country_code = isrc_normalized[:2]
    rest = isrc_normalized[2:]
    
    # Mappings des codes non-standards
    country_mappings = {
        'gx': 'gb',  # GX → GB
        'uk': 'gb',  # UK → GB
        # Ajoute d'autres mappings si nécessaire
    }
    
    # Appliquer la normalisation si le code existe dans le mapping
    if country_code in country_mappings:
        return country_mappings[country_code] + rest
    
    return isrc_normalized

def normalize_wkd_class(wkd_class):
    """
    Normalise la classe Wikidata:
    - Qxxxx -> wd:Qxxxx
    - wdt:Qxxxx -> wd:Qxxxx (wdt n'est pas valide pour les items)
    - wd:Qxxxx ou IRI complet -> inchangé
    """
    if not wkd_class:
        return None
    wkd_class = wkd_class.strip()
    if wkd_class.startswith("http://") or wkd_class.startswith("https://"):
        return f"<{wkd_class}>"
    if wkd_class.startswith("wd:"):
        return wkd_class
    if wkd_class.startswith("wdt:Q"):
        return "wd:" + wkd_class.split("wdt:", 1)[1]
    if re.match(r'^[Qq]\d+$', wkd_class):
        return "wd:" + wkd_class.upper()
    return wkd_class

def normalize_wkd_prop_class(prop_class):
    """
    Normalise la classe Wikidata pour les propriétés:
    - Qxxxx -> wd:Qxxxx
    - wdt:Qxxxx -> wd:Qxxxx
    - wd:Qxxxx ou IRI complet -> inchangé
    """
    if not prop_class:
        return None
    prop_class = prop_class.strip()
    if prop_class.startswith("http://") or prop_class.startswith("https://"):
        return f"<{prop_class}>"
    if prop_class.startswith("wd:"):
        return prop_class
    if prop_class.startswith("wdt:Q"):
        return "wd:" + prop_class.split("wdt:", 1)[1]
    if re.match(r'^[Qq]\d+$', prop_class):
        return "wd:" + prop_class.upper()
    return prop_class

def normalize_wdc_type(wdc_type):
    if not wdc_type:
        return None
    wdc_type = wdc_type.strip()
    if wdc_type.startswith("<") and wdc_type.endswith(">"):
        return wdc_type
    if wdc_type.startswith("http://") or wdc_type.startswith("https://"):
        return f"<{wdc_type}>"
    if wdc_type.startswith("schema:"):
        return f"<http://schema.org/{wdc_type.split(':',1)[1]}>"
    return f"<http://schema.org/{wdc_type}>"

def normalize_wikidata_property(wikidata_property):
    """
    Normalise la propriété Wikidata pour SPARQL:
    - Pxx -> wdt:Pxx
    - IRI complet -> <IRI>
    - Prefix:suffix -> inchangé (suppose préfixe défini)
    """
    if not wikidata_property:
        return None
    wikidata_property = wikidata_property.strip()
    if wikidata_property.startswith("<") and wikidata_property.endswith(">"):
        return wikidata_property
    if wikidata_property.startswith("http://") or wikidata_property.startswith("https://"):
        return f"<{wikidata_property}>"
    if re.match(r'^[Pp]\d+$', wikidata_property):
        return "wdt:" + wikidata_property.upper()
    # Bare term -> assume wdt: prefix
    if re.match(r'^[A-Za-z_][A-Za-z0-9_-]*$', wikidata_property):
        return "wdt:" + wikidata_property
    return wikidata_property


def normalize_target_endpoint_key(target_endpoint):
    key = str(target_endpoint or "").strip().lower()
    if key in TARGET_ENDPOINTS:
        return key
    return "wikidata"


def resolve_target_endpoint_url(target_endpoint, target_endpoint_url=None):
    key = normalize_target_endpoint_key(target_endpoint)
    custom = str(target_endpoint_url or "").strip()
    if key == "custom":
        return custom
    default_url = str((TARGET_ENDPOINTS.get(key) or {}).get("sparql_url") or "").strip()
    return custom or default_url


def normalize_target_class(target_class, target_endpoint="wikidata"):
    key = normalize_target_endpoint_key(target_endpoint)
    if key == "wikidata":
        return normalize_wkd_class(target_class)
    raw = str(target_class or "").strip()
    if not raw:
        return None
    if raw.startswith("<") and raw.endswith(">"):
        return raw
    if raw.startswith("http://") or raw.startswith("https://"):
        return f"<{raw}>"
    return raw


def normalize_target_property(target_property, target_endpoint="wikidata"):
    key = normalize_target_endpoint_key(target_endpoint)
    if key == "wikidata":
        return normalize_wikidata_property(target_property)
    raw = str(target_property or "").strip()
    if not raw:
        return None
    if raw.startswith("<") and raw.endswith(">"):
        return raw
    if raw.startswith("http://") or raw.startswith("https://"):
        return f"<{raw}>"
    return raw


def normalize_prefix_declarations(prefix_text):
    text = str(prefix_text or "").strip()
    if not text:
        return []
    out = []
    seen = set()
    for raw_line in text.splitlines():
        line = str(raw_line or "").strip()
        if not line:
            continue
        m = _PREFIX_DECL_RE.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        iri = m.group(2).strip()
        key = f"{name.lower()}|{iri}"
        if key in seen:
            continue
        seen.add(key)
        out.append(f"PREFIX {name}: <{iri}>")
    return out


def render_prefix_declarations(prefix_text):
    rows = normalize_prefix_declarations(prefix_text)
    if not rows:
        return ""
    return "\n".join(rows)


def extract_target_entity_iri(value, target_endpoint="wikidata", target_endpoint_url=None):
    key = normalize_target_endpoint_key(target_endpoint)
    if key == "wikidata":
        return extract_wd_entity_iri(value)
    raw = str(value or "").strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1].strip()
    if not raw:
        return None
    try:
        parsed = urlparse(unquote(raw))
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    return raw

def extract_wd_entity_iri(value):
    if not value:
        return None
    value = str(value).strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    if not value:
        return None

    # Already a bare QID.
    m = re.fullmatch(r"[Qq](\d+)", value)
    if m:
        return f"http://www.wikidata.org/entity/Q{m.group(1)}"

    # Prefix form (wd:Q42).
    m = re.fullmatch(r"wd:[Qq](\d+)", value, flags=re.IGNORECASE)
    if m:
        return f"http://www.wikidata.org/entity/Q{m.group(1)}"

    try:
        parsed = urlparse(unquote(value))
    except Exception:
        parsed = None
    if not parsed or parsed.scheme not in {"http", "https"}:
        return None

    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    if host != "wikidata.org":
        return None

    path_parts = [p for p in (parsed.path or "").split("/") if p]
    for token in reversed(path_parts):
        m = re.fullmatch(r"[Qq](\d+)", token.strip())
        if m:
            return f"http://www.wikidata.org/entity/Q{m.group(1)}"

    query_map = parse_qs(parsed.query or "", keep_blank_values=False)
    for key in ("title", "entity", "id", "q"):
        for raw in query_map.get(key, []):
            m = re.fullmatch(r"[Qq](\d+)", str(raw).strip())
            if m:
                return f"http://www.wikidata.org/entity/Q{m.group(1)}"

    frag = (parsed.fragment or "").strip()
    m = re.fullmatch(r"[Qq](\d+)", frag)
    if m:
        return f"http://www.wikidata.org/entity/Q{m.group(1)}"

    return None

def discover_parts(class_name):
    """Découvre les parts disponibles pour une classe"""
    url = urljoin(WDC_BASE_URL, f"{class_name}/")
    
    print_color(f"🔍 Découverte des parts disponibles pour {class_name}...", Colors.BLUE)
    print(f"   URL: {url}")
    
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        parts = []
        
        for link in soup.find_all('a'):
            href = link.get('href', '')
            if re.match(r'part_\d+\.gz$', href):
                parts.append(href)
        
        parts.sort(key=lambda x: int(re.search(r'\d+', x).group()))
        
        print_color(f"✅ {len(parts)} parts trouvées", Colors.GREEN)
        return parts
        
    except Exception as e:
        print_color(f"❌ Erreur: {e}", Colors.RED)
        return []

def parse_parts_spec(parts_spec, available_parts=None):
    """Parse la spécification des parts (all, 0-3, 0,1,2)"""
    if parts_spec.lower() == "all":
        return available_parts or []
    
    selected = []
    
    # Range: 0-3
    if '-' in parts_spec:
        start, end = map(int, parts_spec.split('-'))
        for i in range(start, end + 1):
            part_file = f"part_{i}.gz"
            if available_parts is None or part_file in available_parts:
                selected.append(part_file)
    
    # Liste: 0,1,2
    elif ',' in parts_spec:
        for num in parts_spec.split(','):
            part_file = f"part_{num.strip()}.gz"
            if available_parts is None or part_file in available_parts:
                selected.append(part_file)
    
    # Single: 0
    else:
        part_file = f"part_{parts_spec}.gz"
        if available_parts is None or part_file in available_parts:
            selected.append(part_file)
    
    return selected

def download_file(url, dest_path):
    """Télécharge un fichier avec barre de progression"""
    if _CANCEL_CHECK and _CANCEL_CHECK():
        raise RuntimeError("Cancelled")
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()
    
    total_size = int(response.headers.get('content-length', 0))
    
    try:
        with open(dest_path, 'wb') as f:
            if total_size == 0:
                f.write(response.content)
            else:
                downloaded = 0
                chunk_size = 8192
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if _CANCEL_CHECK and _CANCEL_CHECK():
                        raise RuntimeError("Cancelled")
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        percent = (downloaded / total_size) * 100
                        print(f"\r  Téléchargement: {percent:.1f}% ({downloaded}/{total_size} bytes)", end='')
                print()  # Newline après progression
    except Exception:
        try:
            if Path(dest_path).exists():
                Path(dest_path).unlink()
        except Exception:
            pass
        raise
    finally:
        try:
            response.close()
        except Exception:
            pass

def _decompress_worker(gz_path, nq_path):
    gz_path = Path(gz_path)
    nq_path = Path(nq_path)
    with gzip.open(gz_path, 'rb') as f_in:
        with open(nq_path, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
    if gz_path.exists():
        try:
            gz_path.unlink()
        except Exception:
            pass
    return str(nq_path)

def download_and_decompress(class_name, parts, work_dir, parallel_decompress=True, workers=None, lock_path=None):
    """Télécharge et décompresse les parts"""
    work_dir = Path(work_dir)
    work_dir.mkdir(exist_ok=True)
    
    decompressed_files = []
    
    print_color(f"\n📦 Téléchargement/Décompression de {len(parts)} parts...", Colors.BLUE)
    executor = None
    futures = {}
    current_workers = None
    
    for i, part_file in enumerate(parts, 1):
        if _CANCEL_CHECK and _CANCEL_CHECK():
            raise RuntimeError("Cancelled")
        print(f"\n[{i}/{len(parts)}] {part_file}")
        
        gz_path = work_dir / part_file
        nq_path = work_dir / part_file.replace('.gz', '')
        
        # Skip si déjà décompressé
        if nq_path.exists():
            size = nq_path.stat().st_size / (1024**2)  # MB
            print_color(f"  ✅ Déjà disponible ({size:.1f} MB)", Colors.GREEN)
            if gz_path.exists():
                try:
                    gz_path.unlink()
                    print_color("  🧹 .gz supprimé (déjà décompressé)", Colors.GREEN)
                except Exception:
                    pass
            decompressed_files.append(nq_path)
            continue
        
        # Download si nécessaire
        if not gz_path.exists():
            url = urljoin(WDC_BASE_URL, f"{class_name}/{part_file}")
            print(f"  ⬇️  Téléchargement depuis {url}")
            try:
                download_file(url, gz_path)
                size = gz_path.stat().st_size / (1024**2)
                print_color(f"  ✅ Téléchargé ({size:.1f} MB)", Colors.GREEN)
            except Exception as e:
                if "Cancelled" in str(e):
                    raise
                print_color(f"  ❌ Erreur: {e}", Colors.RED)
                continue
        else:
            size = gz_path.stat().st_size / (1024**2)
            print_color(f"  ✅ Déjà téléchargé ({size:.1f} MB)", Colors.GREEN)
        
        # Décompresser (parallèle si activé)
        print("  📂 Décompression...")
        try:
            if parallel_decompress:
                if lock_path:
                    desired_workers, _runs, _cpu = get_shared_workers(
                        lock_path, share=ALIGN_CPU_SHARE, override=workers
                    )
                else:
                    desired_workers = workers or 1
                
                if executor is None or (not futures and desired_workers != current_workers):
                    if executor:
                        executor.shutdown(wait=True)
                    executor = ProcessPoolExecutor(max_workers=desired_workers)
                    current_workers = desired_workers
                
                fut = executor.submit(_decompress_worker, str(gz_path), str(nq_path))
                futures[fut] = (part_file, gz_path, nq_path)
            else:
                _decompress_worker(str(gz_path), str(nq_path))
                size = nq_path.stat().st_size / (1024**2)
                print_color(f"  ✅ Décompressé ({size:.1f} MB)", Colors.GREEN)
                decompressed_files.append(nq_path)
        except Exception as e:
            if "Cancelled" in str(e):
                raise
            print_color(f"  ❌ Erreur décompression: {e}", Colors.RED)
    
    if executor:
        for fut in as_completed(futures):
            part_file, gz_path, nq_path = futures[fut]
            try:
                nq_path_str = fut.result()
                nq_path = Path(nq_path_str)
                size = nq_path.stat().st_size / (1024**2)
                print_color(f"  ✅ Décompressé ({nq_path.name}, {size:.1f} MB)", Colors.GREEN)
                decompressed_files.append(nq_path)
            except Exception as e:
                if "Cancelled" in str(e):
                    raise
                print_color(f"  ❌ Erreur décompression: {e}", Colors.RED)
                # Auto-heal once: remove broken artifacts, re-download and decompress this part.
                try:
                    if nq_path.exists():
                        nq_path.unlink()
                except Exception:
                    pass
                try:
                    if gz_path.exists():
                        gz_path.unlink()
                except Exception:
                    pass
                try:
                    url = urljoin(WDC_BASE_URL, f"{class_name}/{part_file}")
                    print(f"  🔁 Retry download: {url}")
                    download_file(url, gz_path)
                    _decompress_worker(str(gz_path), str(nq_path))
                    size = nq_path.stat().st_size / (1024**2)
                    print_color(f"  ✅ Retry OK ({nq_path.name}, {size:.1f} MB)", Colors.GREEN)
                    decompressed_files.append(nq_path)
                except Exception as retry_e:
                    if "Cancelled" in str(retry_e):
                        raise
                    print_color(f"  ❌ Retry décompression échouée: {retry_e}", Colors.RED)
        executor.shutdown(wait=True)
    
    return decompressed_files

def _filter_file_worker(args):
    file_path, prepared_patterns, tmp_dir, collect_top_props = args
    file_path = Path(file_path)
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_out = tmp_dir / f"{file_path.name}.filtered"
    
    file_lines = 0
    file_matched = 0
    predicates_found = defaultdict(int) if collect_top_props else None
    
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as in_f:
        with open(tmp_out, 'w', encoding='utf-8') as out_f:
            for line in in_f:
                file_lines += 1
                predicates = re.findall(r'<([^>]+)>', line)
                if len(predicates) >= 1:
                    predicate = predicates[0]
                    if collect_top_props:
                        predicates_found[predicate] += 1
                    if predicate_matches_prepared_patterns(predicate, prepared_patterns):
                        out_f.write(line)
                        file_matched += 1
    
    return {
        "file": str(file_path),
        "tmp": str(tmp_out),
        "lines": file_lines,
        "matched": file_matched,
        "predicates": predicates_found or {},
    }

def filter_by_pattern(files, pattern, output_file, collect_top_props=False, top_n=100, parallel=True, workers=None):
    """
    Filtre les lignes dont le PRÉDICAT contient le pattern
    Équivalent à: ?x <...pattern...> ?value
    """
    print_color(f"\n🔍 Filtrage par pattern dans les PRÉDICATS: '{pattern}'", Colors.BLUE)
    print("   Recherche: <predicate> qui contient le pattern (case-insensitive)")
    
    prepared_patterns = prepare_predicate_patterns(pattern)
    if not prepared_patterns:
        raise ValueError("Empty predicate pattern")
    if len(prepared_patterns) == 1:
        pattern_normalized, pattern_raw, _ = prepared_patterns[0]
        if pattern_raw:
            print(f"   Pattern brut: '{pattern}'")
        else:
            print(f"   Pattern normalisé: '{pattern_normalized}'")
    else:
        print(f"   Patterns (OR): {', '.join(t for _, _, t in prepared_patterns)}")
    
    total_lines = 0
    matched_lines = 0
    predicates_found = defaultdict(int) if collect_top_props else None
    
    files = [Path(p) for p in files]
    do_parallel = parallel and len(files) > 1
    
    if do_parallel:
        tmp_dir = output_file.parent / f".tmp_filter_{output_file.stem}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        print_color(f"\n⚙️  Filtrage parallèle ({len(files)} fichiers, workers={workers or 1})...", Colors.BLUE)
        tasks = [(str(p), prepared_patterns, str(tmp_dir), collect_top_props) for p in files]
        results = {}
        with ProcessPoolExecutor(max_workers=workers) as ex:
            future_map = {ex.submit(_filter_file_worker, t): t[0] for t in tasks}
            for fut in as_completed(future_map):
                res = fut.result()
                results[res["file"]] = res
                print(f"  ✅ {Path(res['file']).name}: {res['matched']:,} matches")
        
        # Concaténer dans l'ordre des fichiers d'entrée
        with open(output_file, 'w', encoding='utf-8') as out_f:
            for file_path in files:
                res = results[str(file_path)]
                total_lines += res["lines"]
                matched_lines += res["matched"]
                if collect_top_props and predicates_found is not None:
                    for pred, cnt in res["predicates"].items():
                        predicates_found[pred] += cnt
                with open(res["tmp"], 'r', encoding='utf-8', errors='ignore') as in_f:
                    shutil.copyfileobj(in_f, out_f)
        
        shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        with open(output_file, 'w', encoding='utf-8') as out_f:
            for file_path in files:
                print(f"\n  📄 Traitement: {file_path.name}")
                file_lines = 0
                file_matched = 0
                
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as in_f:
                    for line in in_f:
                        file_lines += 1
                        total_lines += 1
                        
                        # Le prédicat est TOUJOURS le premier <...> dans NQuads
                        # Format: (sujet_ou_blanknode) <predicate> (objet) <graph> .
                        predicates = re.findall(r'<([^>]+)>', line)
                        
                        if len(predicates) >= 1:
                            predicate = predicates[0]  # Premier <...> = prédicat
                            if collect_top_props:
                                predicates_found[predicate] += 1
                            # Match si un pattern match le prédicat (OR)
                            if predicate_matches_prepared_patterns(predicate, prepared_patterns):
                                out_f.write(line)
                                matched_lines += 1
                                file_matched += 1
                        
                        if file_lines % 100000 == 0:
                            print(f"\r    Lignes: {file_lines:,} | Matches: {file_matched:,}", end='')
                
                print(f"\r    Lignes: {file_lines:,} | Matches: {file_matched:,}")
                percent = (file_matched / file_lines * 100) if file_lines > 0 else 0
                print(f"    Taux: {percent:.2f}%")
    
    print_color(f"\n✅ Filtrage terminé", Colors.GREEN)
    print(f"   Total lignes traitées: {total_lines:,}")
    print(f"   Lignes matchées: {matched_lines:,}")
    if total_lines > 0:
        print(f"   Taux global: {(matched_lines/total_lines*100):.2f}%")
    
    # Afficher les prédicats trouvés (top N)
    if collect_top_props and predicates_found is not None:
        print(f"\n📋 Prédicats trouvés (top {top_n}):")
        for pred, count in sorted(predicates_found.items(), key=lambda x: -x[1])[:top_n]:
            print(f"   {count:>8} × {pred}")
    
    return matched_lines


def _extract_batch(lines):
    value_map = defaultdict(list)
    all_raw_values = set()
    all_iris = set()
    country_code_changes = defaultdict(int)
    line_count = 0
    for line in lines:
        line_count += 1
        match = re.match(r'(\S+)\s+<([^>]+)>\s+"([^"]+)"', line)
        if match:
            subject = match.group(1)
            value = match.group(3)
            all_raw_values.add(value)
            all_iris.add(subject)
            value_normalized = normalize_for_matching(value)
            value_normalized_original = value_normalized
            value_normalized = normalize_country_code(value_normalized)
            if value_normalized != value_normalized_original:
                old_code = value_normalized_original[:2]
                new_code = value_normalized[:2]
                country_code_changes[f"{old_code.upper()}→{new_code.upper()}"] += 1
            if value_normalized:
                value_map[value_normalized].append((value, subject))
    return value_map, all_raw_values, all_iris, country_code_changes, line_count

def _extract_batch_with_pattern(args):
    lines, prepared_patterns, collect_top_props, wdc_value_is_wd_iri, phone_mode = args
    value_map = defaultdict(list)
    all_raw_values = set()
    all_iris = set()
    country_code_changes = defaultdict(int)
    predicates_found = defaultdict(int) if collect_top_props else None
    line_count = 0
    matched_count = 0
    for line in lines:
        line_count += 1
        subject, predicate_tok, obj_tok = _extract_spo_tokens(line)
        if not (subject and predicate_tok and obj_tok):
            continue
        predicate = predicate_tok.strip("<>")
        if collect_top_props:
            predicates_found[predicate] += 1
        if not predicate_matches_prepared_patterns(predicate, prepared_patterns):
            continue
        
        matched_count += 1
        if obj_tok.startswith('"'):
            value = _literal_lex(obj_tok)
            if value is None:
                continue
        elif obj_tok.startswith("<") and obj_tok.endswith(">"):
            value = obj_tok[1:-1]
        else:
            continue

        value_for_norm = value
        if wdc_value_is_wd_iri:
            wd_iri = extract_wd_entity_iri(value)
            if not wd_iri:
                continue
            value_for_norm = wd_iri
        elif not obj_tok.startswith('"'):
            # In non-Wikidata mode, only literal values should be aligned.
            continue

        all_raw_values.add(value)
        all_iris.add(subject)
        value_normalized = normalize_value_for_matching(value_for_norm, phone_mode=phone_mode)
        value_normalized_original = value_normalized
        value_normalized = normalize_country_code(value_normalized)
        if value_normalized != value_normalized_original:
            old_code = value_normalized_original[:2]
            new_code = value_normalized[:2]
            country_code_changes[f"{old_code.upper()}→{new_code.upper()}"] += 1
        if value_normalized:
            value_map[value_normalized].append((value, subject))
    return value_map, all_raw_values, all_iris, country_code_changes, line_count, matched_count, (predicates_found or {})

def extract_unique_iris(filtered_file, parallel=True, workers=None, batch_size=200000):
    """
    Extrait les valeurs distinctes (comme COUNT(DISTINCT ?value))
    Returns: {value_normalized: [(original_value, wdc_iri), ...]}
    """
    print_color(f"\n📊 Extraction des valeurs distinctes (équivalent SPARQL)...", Colors.BLUE)
    
    # {value_normalized: [(original_value, wdc_iri), ...]}
    value_map = defaultdict(list)
    all_raw_values = set()
    all_iris = set()
    country_code_changes = defaultdict(int)
    
    line_count = 0
    if parallel:
        n_workers = workers or 1
        futures = []
        buffer = []
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            with open(filtered_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    buffer.append(line)
                    if len(buffer) >= batch_size:
                        futures.append(ex.submit(_extract_batch, buffer))
                        buffer = []
                if buffer:
                    futures.append(ex.submit(_extract_batch, buffer))
            for fut in as_completed(futures):
                vmap, raw_vals, iris, cc_changes, lines = fut.result()
                line_count += lines
                all_raw_values.update(raw_vals)
                all_iris.update(iris)
                for k, v in cc_changes.items():
                    country_code_changes[k] += v
                for norm, entries in vmap.items():
                    value_map[norm].extend(entries)
                if line_count % 10000 == 0:
                    print(f"\r  Lignes: {line_count:,} | Valeurs distinctes: {len(all_raw_values)} | IRIs: {len(all_iris)}", end='')
    else:
        with open(filtered_file, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line_count += 1
                
                # Parse NQuads: <subj> <pred> "value" <graph>
                # ou: _:blanknode <pred> "value" <graph>
                match = re.match(r'(\S+)\s+<([^>]+)>\s+"([^"]+)"', line)
                
                if match:
                    subject = match.group(1)
                    predicate = match.group(2)
                    value = match.group(3)
                    
                    all_raw_values.add(value)
                    all_iris.add(subject)
                    
                    # Normaliser la valeur
                    value_normalized = normalize_for_matching(value)
                    
                    # Appliquer la normalisation des codes pays
                    value_normalized_original = value_normalized
                    value_normalized = normalize_country_code(value_normalized)
                    
                    # Tracker les changements
                    if value_normalized != value_normalized_original:
                        old_code = value_normalized_original[:2]
                        new_code = value_normalized[:2]
                        country_code_changes[f"{old_code.upper()}→{new_code.upper()}"] += 1
                    
                    if value_normalized:
                        value_map[value_normalized].append((value, subject))
                
                if line_count % 10000 == 0:
                    print(f"\r  Lignes: {line_count:,} | Valeurs distinctes: {len(all_raw_values)} | IRIs: {len(all_iris)}", end='')
    
    print(f"\r  Lignes: {line_count:,} | Valeurs distinctes: {len(all_raw_values)} | IRIs: {len(all_iris)}")
    
    # Afficher les changements de codes pays
    if country_code_changes:
        print(f"\n🌍 Normalisation des codes pays:")
        for change, count in sorted(country_code_changes.items(), key=lambda x: -x[1]):
            print(f"   {change}: {count} valeurs")
    
    # Statistiques comme SPARQL
    print_color(f"\n📈 Statistiques (équivalent requêtes SPARQL):", Colors.CYAN)
    print(f"   Lignes totales (triplets):           {line_count:,}")
    print(f"   IRIs distincts (?songWdc):           {len(all_iris):,}")
    print(f"   Valeurs brutes distinctes (?value):  {len(all_raw_values):,}")
    print(f"   Valeurs normalisées:                 {len(value_map):,}")
    
    # Distribution des longueurs
    lengths = defaultdict(int)
    for norm_val in value_map:
        lengths[len(norm_val)] += 1
    
    print(f"\n📏 Distribution des longueurs (normalisées):")
    for length in sorted(lengths.keys())[:10]:  # Top 10
        print(f"   {length:>2} chars: {lengths[length]:>6} valeurs")
    
    # Exemples
    print(f"\n📋 Exemples de valeurs (5 premiers):")
    for i, (norm, entries) in enumerate(list(value_map.items())[:5]):
        orig, iri = entries[0]
        # Tronquer les valeurs trop longues
        orig_display = orig if len(orig) <= 50 else orig[:47] + "..."
        print(f"   {i+1}. '{orig_display}'")
        print(f"      → '{norm}' (len={len(norm)})")
    
    return value_map

def _process_extract_window(window_batches, prepared_patterns, collect_top_props, wdc_value_is_wd_iri, phone_mode, executor):
    futures = [
        executor.submit(
            _extract_batch_with_pattern,
            (b, prepared_patterns, collect_top_props, wdc_value_is_wd_iri, phone_mode),
        )
        for b in window_batches
    ]
    for fut in as_completed(futures):
        vmap, raw_vals, iris, cc_changes, lines, matched, preds = fut.result()
        yield vmap, raw_vals, iris, cc_changes, lines, matched, preds

def extract_unique_iris_from_graph(graph_file, pattern, collect_top_props=False, top_n=100, parallel=True, workers=None, batch_size=500000, lock_path=None, progress_every=100, top_props_file=None, wdc_value_is_wd_iri=False, type_filter_iris=None):
    """
    Scanne un fichier NQuads complet, filtre par pattern de prédicat,
    et extrait les valeurs distinctes sans générer de fichier filtré.
    """
    print_color(f"\n📊 Extraction directe depuis le graphe (sans fichier filtré)...", Colors.BLUE)
    prepared_patterns = prepare_predicate_patterns(pattern)
    if not prepared_patterns:
        return {}, 0
    phone_mode = _looks_like_phone_mode(pattern)
    if len(prepared_patterns) == 1:
        pattern_normalized, pattern_raw, _ = prepared_patterns[0]
        if pattern_raw:
            print(f"   Pattern brut: '{pattern}'")
        else:
            print(f"   Pattern normalisé: '{pattern_normalized}'")
    else:
        print(f"   Patterns (OR): {', '.join(t for _, _, t in prepared_patterns)}")
    
    value_map = defaultdict(list)
    all_raw_values = set()
    all_iris = set()
    country_code_changes = defaultdict(int)
    predicates_found = defaultdict(int) if collect_top_props else None
    total_lines = 0
    matched_lines = 0
    allowed_subjects = collect_allowed_subjects_by_type([graph_file], type_filter_iris, progress_every=progress_every)
    if allowed_subjects is not None and len(allowed_subjects) == 0:
        print_color("❌ Aucun sujet ne matche le rdf:type demandé", Colors.RED)
        return {}, 0
    
    total_bytes = Path(graph_file).stat().st_size
    done_bytes = 0
    start_ts = time.time()
    if progress_every:
        prog = _progress_line(start_ts, done_bytes, total_bytes)
        print(f"  ⏳ Progress: Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", flush=True)
    if parallel:
        buffer = []
        window_batches = []
        if lock_path:
            n_workers, _runs, _cpu = get_shared_workers(
                lock_path, share=ALIGN_CPU_SHARE, override=workers
            )
        else:
            n_workers = min(max(1, int(workers or 1)), MAX_PARALLEL_WORKERS)
        window_size = max(1, n_workers * 6)
        bytes_read = 0
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            with open(graph_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    bytes_read += len(line)
                    total_lines += 1
                    if allowed_subjects is not None:
                        subject_tok, _, _ = _extract_spo_tokens(line)
                        if not subject_tok or subject_tok not in allowed_subjects:
                            continue
                    buffer.append(line)
                    if len(buffer) >= batch_size:
                        window_batches.append(buffer)
                        buffer = []

                    if progress_every and total_lines % progress_every == 0:
                        done_bytes = bytes_read
                        prog = _progress_line(start_ts, done_bytes, total_bytes)
                        print(f"\r  Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", end='', flush=True)

                    if len(window_batches) >= window_size:
                        for vmap, raw_vals, iris, cc_changes, lines, matched, preds in _process_extract_window(
                            window_batches, prepared_patterns, collect_top_props, wdc_value_is_wd_iri, phone_mode, ex
                        ):
                            matched_lines += matched
                            all_raw_values.update(raw_vals)
                            all_iris.update(iris)
                            for k, v in cc_changes.items():
                                country_code_changes[k] += v
                            for norm, entries in vmap.items():
                                value_map[norm].extend(entries)
                            if collect_top_props and predicates_found is not None:
                                for pred, cnt in preds.items():
                                    predicates_found[pred] += cnt
                            if progress_every and total_lines % progress_every == 0:
                                done_bytes = bytes_read
                                prog = _progress_line(start_ts, done_bytes, total_bytes)
                                print(f"\r  Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", end='', flush=True)
                        window_batches = []

                # Reste
                if buffer:
                    window_batches.append(buffer)
                if window_batches:
                    for vmap, raw_vals, iris, cc_changes, lines, matched, preds in _process_extract_window(
                        window_batches, prepared_patterns, collect_top_props, wdc_value_is_wd_iri, phone_mode, ex
                    ):
                        matched_lines += matched
                        all_raw_values.update(raw_vals)
                        all_iris.update(iris)
                        for k, v in cc_changes.items():
                            country_code_changes[k] += v
                        for norm, entries in vmap.items():
                            value_map[norm].extend(entries)
                        if collect_top_props and predicates_found is not None:
                            for pred, cnt in preds.items():
                                predicates_found[pred] += cnt
                        if progress_every and total_lines % progress_every == 0:
                            done_bytes = bytes_read
                            prog = _progress_line(start_ts, done_bytes, total_bytes)
                            print(f"\r  Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", end='', flush=True)
    else:
        bytes_read = 0
        with open(graph_file, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                bytes_read += len(line)
                total_lines += 1
                subject, predicate_tok, obj_tok = _extract_spo_tokens(line)
                if not (subject and predicate_tok and obj_tok):
                    continue
                if allowed_subjects is not None and subject not in allowed_subjects:
                    continue
                predicate = predicate_tok.strip("<>")
                if collect_top_props:
                    predicates_found[predicate] += 1
                if not predicate_matches_prepared_patterns(predicate, prepared_patterns):
                    continue
                
                matched_lines += 1
                if obj_tok.startswith('"'):
                    value = _literal_lex(obj_tok)
                    if value is None:
                        continue
                elif obj_tok.startswith("<") and obj_tok.endswith(">"):
                    value = obj_tok[1:-1]
                else:
                    continue
                value_for_norm = value
                if wdc_value_is_wd_iri:
                    wd_iri = extract_wd_entity_iri(value)
                    if not wd_iri:
                        continue
                    value_for_norm = wd_iri
                elif not obj_tok.startswith('"'):
                    continue
                all_raw_values.add(value)
                all_iris.add(subject)
                value_normalized = normalize_value_for_matching(value_for_norm, phone_mode=phone_mode)
                value_normalized_original = value_normalized
                value_normalized = normalize_country_code(value_normalized)
                if value_normalized != value_normalized_original:
                    old_code = value_normalized_original[:2]
                    new_code = value_normalized[:2]
                    country_code_changes[f"{old_code.upper()}→{new_code.upper()}"] += 1
                if value_normalized:
                    value_map[value_normalized].append((value, subject))
                if progress_every and total_lines % progress_every == 0:
                    done_bytes = bytes_read
                    prog = _progress_line(start_ts, done_bytes, total_bytes)
                    print(f"\r  Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", end='', flush=True)
    
    if progress_every:
        done_bytes = total_bytes
        prog = _progress_line(start_ts, done_bytes, total_bytes)
        print(f"\r  Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", end='', flush=True)
    print(f"\r  Lignes: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)}")
    
    if progress_every:
        done_bytes = total_bytes
        prog = _progress_line(start_ts, done_bytes, total_bytes)
        print(f"\r  Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", end='', flush=True)
    if matched_lines == 0:
        print_color("❌ Aucune ligne ne matche le pattern", Colors.RED)
        return {}, 0
    
    if country_code_changes:
        print(f"\n🌍 Normalisation des codes pays:")
        for change, count in sorted(country_code_changes.items(), key=lambda x: -x[1]):
            print(f"   {change}: {count} valeurs")
    
    print_color(f"\n📈 Statistiques (équivalent requêtes SPARQL):", Colors.CYAN)
    print(f"   Lignes totales (triplets):           {matched_lines:,}")
    print(f"   IRIs distincts (?songWdc):           {len(all_iris):,}")
    print(f"   Valeurs brutes distinctes (?value):  {len(all_raw_values):,}")
    print(f"   Valeurs normalisées:                 {len(value_map):,}")
    
    # Distribution des longueurs
    lengths = defaultdict(int)
    for norm_val in value_map:
        lengths[len(norm_val)] += 1
    print(f"\n📏 Distribution des longueurs (normalisées):")
    for length in sorted(lengths.keys())[:10]:
        print(f"   {length:>2} chars: {lengths[length]:>6} valeurs")
    
    # Exemples
    print(f"\n📋 Exemples de valeurs (5 premiers):")
    for i, (norm, entries) in enumerate(list(value_map.items())[:5]):
        orig, iri = entries[0]
        orig_display = orig if len(orig) <= 50 else orig[:47] + "..."
        print(f"   {i+1}. '{orig_display}'")
        print(f"      → '{norm}' (len={len(norm)})")
    
    if collect_top_props and predicates_found is not None:
        print_top_props(
            predicates_found,
            top_n=top_n,
            title=f"\n📋 Prédicats trouvés (top {top_n}):",
            output_file=top_props_file,
        )
    
    return value_map, matched_lines

def extract_unique_iris_from_files(files, pattern, collect_top_props=False, top_n=100, parallel=True, workers=None, batch_size=500000, lock_path=None, progress_every=100, top_props_file=None, wdc_value_is_wd_iri=False, type_filter_iris=None):
    """
    Scanne plusieurs fichiers NQuads (parts), filtre par pattern de prédicat,
    et extrait les valeurs distinctes sans générer de fichier fusionné.
    """
    print_color(f"\n📊 Extraction directe depuis les parts (sans graphe fusionné)...", Colors.BLUE)
    prepared_patterns = prepare_predicate_patterns(pattern)
    if not prepared_patterns:
        return {}, 0
    phone_mode = _looks_like_phone_mode(pattern)
    if len(prepared_patterns) == 1:
        pattern_normalized, pattern_raw, _ = prepared_patterns[0]
        if pattern_raw:
            print(f"   Pattern brut: '{pattern}'")
        else:
            print(f"   Pattern normalisé: '{pattern_normalized}'")
    else:
        print(f"   Patterns (OR): {', '.join(t for _, _, t in prepared_patterns)}")
    
    value_map = defaultdict(list)
    all_raw_values = set()
    all_iris = set()
    country_code_changes = defaultdict(int)
    predicates_found = defaultdict(int) if collect_top_props else None
    total_lines = 0
    matched_lines = 0
    
    files = [Path(p) for p in files]
    allowed_subjects = collect_allowed_subjects_by_type(files, type_filter_iris, progress_every=progress_every)
    if allowed_subjects is not None and len(allowed_subjects) == 0:
        print_color("❌ Aucun sujet ne matche le rdf:type demandé", Colors.RED)
        return {}, 0
    
    total_bytes = sum(Path(p).stat().st_size for p in files)
    done_bytes = 0
    start_ts = time.time()
    if progress_every:
        prog = _progress_line(start_ts, done_bytes, total_bytes)
        print(f"  ⏳ Progress: Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", flush=True)
    if parallel:
        buffer = []
        window_batches = []
        if lock_path:
            n_workers, _runs, _cpu = get_shared_workers(
                lock_path, share=ALIGN_CPU_SHARE, override=workers
            )
        else:
            n_workers = min(max(1, int(workers or 1)), MAX_PARALLEL_WORKERS)
        window_size = max(1, n_workers * 6)
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            for file_path in files:
                file_base = done_bytes
                bytes_read = 0
                print(f"\n  📄 Scan: {file_path.name}")
                if progress_every:
                    prog = _progress_line(start_ts, done_bytes, total_bytes)
                    print(f"  ⏳ Progress: Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", flush=True)
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        bytes_read += len(line)
                        total_lines += 1
                        if allowed_subjects is not None:
                            subject_tok, _, _ = _extract_spo_tokens(line)
                            if not subject_tok or subject_tok not in allowed_subjects:
                                continue
                        buffer.append(line)
                        if len(buffer) >= batch_size:
                            window_batches.append(buffer)
                            buffer = []

                        if progress_every and total_lines % progress_every == 0:
                            done_bytes = file_base + bytes_read
                            prog = _progress_line(start_ts, done_bytes, total_bytes)
                            print(f"\r  Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", end='', flush=True)

                        if len(window_batches) >= window_size:
                            for vmap, raw_vals, iris, cc_changes, lines, matched, preds in _process_extract_window(
                                window_batches, prepared_patterns, collect_top_props, wdc_value_is_wd_iri, phone_mode, ex
                            ):
                                matched_lines += matched
                                all_raw_values.update(raw_vals)
                                all_iris.update(iris)
                                for k, v in cc_changes.items():
                                    country_code_changes[k] += v
                                for norm, entries in vmap.items():
                                    value_map[norm].extend(entries)
                                if collect_top_props and predicates_found is not None:
                                    for pred, cnt in preds.items():
                                        predicates_found[pred] += cnt
                                if progress_every and total_lines % progress_every == 0:
                                    done_bytes = file_base + bytes_read
                                    prog = _progress_line(start_ts, done_bytes, total_bytes)
                                    print(f"\r  Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", end='', flush=True)
                            window_batches = []
                if collect_top_props and predicates_found is not None:
                    print_top_props(
                        predicates_found,
                        top_n=top_n,
                        title=f"\n  📋 Top {top_n} prédicats (après {file_path.name}):",
                        output_file=top_props_file,
                    )

            if buffer:
                window_batches.append(buffer)
            if window_batches:
                for vmap, raw_vals, iris, cc_changes, lines, matched, preds in _process_extract_window(
                    window_batches, prepared_patterns, collect_top_props, wdc_value_is_wd_iri, phone_mode, ex
                ):
                    matched_lines += matched
                    all_raw_values.update(raw_vals)
                    all_iris.update(iris)
                    for k, v in cc_changes.items():
                        country_code_changes[k] += v
                    for norm, entries in vmap.items():
                        value_map[norm].extend(entries)
                    if collect_top_props and predicates_found is not None:
                        for pred, cnt in preds.items():
                            predicates_found[pred] += cnt
                    if progress_every and total_lines % progress_every == 0:
                        print(f"\r  Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)}", end='', flush=True)
    else:
        for file_path in files:
            file_base = done_bytes
            bytes_read = 0
            print(f"\n  📄 Scan: {file_path.name}")
            if progress_every:
                prog = _progress_line(start_ts, done_bytes, total_bytes)
                print(f"  ⏳ Progress: Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", flush=True)
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    bytes_read += len(line)
                    total_lines += 1
                    subject, predicate_tok, obj_tok = _extract_spo_tokens(line)
                    if not (subject and predicate_tok and obj_tok):
                        continue
                    if allowed_subjects is not None and subject not in allowed_subjects:
                        continue
                    predicate = predicate_tok.strip("<>")
                    if collect_top_props:
                        predicates_found[predicate] += 1
                    if not predicate_matches_prepared_patterns(predicate, prepared_patterns):
                        continue
                    
                    matched_lines += 1
                    if obj_tok.startswith('"'):
                        value = _literal_lex(obj_tok)
                        if value is None:
                            continue
                    elif obj_tok.startswith("<") and obj_tok.endswith(">"):
                        value = obj_tok[1:-1]
                    else:
                        continue
                    value_for_norm = value
                    if wdc_value_is_wd_iri:
                        wd_iri = extract_wd_entity_iri(value)
                        if not wd_iri:
                            continue
                        value_for_norm = wd_iri
                    elif not obj_tok.startswith('"'):
                        continue
                    all_raw_values.add(value)
                    all_iris.add(subject)
                    value_normalized = normalize_value_for_matching(value_for_norm, phone_mode=phone_mode)
                    value_normalized_original = value_normalized
                    value_normalized = normalize_country_code(value_normalized)
                    if value_normalized != value_normalized_original:
                        old_code = value_normalized_original[:2]
                        new_code = value_normalized[:2]
                        country_code_changes[f"{old_code.upper()}→{new_code.upper()}"] += 1
                    if value_normalized:
                        value_map[value_normalized].append((value, subject))
                if progress_every and total_lines % progress_every == 0:
                    done_bytes = file_base + bytes_read
                    prog = _progress_line(start_ts, done_bytes, total_bytes)
                    print(f"\r  Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", end='', flush=True)
            if collect_top_props and predicates_found is not None:
                print_top_props(
                    predicates_found,
                    top_n=top_n,
                    title=f"\n  📋 Top {top_n} prédicats (après {file_path.name}):",
                    output_file=top_props_file,
                )
    
    print(f"\r  Lignes: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)}")
    
    if matched_lines == 0:
        print_color("❌ Aucune ligne ne matche le pattern", Colors.RED)
        return {}, 0
    
    if country_code_changes:
        print(f"\n🌍 Normalisation des codes pays:")
        for change, count in sorted(country_code_changes.items(), key=lambda x: -x[1]):
            print(f"   {change}: {count} valeurs")
    
    print_color(f"\n📈 Statistiques (équivalent requêtes SPARQL):", Colors.CYAN)
    print(f"   Lignes totales (triplets):           {matched_lines:,}")
    print(f"   IRIs distincts (?songWdc):           {len(all_iris):,}")
    print(f"   Valeurs brutes distinctes (?value):  {len(all_raw_values):,}")
    print(f"   Valeurs normalisées:                 {len(value_map):,}")
    
    # Distribution des longueurs
    lengths = defaultdict(int)
    for norm_val in value_map:
        lengths[len(norm_val)] += 1
    print(f"\n📏 Distribution des longueurs (normalisées):")
    for length in sorted(lengths.keys())[:10]:
        print(f"   {length:>2} chars: {lengths[length]:>6} valeurs")
    
    # Exemples
    print(f"\n📋 Exemples de valeurs (5 premiers):")
    for i, (norm, entries) in enumerate(list(value_map.items())[:5]):
        orig, iri = entries[0]
        orig_display = orig if len(orig) <= 50 else orig[:47] + "..."
        print(f"   {i+1}. '{orig_display}'")
        print(f"      → '{norm}' (len={len(norm)})")
    
    if collect_top_props and predicates_found is not None:
        print(f"\n📋 Prédicats trouvés (top {top_n}):")
        for pred, count in sorted(predicates_found.items(), key=lambda x: -x[1])[:top_n]:
            print(f"   {count:>8} × {pred}")
    
    return value_map, matched_lines


def _is_rate_limited_error(exc: Exception) -> bool:
    msg = str(exc)
    return "429" in msg or "Too Many Requests" in msg


def _is_retryable_query_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    retry_tokens = (
        "incompleteread",
        "remote disconnected",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
        "timed out",
        "timeout",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "invalid control character",
        "unterminated string",
        "jsondecodeerror",
        "expecting value",
        "extra data",
    )
    return any(tok in msg for tok in retry_tokens)


def _load_sparql_json_payload(payload_text: str):
    try:
        return json.loads(payload_text)
    except json.JSONDecodeError:
        pass

    try:
        return json.loads(payload_text, strict=False)
    except json.JSONDecodeError:
        pass

    # Some endpoint responses may contain raw control chars in string values.
    cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", payload_text)
    return json.loads(cleaned, strict=False)


def _chunk_list(values, chunk_size):
    chunk_size = max(1, int(chunk_size))
    for i in range(0, len(values), chunk_size):
        yield values[i : i + chunk_size]


def _run_sparql_query_with_retry_to_endpoint(endpoint_url, query, headers, timeout_s, max_attempts, base_delay):
    endpoint = str(endpoint_url or "").strip() or WIKIDATA_ENDPOINT
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                endpoint,
                data={"query": query, "format": "json"},
                headers=headers,
                timeout=timeout_s,
            )
            response.raise_for_status()
            return _load_sparql_json_payload(response.text)
        except Exception as e:
            if (_is_rate_limited_error(e) or _is_retryable_query_error(e)) and attempt < max_attempts:
                delay_s = (base_delay * (2 ** (attempt - 1))) + random.uniform(0, 0.5)
                print_color(
                    f"⚠️ Wikidata query retry {attempt}/{max_attempts} in {delay_s:.1f}s ({type(e).__name__})...",
                    Colors.YELLOW,
                )
                time.sleep(delay_s)
                continue
            raise
    return None


def _run_sparql_query_with_retry(query, headers, timeout_s, max_attempts, base_delay):
    return _run_sparql_query_with_retry_to_endpoint(
        WIKIDATA_ENDPOINT,
        query=query,
        headers=headers,
        timeout_s=timeout_s,
        max_attempts=max_attempts,
        base_delay=base_delay,
    )


def _truthy_env(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _wikidata_cache_path(prop, wkd_class_norm, wkd_prop_class_norm, entity_iris=None, lang_key="all"):
    entity_iris = sorted({str(v).strip() for v in (entity_iris or []) if str(v).strip()})
    entity_hash = "none"
    if entity_iris:
        sha = hashlib.sha1()
        for iri in entity_iris:
            sha.update(iri.encode("utf-8", errors="ignore"))
            sha.update(b"\n")
        entity_hash = sha.hexdigest()
    key_payload = {
        "v": 1,
        "prop": prop or "?prop",
        "wkd_class": wkd_class_norm or "",
        "wkd_prop_class": wkd_prop_class_norm or "",
        "lang": str(lang_key or "all"),
        "entity_count": len(entity_iris),
        "entity_hash": entity_hash,
    }
    cache_key = hashlib.sha1(
        json.dumps(key_payload, sort_keys=True, ensure_ascii=True).encode("utf-8", errors="ignore")
    ).hexdigest()
    cache_root = Path(os.environ.get("WIKIDATA_CACHE_DIR", str(Path("Download") / ".wikidata_cache")))
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root / f"values_{cache_key}.json.gz"


def _load_wikidata_value_cache(path):
    if not path.exists() or not path.is_file():
        return None
    ttl_s_raw = os.environ.get("WIKIDATA_CACHE_TTL_S", os.environ.get("WIKIDATA_CACHE_TTL", "604800"))
    try:
        ttl_s = int(ttl_s_raw)
    except Exception:
        ttl_s = 604800
    if ttl_s > 0:
        try:
            age_s = max(0.0, time.time() - float(path.stat().st_mtime))
            if age_s > ttl_s:
                return None
        except Exception:
            return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return None
    value_map_raw = (payload or {}).get("value_map")
    if not isinstance(value_map_raw, dict):
        return None
    value_map = defaultdict(list)
    for norm, entries in value_map_raw.items():
        if not isinstance(norm, str):
            continue
        if not isinstance(entries, list):
            continue
        for pair in entries:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            value_map[norm].append((str(pair[0]), str(pair[1])))
    return value_map


def _save_wikidata_value_cache(path, value_map):
    try:
        tmp_path = path.with_name(path.name + f".tmp.{os.getpid()}")
        serializable = {}
        for norm, entries in (value_map or {}).items():
            if not isinstance(norm, str):
                continue
            rows = []
            for pair in entries:
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    continue
                rows.append([str(pair[0]), str(pair[1])])
            serializable[norm] = rows
        payload = {
            "saved_at": time.time(),
            "value_map": serializable,
        }
        with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, path)
        return True
    except Exception:
        try:
            if "tmp_path" in locals() and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def fetch_wikidata_values(wikidata_property, wkd_class=None, wkd_prop_class=None, entity_iris=None):
    """Récupère les valeurs depuis Wikidata pour une propriété donnée, avec filtre de classe optionnel"""
    print_color(f"\n🌐 Récupération des valeurs Wikidata ({wikidata_property})...", Colors.BLUE)
    
    prop = normalize_wikidata_property(wikidata_property)
    phone_mode = _looks_like_phone_mode(wikidata_property)
    
    class_filter = ""
    wkd_class_norm = normalize_wkd_class(wkd_class)
    if wkd_class_norm:
        class_filter = f"""
      ?entity wdt:P31 ?type .
      ?type wdt:P279* {wkd_class_norm} .
    """
    
    if not prop and entity_iris:
        property_triple = "BIND(STR(?entity) AS ?value) ."
    else:
        property_triple = "?entity ?prop ?value ." if not prop else f"?entity {prop} ?value ."
    prop_class_filter = ""
    wkd_prop_class_norm = normalize_wkd_prop_class(wkd_prop_class)
    if wkd_prop_class_norm:
        prop_class_filter = f"""
      ?prop wdt:P31 ?propType .
      ?propType wdt:P279* {wkd_prop_class_norm} .
    """
    
    entity_iris_sorted = sorted({str(v).strip() for v in (entity_iris or []) if str(v).strip()})
    if not prop and not entity_iris_sorted:
        print_color("❌ No Wikidata entity IRIs provided (empty VALUES set).", Colors.RED)
        return {}

    cache_disabled = _truthy_env(os.environ.get("WIKIDATA_CACHE_DISABLED", "0"))
    cache_lang = os.environ.get("WIKIDATA_CACHE_LANG", "all")
    cache_path = _wikidata_cache_path(
        prop=prop,
        wkd_class_norm=wkd_class_norm,
        wkd_prop_class_norm=wkd_prop_class_norm,
        entity_iris=entity_iris_sorted,
        lang_key=cache_lang,
    )
    if not cache_disabled:
        cached_map = _load_wikidata_value_cache(cache_path)
        if cached_map is not None:
            total_entities = sum(len(entries) for entries in cached_map.values())
            print_color(
                f"💾 Cache hit: {cache_path.name} ({len(cached_map)} valeurs normalisées, {total_entities} entités)",
                Colors.GREEN,
            )
            return cached_map
    query_template = """
    PREFIX wd: <http://www.wikidata.org/entity/>
    PREFIX wdt: <http://www.wikidata.org/prop/direct/>
    PREFIX p: <http://www.wikidata.org/prop/>
    PREFIX ps: <http://www.wikidata.org/prop/statement/>
    PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
    PREFIX pr: <http://www.wikidata.org/prop/reference/>
    PREFIX wds: <http://www.wikidata.org/entity/statement/>
    PREFIX wikibase: <http://wikiba.se/ontology#>
    PREFIX schema: <http://schema.org/>
    PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX prov: <http://www.w3.org/ns/prov#>
    
    SELECT ?entity ?value WHERE {{
      {values_filter}
      {property_triple}
      {prop_class_filter}
      {class_filter}
    }}
    """
    
    print(f"   Requête SPARQL pour {prop or '?prop'}...")

    try:
        max_attempts = max(1, int(os.environ.get("WIKIDATA_QUERY_MAX_RETRIES", "4")))
        base_delay = max(0.1, float(os.environ.get("WIKIDATA_QUERY_RETRY_DELAY", "2.0")))
        timeout_s = max(1, int(os.environ.get("WIKIDATA_QUERY_TIMEOUT", "300")))
        entity_batch_size = max(1, int(os.environ.get("WIKIDATA_ENTITY_BATCH_SIZE", "500")))
        headers = {
            "Accept": "application/sparql-results+json",
            "User-Agent": "beam-align/1.0",
        }
        bindings = []

        # Large VALUES payloads can yield massive/truncated JSON responses; batch them.
        if entity_iris_sorted and len(entity_iris_sorted) > entity_batch_size:
            batches = list(_chunk_list(entity_iris_sorted, entity_batch_size))
            print(f"   Batching entities: {len(entity_iris_sorted):,} IRIs in {len(batches)} batches (size={entity_batch_size})")
            for idx, entity_batch in enumerate(batches, 1):
                values = " ".join(f"<{uri}>" for uri in entity_batch)
                values_filter = f"VALUES ?entity {{ {values} }}\n"
                batch_query = query_template.format(
                    values_filter=values_filter,
                    property_triple=property_triple,
                    prop_class_filter=prop_class_filter,
                    class_filter=class_filter,
                )
                print(f"   [WD] batch {idx}/{len(batches)} size={len(entity_batch)}")
                results = _run_sparql_query_with_retry(
                    batch_query,
                    headers=headers,
                    timeout_s=timeout_s,
                    max_attempts=max_attempts,
                    base_delay=base_delay,
                )
                if results and isinstance(results, dict):
                    batch_bindings = (((results.get("results") or {}).get("bindings")) or [])
                    if isinstance(batch_bindings, list):
                        bindings.extend(batch_bindings)
        else:
            values_filter = ""
            if entity_iris_sorted:
                values = " ".join(f"<{uri}>" for uri in entity_iris_sorted)
                values_filter = f"VALUES ?entity {{ {values} }}\n"
            query = query_template.format(
                values_filter=values_filter,
                property_triple=property_triple,
                prop_class_filter=prop_class_filter,
                class_filter=class_filter,
            )
            results = _run_sparql_query_with_retry(
                query,
                headers=headers,
                timeout_s=timeout_s,
                max_attempts=max_attempts,
                base_delay=base_delay,
            )
            if results and isinstance(results, dict):
                direct_bindings = (((results.get("results") or {}).get("bindings")) or [])
                if isinstance(direct_bindings, list):
                    bindings.extend(direct_bindings)
        if not bindings:
            return {}
        
        # {value_normalized: [(original_value, wikidata_uri), ...]}
        value_map = defaultdict(list)
        all_raw_values = set()
        
        for result in bindings:
            try:
                value = result["value"]["value"]
                entity_uri = result["entity"]["value"]
            except Exception:
                continue
            
            all_raw_values.add(value)
            
            value_normalized = normalize_value_for_matching(value, phone_mode=phone_mode)
            
            # Appliquer la normalisation des codes pays
            value_normalized = normalize_country_code(value_normalized)
            
            if value_normalized:
                value_map[value_normalized].append((value, entity_uri))
        
        print_color(f"✅ {len(all_raw_values)} valeurs brutes distinctes", Colors.GREEN)
        print_color(f"✅ {len(value_map)} valeurs normalisées distinctes", Colors.GREEN)
        
        total_entities = sum(len(entries) for entries in value_map.values())
        print_color(f"✅ {total_entities} entités Wikidata", Colors.GREEN)
        if not cache_disabled:
            if _save_wikidata_value_cache(cache_path, value_map):
                print_color(f"💾 Cache saved: {cache_path.name}", Colors.BLUE)
        
        # Exemples
        print(f"\n📋 Exemples Wikidata (5 premiers):")
        for i, (norm, entries) in enumerate(list(value_map.items())[:5]):
            orig, uri = entries[0]
            print(f"   {i+1}. '{orig}' → '{norm}' (len={len(norm)})")
        
        return value_map
        
    except Exception as e:
        print_color(f"❌ Erreur: {e}", Colors.RED)
        return {}


def fetch_target_values(
    target_property,
    target_class=None,
    target_prop_class=None,
    entity_iris=None,
    target_endpoint="wikidata",
    target_endpoint_url=None,
    target_prefixes=None,
):
    endpoint_key = normalize_target_endpoint_key(target_endpoint)
    if endpoint_key == "wikidata":
        return fetch_wikidata_values(
            wikidata_property=target_property,
            wkd_class=target_class,
            wkd_prop_class=target_prop_class,
            entity_iris=entity_iris,
        )

    endpoint_url = resolve_target_endpoint_url(endpoint_key, target_endpoint_url)
    if not endpoint_url:
        print_color("❌ Target endpoint URL is empty.", Colors.RED)
        return {}

    print_color(
        f"\n🌐 Récupération des valeurs target ({target_property}) via {endpoint_key}...",
        Colors.BLUE,
    )

    prop = normalize_target_property(target_property, endpoint_key)
    class_norm = normalize_target_class(target_class, endpoint_key)
    entity_iris_sorted = sorted({str(v).strip() for v in (entity_iris or []) if str(v).strip()})
    if not prop and not entity_iris_sorted:
        print_color("❌ target_property is required when no entity IRIs are provided.", Colors.RED)
        return {}

    if not prop and entity_iris_sorted:
        property_triple = "BIND(STR(?entity) AS ?value) ."
    else:
        property_triple = f"?entity {prop} ?value ."

    class_filter = ""
    if class_norm:
        class_filter = f"""
      ?entity rdf:type ?type .
      ?type rdfs:subClassOf* {class_norm} .
    """

    query_template = """
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX schema: <http://schema.org/>
    PREFIX dbo: <http://dbpedia.org/ontology/>
    PREFIX dbp: <http://dbpedia.org/property/>
    PREFIX yago: <http://yago-knowledge.org/resource/>
    {custom_prefixes}
    SELECT ?entity ?value WHERE {{
      {values_filter}
      {property_triple}
      {class_filter}
    }}
    """
    custom_prefixes = render_prefix_declarations(target_prefixes)

    try:
        max_attempts = max(1, int(os.environ.get("TARGET_QUERY_MAX_RETRIES", "3")))
        base_delay = max(0.1, float(os.environ.get("TARGET_QUERY_RETRY_DELAY", "1.5")))
        timeout_s = max(1, int(os.environ.get("TARGET_QUERY_TIMEOUT", "120")))
        entity_batch_size = max(1, int(os.environ.get("TARGET_ENTITY_BATCH_SIZE", "500")))
        headers = {
            "Accept": "application/sparql-results+json",
            "User-Agent": "beam-align/1.0",
        }
        bindings = []

        if entity_iris_sorted and len(entity_iris_sorted) > entity_batch_size:
            batches = list(_chunk_list(entity_iris_sorted, entity_batch_size))
            print(
                f"   Batching entities: {len(entity_iris_sorted):,} IRIs in {len(batches)} batches (size={entity_batch_size})"
            )
            for idx, entity_batch in enumerate(batches, 1):
                values = " ".join(f"<{uri}>" for uri in entity_batch)
                values_filter = f"VALUES ?entity {{ {values} }}\n"
                batch_query = query_template.format(
                    custom_prefixes=custom_prefixes,
                    values_filter=values_filter,
                    property_triple=property_triple,
                    class_filter=class_filter,
                )
                print(f"   [TARGET] batch {idx}/{len(batches)} size={len(entity_batch)}")
                results = _run_sparql_query_with_retry_to_endpoint(
                    endpoint_url,
                    query=batch_query,
                    headers=headers,
                    timeout_s=timeout_s,
                    max_attempts=max_attempts,
                    base_delay=base_delay,
                )
                if results and isinstance(results, dict):
                    batch_bindings = (((results.get("results") or {}).get("bindings")) or [])
                    if isinstance(batch_bindings, list):
                        bindings.extend(batch_bindings)
        else:
            values_filter = ""
            if entity_iris_sorted:
                values = " ".join(f"<{uri}>" for uri in entity_iris_sorted)
                values_filter = f"VALUES ?entity {{ {values} }}\n"
            query = query_template.format(
                custom_prefixes=custom_prefixes,
                values_filter=values_filter,
                property_triple=property_triple,
                class_filter=class_filter,
            )
            results = _run_sparql_query_with_retry_to_endpoint(
                endpoint_url,
                query=query,
                headers=headers,
                timeout_s=timeout_s,
                max_attempts=max_attempts,
                base_delay=base_delay,
            )
            if results and isinstance(results, dict):
                direct_bindings = (((results.get("results") or {}).get("bindings")) or [])
                if isinstance(direct_bindings, list):
                    bindings.extend(direct_bindings)
        if not bindings:
            return {}

        phone_mode = _looks_like_phone_mode(target_property)
        value_map = defaultdict(list)
        all_raw_values = set()
        for result in bindings:
            try:
                value = result["value"]["value"]
                entity_uri = result["entity"]["value"]
            except Exception:
                continue
            all_raw_values.add(value)
            value_normalized = normalize_value_for_matching(value, phone_mode=phone_mode)
            value_normalized = normalize_country_code(value_normalized)
            if value_normalized:
                value_map[value_normalized].append((value, entity_uri))

        print_color(f"✅ {len(all_raw_values)} valeurs brutes distinctes", Colors.GREEN)
        print_color(f"✅ {len(value_map)} valeurs normalisées distinctes", Colors.GREEN)
        return value_map
    except Exception as e:
        print_color(f"❌ Erreur target endpoint: {e}", Colors.RED)
        return {}

def _exact_worker(args):
    wdc_items, wikidata_map, min_length = args
    exact_matches = []
    wdc_values_matched = set()
    for wdc_norm, wdc_entries in wdc_items:
        if len(wdc_norm) < min_length:
            continue
        if wdc_norm in wikidata_map:
            for wdc_orig, wdc_iri in wdc_entries:
                for wiki_orig, wiki_uri in wikidata_map[wdc_norm]:
                    exact_matches.append({
                        'wdc_iri': wdc_iri,
                        'wikidata_uri': wiki_uri,
                        'wdc_value': wdc_orig,
                        'wiki_value': wiki_orig,
                        'method': 'exact'
                    })
                    wdc_values_matched.add(wdc_orig)
    return exact_matches, wdc_values_matched

def _fuzzy_worker(args):
    wdc_items, wikidata_map, wikidata_norms, min_length = args
    fuzzy_matches = []
    wdc_values_matched = set()
    for wdc_norm, wdc_entries in wdc_items:
        if len(wdc_norm) < min_length:
            continue
        for wiki_norm in wikidata_norms:
            if len(wiki_norm) < min_length:
                continue
            min_len = min(len(wdc_norm), len(wiki_norm))
            if wdc_norm[:min_len] == wiki_norm[:min_len]:
                for wdc_orig, wdc_iri in wdc_entries:
                    for wiki_orig, wiki_uri in wikidata_map[wiki_norm]:
                        fuzzy_matches.append({
                            'wdc_iri': wdc_iri,
                            'wikidata_uri': wiki_uri,
                            'wdc_value': wdc_orig,
                            'wiki_value': wiki_orig,
                            'min_len': min_len,
                            'method': f'fuzzy_{min_len}'
                        })
                        wdc_values_matched.add(wdc_orig)
    return fuzzy_matches, wdc_values_matched

def _process_exact_window(chunks, wikidata_map, min_length, executor):
    futures = [executor.submit(_exact_worker, (chunk, wikidata_map, min_length)) for chunk in chunks]
    for fut in as_completed(futures):
        yield fut.result()

def _process_fuzzy_window(chunks, wikidata_map, wikidata_norms, min_length, executor):
    futures = [executor.submit(_fuzzy_worker, (chunk, wikidata_map, wikidata_norms, min_length)) for chunk in chunks]
    for fut in as_completed(futures):
        yield fut.result()

def fuzzy_link(wdc_map, wikidata_map, parallel=True, workers=None, lock_path=None, min_length=1):
    """
    Lie les entités WDC et Wikidata via fuzzy matching
    Compare sur la longueur du plus court des deux
    """
    print_color(f"\n🔗 Linking WDC ↔ Wikidata...", Colors.CYAN)
    print("   Stratégie: Matching exact")
    # Fuzzy min-len removed permanently
    
    # Fuzzy phase is disabled; keep exact matching available for short identifiers too
    # (e.g. ISO-2 country codes).
    MIN_LENGTH = max(1, int(min_length))
    
    exact_matches = []
    fuzzy_matches = []
    matched_pairs = set()
    
    total_comparisons = 0
    skipped_too_short = 0
    short_value_infos = []
    wdc_values_matched = set()  # Pour compter les valeurs WDC distinctes matchées
    
    print("\n   Phase 1: Matching exact...")
    wdc_items = list(wdc_map.items())
    if parallel and len(wdc_items) > 1:
        if lock_path:
            n_workers, _runs, _cpu = get_shared_workers(
                lock_path, share=ALIGN_CPU_SHARE, override=workers
            )
        else:
            n_workers = min(max(1, int(workers or 1)), MAX_PARALLEL_WORKERS)
        chunk_size = max(1, len(wdc_items) // max(1, n_workers))
        chunks = [wdc_items[i:i+chunk_size] for i in range(0, len(wdc_items), chunk_size)]
        window_size = max(1, n_workers * 2)
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            idx = 0
            while idx < len(chunks):
                window = chunks[idx:idx+window_size]
                for matches_part, wdc_matched_part in _process_exact_window(window, wikidata_map, MIN_LENGTH, ex):
                    exact_matches.extend(matches_part)
                    wdc_values_matched.update(wdc_matched_part)
                idx += window_size
    else:
        matches_part, wdc_matched_part = _exact_worker((wdc_items, wikidata_map, MIN_LENGTH))
        exact_matches.extend(matches_part)
        wdc_values_matched.update(wdc_matched_part)
    
    # Dédoublonnage exact par paire
    exact_unique = []
    exact_pairs = set()
    for m in exact_matches:
        pair = (m['wdc_iri'], m['wikidata_uri'])
        if pair not in exact_pairs:
            exact_pairs.add(pair)
            exact_unique.append(m)
    exact_matches = exact_unique
    
    print(f"   ✅ {len(exact_matches)} paires (exact)")
    
    print("\n   Phase 2: Matching fuzzy supprimé")
    all_matches = exact_matches
    print(f"   ✅ {len(all_matches)} paires (total)")
    return all_matches, wdc_values_matched

def export_unmatched_values(wdc_values_matched, wdc_map, output_dir, key_name=None):
    output_dir = Path(output_dir)
    header = f"{key_name}_value" if key_name else "wdc_value"
    unmatched_values = sorted({
        orig
        for entries in wdc_map.values()
        for orig, _iri in entries
        if orig not in wdc_values_matched
    })
    unmatched_file = output_dir / "wdc_unmatched_values.csv"
    with open(unmatched_file, "w", encoding="utf-8") as f:
        f.write(f"{header}\n")
        for val in unmatched_values:
            f.write(f"{val}\n")
    print(f"   ✅ {unmatched_file}")


def export_results(matches, wdc_values_matched, wdc_map, wikidata_map, output_dir, key_name=None,
                   class_name=None, parts_spec=None, pattern=None, wikidata_property=None,
                   wkd_class=None, wkd_prop_class=None, start_ts=None):
    """Exporte les résultats"""
    output_dir = Path(output_dir)
    
    print_color(f"\n💾 Export des résultats...", Colors.BLUE)
    
    # TSV détaillé
    tsv_file = output_dir / "wdc_wikidata_links.tsv"
    with open(tsv_file, 'w', encoding='utf-8') as f:
        f.write("wdc_iri\twikidata_uri\twdc_value\twiki_value\tmethod\tmin_len\n")
        for m in matches:
            min_len = m.get('min_len', '')
            f.write(f"{m['wdc_iri']}\t{m['wikidata_uri']}\t{m['wdc_value']}\t{m['wiki_value']}\t{m['method']}\t{min_len}\n")
    
    print(f"   ✅ {tsv_file}")
    
    # N-Triples owl:sameAs
    nt_file = output_dir / "owl_sameas.nt"
    with open(nt_file, 'w', encoding='utf-8') as f:
        for m in matches:
            f.write(f"<{m['wdc_iri']}> <http://www.w3.org/2002/07/owl#sameAs> <{m['wikidata_uri']}> .\n")
    
    print(f"   ✅ {nt_file}")
    
    # Statistiques
    stats_file = output_dir / "stats.txt"
    with open(stats_file, 'w', encoding='utf-8') as f:
        f.write("="*60 + "\n")
        f.write("STATISTIQUES DE LINKING\n")
        f.write("="*60 + "\n\n")

        # Inputs / contexte
        f.write("Inputs:\n")
        f.write(f"  Class: {class_name or ''}\n")
        f.write(f"  Parts: {parts_spec or ''}\n")
        f.write(f"  WDC predicate pattern: {pattern or ''}\n")
        f.write(f"  WD property: {wikidata_property or ''}\n")
        f.write(f"  WD class filter: {wkd_class or ''}\n")
        f.write(f"  WD property-class filter: {wkd_prop_class or ''}\n")
        f.write(f"  Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        if start_ts:
            elapsed = time.time() - start_ts
            took = _format_eta(elapsed).replace("ETA: ", "")
            f.write(f"  Duration: {took}\n")
        f.write("\n")

        # WDC volumes
        wdc_entities = {iri for entries in wdc_map.values() for (_val, iri) in entries}
        wdc_values_raw = {val for entries in wdc_map.values() for (val, _iri) in entries}
        wdc_values_norm = set(wdc_map.keys())
        wdc_pairs_total = sum(len(v) for v in wdc_map.values())
        f.write("WDC (volumes):\n")
        f.write(f"  Entities (distinct IRIs): {len(wdc_entities)}\n")
        f.write(f"  Values (distinct, raw): {len(wdc_values_raw)}\n")
        f.write(f"  Values (distinct, normalized): {len(wdc_values_norm)}\n")
        f.write(f"  Value→Entity pairs (total): {wdc_pairs_total}\n\n")

        # Wikidata volumes
        wd_entities = {iri for entries in wikidata_map.values() for (_val, iri) in entries}
        wd_values_raw = {val for entries in wikidata_map.values() for (val, _iri) in entries}
        wd_values_norm = set(wikidata_map.keys())
        wd_pairs_total = sum(len(v) for v in wikidata_map.values())
        f.write("Wikidata (volumes):\n")
        f.write(f"  Entities (distinct IRIs): {len(wd_entities)}\n")
        f.write(f"  Values (distinct, raw): {len(wd_values_raw)}\n")
        f.write(f"  Values (distinct, normalized): {len(wd_values_norm)}\n")
        f.write(f"  Value→Entity pairs (total): {wd_pairs_total}\n\n")

        exact_count = len([m for m in matches if m['method'] == 'exact'])
        fuzzy_count = len([m for m in matches if m['method'].startswith('fuzzy')])

        matched_wdc_entities = {m["wdc_iri"] for m in matches}
        matched_wd_entities = {m["wikidata_uri"] for m in matches}
        matched_wdc_values_raw = {m["wdc_value"] for m in matches}
        matched_wd_values_raw = {m["wiki_value"] for m in matches}
        matched_wdc_values_norm = set(wdc_values_matched)
        phone_mode = _looks_like_phone_mode(pattern) or _looks_like_phone_mode(wikidata_property)
        matched_wd_values_norm = set(
            normalize_value_for_matching(v, phone_mode=phone_mode)
            for v in matched_wd_values_raw
            if v
        )

        f.write("Matches:\n")
        f.write(f"  Pairs (exact): {exact_count}\n")
        f.write(f"  Pairs (fuzzy): {fuzzy_count}\n")
        f.write(f"  Total pairs: {len(matches)}\n")
        f.write(f"  WDC entities matched (distinct): {len(matched_wdc_entities)}\n")
        f.write(f"  Wikidata entities matched (distinct): {len(matched_wd_entities)}\n")
        f.write(f"  WDC values matched (distinct, raw): {len(matched_wdc_values_raw)}\n")
        f.write(f"  Wikidata values matched (distinct, raw): {len(matched_wd_values_raw)}\n\n")

        # Coverage
        def _pct(n, d):
            return (n / d * 100) if d else 0.0
        f.write("Coverage:\n")
        f.write(f"  WDC values (normalized): {len(matched_wdc_values_norm)}/{len(wdc_values_norm)} ({_pct(len(matched_wdc_values_norm), len(wdc_values_norm)):.2f}%)\n")
        f.write(f"  WDC entities: {len(matched_wdc_entities)}/{len(wdc_entities)} ({_pct(len(matched_wdc_entities), len(wdc_entities)):.2f}%)\n")
        f.write(f"  Wikidata entities: {len(matched_wd_entities)}/{len(wd_entities)} ({_pct(len(matched_wd_entities), len(wd_entities)):.2f}%)\n")
        f.write(f"  Wikidata values (normalized): {len(matched_wd_values_norm)}/{len(wd_values_norm)} ({_pct(len(matched_wd_values_norm), len(wd_values_norm)):.2f}%)\n")
    
    print(f"   ✅ {stats_file}")
    
    # Valeurs WDC non alignées
    export_unmatched_values(wdc_values_matched, wdc_map, output_dir, key_name=key_name)
    
    print_color(f"\n✅ Alignnement done.", Colors.GREEN)

def _sum_file_sizes(paths):
    total = 0
    for p in paths:
        try:
            total += Path(p).stat().st_size
        except FileNotFoundError:
            continue
    return total

def main():
    parser = argparse.ArgumentParser(description="WDC Entity Linker")
    parser.add_argument("class_name")
    parser.add_argument("parts_spec", nargs="?")
    parser.add_argument("pattern", nargs="?")
    parser.add_argument("wikidata_property", nargs="?")
    parser.add_argument("--wkd-class", help="Wikidata class QID or IRI (e.g., Q6256 or wdt:Q6256) used to filter items")
    parser.add_argument("--wkd-prop-class", help="Wikidata property class QID/IRI to filter ?prop (e.g., Q853614 for identifiers)")
    parser.add_argument("--wdc-type", help="WDC rdf:type IRI or class name (e.g., http://schema.org/Country or Country) used to filter extracted subjects")
    parser.add_argument("--top-props", action="store_true", help="Afficher le top 100 des propriétés WDC (calculé pendant le filtrage)")
    parser.add_argument("--workers", type=int, help="Nombre de workers pour le parallélisme (défaut: 80% CPU partagé entre runs)")
    parser.add_argument("--ignore-chars", help="Liste de caractères à supprimer avant normalisation (ex: \"spaces;+;\\\\;\\\\/;\\\\\\\\\")")
    parser.add_argument("--wdc-value-is-wikidata", action="store_true", help="Interpréter les valeurs WDC comme URLs Wikidata et matcher directement les entités")
    args = parser.parse_args()
    
    start_ts = time.time()
    class_name = args.class_name
    pattern = args.pattern
    parts_spec = args.parts_spec
    wikidata_property = args.wikidata_property
    if args.wdc_type:
        type_filter_iris = [normalize_wdc_type(args.wdc_type)]
    else:
        type_filter_iris = default_type_filter_iris_for_class(class_name)
    if args.ignore_chars:
        set_extra_strip_chars(parse_strip_list(args.ignore_chars))
    if args.wdc_value_is_wikidata and not args.wkd_class:
        print_color("❌ --wdc-value-is-wikidata nécessite --wkd-class", Colors.RED)
        sys.exit(1)

    # Mode: top-props uniquement (class_name + --top-props)
    top_props_only = args.top_props and not (parts_spec and pattern and wikidata_property)
    
    print("="*60)
    print("🎯 WDC Entity Linker")
    print("="*60)
    print(f"Classe:              {class_name}")
    print(f"Parts:               {parts_spec}")
    if not top_props_only:
        print(f"Pattern:             {pattern}")
        print(f"Propriété Wikidata:  {wikidata_property or '?prop'}")
    if args.wkd_class:
        print(f"Classe Wikidata:     {normalize_wkd_class(args.wkd_class)}")
    if args.wkd_prop_class:
        print(f"Classe Prop WD:      {normalize_wkd_prop_class(args.wkd_prop_class)}")
    if args.wdc_type:
        print(f"WDC rdf:type:        {normalize_wdc_type(args.wdc_type)}")
    print("="*60)
    
    # Setup directories (always under Download/<ClassName>)
    work_dir = Path("Download") / class_name
    work_dir.mkdir(parents=True, exist_ok=True)
    data_dir = work_dir

    # Calculer le nombre de workers par run (partage 80% CPU)
    lock_path = Path("Download") / ".workers.lock"
    workers_override = args.workers
    if workers_override:
        print(f"Workers:             {workers_override} (override)")
    else:
        workers_default, runs, cpu = compute_shared_workers(lock_path, share=ALIGN_CPU_SHARE)
        print(f"Workers:             {workers_default} (80% CPU partagé / {runs} runs, CPU={cpu})")
    
    # 1. Source WDC: toujours part_* (jamais *_full_graph.nq)
    decompressed_files = []
    available_parts = None
    if parts_spec is None:
        print_color("❌ parts_spec manquant (ex: all)", Colors.RED)
        sys.exit(1)
    if parts_spec.lower() == "all":
        available_parts = discover_parts(class_name)
        if not available_parts:
            print_color("❌ Aucune part disponible", Colors.RED)
            sys.exit(1)
    else:
        available_parts = discover_parts(class_name)
        if not available_parts:
            print_color("⚠️  Impossible de récupérer la liste distante, utilisation de la spécification locale.", Colors.YELLOW)
            available_parts = None

    parts_to_download = parse_parts_spec(parts_spec, available_parts)
    if not parts_to_download:
        print_color(f"❌ Aucune part valide pour '{parts_spec}'", Colors.RED)
        sys.exit(1)

    print_color(f"\n📦 {len(parts_to_download)} parts sélectionnées", Colors.GREEN)
    decompressed_files = download_and_decompress(
        class_name,
        parts_to_download,
        data_dir,
        parallel_decompress=True,
        workers=workers_override,
        lock_path=lock_path,
    )
    if not decompressed_files:
        print_color("❌ Aucun fichier disponible", Colors.RED)
        sys.exit(1)
    
    # 2. Top-props only: pas besoin de pattern/WD
    if top_props_only:
        top_props_file = work_dir / "top-props.txt"
        if top_props_file.exists():
            top_props_file.unlink()
        scan_top_props_from_files(
            decompressed_files,
            top_n=1000,
            parallel=True,
            workers=workers_override,
            lock_path=lock_path,
            progress_every=100,
            output_file=top_props_file,
            type_filter_iris=type_filter_iris,
        )
        print_color(f"\n✅ Top-props écrit dans {top_props_file}", Colors.GREEN)
        elapsed = time.time() - start_ts
        took = _format_eta(elapsed).replace("ETA: ", "")
        print_color(f"\n⏱️  Temps total: {took}", Colors.GREEN)
        try:
            with open(work_dir / "stats.txt", "a", encoding="utf-8") as f:
                f.write(f"top-props took {took}\n")
        except Exception:
            pass
        return

    # 3. Extraire les IRIs WDC uniques (sans fichier filtré ni graphe fusionné)
    wdc_map, matched_count = extract_unique_iris_from_files(
        decompressed_files,
        pattern,
        collect_top_props=args.top_props,
        top_n=100,
        parallel=True,
        workers=workers_override,
        lock_path=lock_path,
        progress_every=100,
        wdc_value_is_wd_iri=args.wdc_value_is_wikidata,
        type_filter_iris=type_filter_iris,
    )
    if matched_count == 0:
        sys.exit(1)
    
    # 6. Récupérer les valeurs Wikidata
    if args.wdc_value_is_wikidata:
        # Extract WD entity IRIs from WDC values
        wd_entity_iris = set()
        for entries in wdc_map.values():
            for value, _iri in entries:
                wd_iri = extract_wd_entity_iri(value)
                if wd_iri:
                    wd_entity_iris.add(wd_iri)
        if not wd_entity_iris:
            print_color("❌ No Wikidata URLs extracted from WDC values.", Colors.RED)
            sys.exit(1)
        wikidata_map = fetch_wikidata_values(
            wikidata_property=None,
            wkd_class=args.wkd_class,
            wkd_prop_class=None,
            entity_iris=sorted(wd_entity_iris),
        )
    else:
        wikidata_map = fetch_wikidata_values(wikidata_property, args.wkd_class, args.wkd_prop_class)
    if not wikidata_map:
        print_color("❌ Impossible de récupérer les données Wikidata", Colors.RED)
        sys.exit(1)
    
    # 7. Linking
    matches, wdc_values_matched = fuzzy_link(
        wdc_map,
        wikidata_map,
        parallel=True,
        workers=workers_override,
        lock_path=lock_path,
    )
    
    # 8. Statistiques finales
    print("\n" + "="*60)
    print_color("📊 RÉSULTATS FINAUX", Colors.CYAN)
    print("="*60)
    print(f"\n🔢 STATISTIQUES WDC:")
    total_lines = 115955562 if len(decompressed_files) == 7 else None
    total_lines_str = f"{total_lines:,}" if total_lines is not None else "N/A"
    print(f"   Lignes totales traitées:           {total_lines_str}")
    print(f"   Lignes matchant le pattern:        {matched_count:,}")
    print(f"   Valeurs distinctes (brutes):       {len(wdc_map):,}")
    
    print(f"\n🌐 STATISTIQUES WIKIDATA:")
    print(f"   Valeurs distinctes:                {len(wikidata_map):,}")
    
    print(f"\n🔗 LINKING:")
    exact_count = len([m for m in matches if m['method'] == 'exact'])
    fuzzy_count = len([m for m in matches if m['method'].startswith('fuzzy')])
    print(f"   Paires matchées (exact):           {exact_count:,}")
    print(f"   Paires matchées (fuzzy):           {fuzzy_count:,}")
    print(f"   TOTAL paires:                      {len(matches):,}")
    
    print_color(f"\n🎯 VALEURS WDC DISTINCTES LINKÉES:   {len(wdc_values_matched):,}", Colors.GREEN)
    
    if len(wdc_map) > 0:
        coverage = (len(wdc_values_matched) / len(wdc_map)) * 100
        print_color(f"📈 COVERAGE WDC:                     {coverage:.2f}%", Colors.GREEN)
    
    print(f"\n💡 Comparaison avec SPARQL:")
    print(f"   SELECT COUNT(DISTINCT ?value) WHERE {{ ?s <...{pattern}...> ?value }}")
    print(f"   → Devrait être environ: {len(wdc_map):,} valeurs distinctes")
    print(f"\n   SELECT DISTINCT ?value WHERE {{")
    print(f"     SERVICE <wikidata> {{ ?w {wikidata_property} ?value")
    if args.wkd_class:
        wkd_class_norm = normalize_wkd_class(args.wkd_class)
        print(f"       ?w wdt:P31 ?type .")
        print(f"       ?type wdt:P279* {wkd_class_norm} .")
    print(f"     }}")
    print(f"     ?s <...{pattern}...> ?value")
    print(f"   }}")
    print(f"   → {len(wdc_values_matched):,} valeurs WDC ont un match Wikidata")
    
    # 9. Export
    export_results(
        matches,
        wdc_values_matched,
        wdc_map,
        wikidata_map,
        work_dir,
        key_name=pattern,
        class_name=class_name,
        parts_spec=parts_spec,
        pattern=pattern,
        wikidata_property=wikidata_property,
        wkd_class=args.wkd_class,
        wkd_prop_class=args.wkd_prop_class,
        start_ts=start_ts,
    )

    elapsed = time.time() - start_ts
    took = _format_eta(elapsed).replace("ETA: ", "")
    print_color(f"\n⏱️  Temps total: {took}", Colors.GREEN)
    
    print("\n" + "="*60)
    print_color("✨ TERMINÉ!", Colors.GREEN)
    print("="*60)
    print(f"\nFichiers générés dans: {work_dir}/")
    print(f"  - wdc_wikidata_links.tsv (liens détaillés)")
    print(f"  - owl_sameas.nt (triplets RDF)")
    print(f"  - stats.txt (statistiques)")
    print()

if __name__ == "__main__":
    main()
