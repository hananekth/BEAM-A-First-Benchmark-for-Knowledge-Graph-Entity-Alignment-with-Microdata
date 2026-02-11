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
import shutil
import requests
import unicodedata
import random
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
import time
import fcntl
from pathlib import Path
from collections import defaultdict
from SPARQLWrapper import SPARQLWrapper, JSON
from urllib.parse import urljoin
from bs4 import BeautifulSoup

# Configuration
WDC_BASE_URL = "https://data.dws.informatik.uni-mannheim.de/structureddata/2024-12/quads/classspecific/"
WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"

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
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p.lower() == "spaces":
            chars.extend([" ", "\t", "\n", "\r"])
            continue
        if p.lower() == "special-chars":
            # Placeholder token handled in normalize_for_matching
            chars.append("__SPECIAL_CHARS__")
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
                            n_workers, _runs, _cpu = get_shared_workers(lock_path, share=0.8, override=workers)
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
                n_workers, _runs, _cpu = get_shared_workers(lock_path, share=0.8, override=workers)
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

def compute_shared_workers(lock_path, share=0.8):
    cpu = os.cpu_count() or 1
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

def get_shared_workers(lock_path, share=0.8, override=None):
    if override:
        return override, None, None
    return compute_shared_workers(lock_path, share=share)

def normalize_for_matching(text):
    """
    Normalisation agressive pour matching:
    - Lowercase
    - Suppression accents/diacritiques
    - Suppression caractères spéciaux
    - Garde seulement alphanumériques
    """
    if not text:
        return ""
    if not _NORMALIZATION_ENABLED:
        return text
    special_chars = "__SPECIAL_CHARS__" in _EXTRA_STRIP_CHARS
    if _EXTRA_STRIP_CHARS:
        text = "".join(ch for ch in text if ch not in _EXTRA_STRIP_CHARS)
    if special_chars:
        # Strip all non-alphanumeric characters
        text = re.sub(r"[^A-Za-z0-9]", "", text)
    # 1) Lowercase
    text = text.lower()
    # 2) Remove accents/diacritics (NFKD)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # 3) Keep only a-z0-9
    return re.sub(r'[^a-z0-9]', '', text)

def prepare_predicate_pattern(pattern):
    """
    Prépare un pattern de prédicat:
    - Si pattern ressemble à une IRI/CURIE (contient ://, / ou :), on le garde tel quel (lowercase).
    - Sinon, normalisation agressive (normalize_for_matching).
    Retourne (pattern_normalized, use_raw).
    """
    if pattern is None:
        return "", False
    p = str(pattern).strip()
    if not p:
        return "", False
    if "://" in p or "/" in p or ":" in p:
        return p.lower(), True
    return normalize_for_matching(p), False

def normalize_predicate_for_match(predicate, use_raw):
    return predicate.lower() if use_raw else normalize_for_matching(predicate)

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

def extract_wd_entity_iri(value):
    if not value:
        return None
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    m = re.search(r'(?:https?://www\.wikidata\.org/(?:wiki|entity)/)(Q\d+)', value)
    if not m:
        return None
    return f"http://www.wikidata.org/entity/{m.group(1)}"

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
    futures = []
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
                    desired_workers, _runs, _cpu = get_shared_workers(lock_path, share=0.8, override=workers)
                else:
                    desired_workers = workers or 1
                
                if executor is None or (not futures and desired_workers != current_workers):
                    if executor:
                        executor.shutdown(wait=True)
                    executor = ProcessPoolExecutor(max_workers=desired_workers)
                    current_workers = desired_workers
                
                futures.append(executor.submit(_decompress_worker, str(gz_path), str(nq_path)))
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
        executor.shutdown(wait=True)
    
    return decompressed_files

def _filter_file_worker(args):
    file_path, pattern_normalized, pattern_raw, tmp_dir, collect_top_props = args
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
                    predicate_normalized = normalize_predicate_for_match(predicate, pattern_raw)
                    if pattern_normalized in predicate_normalized:
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
    
    pattern_normalized, pattern_raw = prepare_predicate_pattern(pattern)
    if pattern_raw:
        print(f"   Pattern brut: '{pattern}'")
    else:
        print(f"   Pattern normalisé: '{pattern_normalized}'")
    
    total_lines = 0
    matched_lines = 0
    predicates_found = defaultdict(int) if collect_top_props else None
    
    files = [Path(p) for p in files]
    do_parallel = parallel and len(files) > 1
    
    if do_parallel:
        tmp_dir = output_file.parent / f".tmp_filter_{output_file.stem}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        print_color(f"\n⚙️  Filtrage parallèle ({len(files)} fichiers, workers={workers or 1})...", Colors.BLUE)
        tasks = [(str(p), pattern_normalized, pattern_raw, str(tmp_dir), collect_top_props) for p in files]
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
                            predicate_normalized = normalize_predicate_for_match(predicate, pattern_raw)
                            
                            # Match si pattern dans prédicat normalisé
                            if pattern_normalized in predicate_normalized:
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
    lines, pattern_normalized, pattern_raw, collect_top_props = args
    value_map = defaultdict(list)
    all_raw_values = set()
    all_iris = set()
    country_code_changes = defaultdict(int)
    predicates_found = defaultdict(int) if collect_top_props else None
    line_count = 0
    matched_count = 0
    for line in lines:
        line_count += 1
        predicates = re.findall(r'<([^>]+)>', line)
        if len(predicates) >= 1:
            predicate = predicates[0]
            if collect_top_props:
                predicates_found[predicate] += 1
            predicate_normalized = normalize_predicate_for_match(predicate, pattern_raw)
            if pattern_normalized not in predicate_normalized:
                continue
        else:
            continue
        
        matched_count += 1
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

def _process_extract_window(window_batches, pattern_normalized, pattern_raw, collect_top_props, n_workers,
                            value_map, all_raw_values, all_iris, country_code_changes, predicates_found):
    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futures = [ex.submit(_extract_batch_with_pattern, (b, pattern_normalized, pattern_raw, collect_top_props)) for b in window_batches]
        for fut in as_completed(futures):
            vmap, raw_vals, iris, cc_changes, lines, matched, preds = fut.result()
            yield vmap, raw_vals, iris, cc_changes, lines, matched, preds

def extract_unique_iris_from_graph(graph_file, pattern, collect_top_props=False, top_n=100, parallel=True, workers=None, batch_size=500000, lock_path=None, progress_every=100, top_props_file=None, wdc_value_is_wd_iri=False):
    """
    Scanne un fichier NQuads complet, filtre par pattern de prédicat,
    et extrait les valeurs distinctes sans générer de fichier filtré.
    """
    print_color(f"\n📊 Extraction directe depuis le graphe (sans fichier filtré)...", Colors.BLUE)
    pattern_normalized, pattern_raw = prepare_predicate_pattern(pattern)
    if pattern_raw:
        print(f"   Pattern brut: '{pattern}'")
    else:
        print(f"   Pattern normalisé: '{pattern_normalized}'")
    
    value_map = defaultdict(list)
    all_raw_values = set()
    all_iris = set()
    country_code_changes = defaultdict(int)
    predicates_found = defaultdict(int) if collect_top_props else None
    total_lines = 0
    matched_lines = 0
    
    total_bytes = Path(graph_file).stat().st_size
    done_bytes = 0
    start_ts = time.time()
    if progress_every:
        prog = _progress_line(start_ts, done_bytes, total_bytes)
        print(f"  ⏳ Progress: Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", flush=True)
    if parallel:
        buffer = []
        window_batches = []
        n_workers = workers or 1
        lines_since_workers_update = 0
        bytes_read = 0
        with open(graph_file, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                bytes_read += len(line)
                total_lines += 1
                lines_since_workers_update += 1
                buffer.append(line)
                if len(buffer) >= batch_size:
                    window_batches.append(buffer)
                    buffer = []
                
                # Recalcule les workers périodiquement (évite lock à chaque ligne)
                if lines_since_workers_update >= 10000:
                    if lock_path:
                        n_workers, _runs, _cpu = get_shared_workers(lock_path, share=0.8, override=workers)
                    else:
                        n_workers = workers or 1
                    lines_since_workers_update = 0
                window_size = max(1, n_workers * 6)
                
                if progress_every and total_lines % progress_every == 0:
                    done_bytes = bytes_read
                    prog = _progress_line(start_ts, done_bytes, total_bytes)
                    print(f"\r  Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", end='', flush=True)
                
                if len(window_batches) >= window_size:
                    for vmap, raw_vals, iris, cc_changes, lines, matched, preds in _process_extract_window(
                        window_batches, pattern_normalized, pattern_raw, collect_top_props, n_workers,
                        value_map, all_raw_values, all_iris, country_code_changes, predicates_found
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
                if lock_path:
                    n_workers, _runs, _cpu = get_shared_workers(lock_path, share=0.8, override=workers)
                else:
                    n_workers = workers or 1
                for vmap, raw_vals, iris, cc_changes, lines, matched, preds in _process_extract_window(
                    window_batches, pattern_normalized, pattern_raw, collect_top_props, n_workers,
                    value_map, all_raw_values, all_iris, country_code_changes, predicates_found
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
                predicates = re.findall(r'<([^>]+)>', line)
                if len(predicates) >= 1:
                    predicate = predicates[0]
                    if collect_top_props:
                        predicates_found[predicate] += 1
                    predicate_normalized = normalize_predicate_for_match(predicate, pattern_raw)
                    if pattern_normalized not in predicate_normalized:
                        continue
                else:
                    continue
                
                matched_lines += 1
                match = re.match(r'(\S+)\s+<([^>]+)>\s+"([^"]+)"', line)
                if match:
                    subject = match.group(1)
                    value = match.group(3)
                    value_for_norm = value
                    if wdc_value_is_wd_iri:
                        wd_iri = extract_wd_entity_iri(value)
                        if wd_iri:
                            value_for_norm = wd_iri
                    all_raw_values.add(value)
                    all_iris.add(subject)
                    value_normalized = normalize_for_matching(value_for_norm)
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

def extract_unique_iris_from_files(files, pattern, collect_top_props=False, top_n=100, parallel=True, workers=None, batch_size=500000, lock_path=None, progress_every=100, top_props_file=None, wdc_value_is_wd_iri=False):
    """
    Scanne plusieurs fichiers NQuads (parts), filtre par pattern de prédicat,
    et extrait les valeurs distinctes sans générer de fichier fusionné.
    """
    print_color(f"\n📊 Extraction directe depuis les parts (sans graphe fusionné)...", Colors.BLUE)
    pattern_normalized, pattern_raw = prepare_predicate_pattern(pattern)
    if pattern_raw:
        print(f"   Pattern brut: '{pattern}'")
    else:
        print(f"   Pattern normalisé: '{pattern_normalized}'")
    
    value_map = defaultdict(list)
    all_raw_values = set()
    all_iris = set()
    country_code_changes = defaultdict(int)
    predicates_found = defaultdict(int) if collect_top_props else None
    total_lines = 0
    matched_lines = 0
    
    files = [Path(p) for p in files]
    
    total_bytes = sum(Path(p).stat().st_size for p in files)
    done_bytes = 0
    start_ts = time.time()
    if progress_every:
        prog = _progress_line(start_ts, done_bytes, total_bytes)
        print(f"  ⏳ Progress: Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", flush=True)
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
                print(f"  ⏳ Progress: Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", flush=True)
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    bytes_read += len(line)
                    total_lines += 1
                    lines_since_workers_update += 1
                    buffer.append(line)
                    if len(buffer) >= batch_size:
                        window_batches.append(buffer)
                        buffer = []

                    # Recalcule les workers périodiquement (évite lock à chaque ligne)
                    if lines_since_workers_update >= 10000:
                        if lock_path:
                            n_workers, _runs, _cpu = get_shared_workers(lock_path, share=0.8, override=workers)
                        else:
                            n_workers = workers or 1
                        lines_since_workers_update = 0

                    window_size = max(1, n_workers * 6)
                    
                    if progress_every and total_lines % progress_every == 0:
                        done_bytes = file_base + bytes_read
                        prog = _progress_line(start_ts, done_bytes, total_bytes)
                        print(f"\r  Lignes lues: {total_lines:,} | Matches: {matched_lines:,} | Valeurs distinctes: {len(all_raw_values)} | {prog}", end='', flush=True)
                    
                    if len(window_batches) >= window_size:
                        for vmap, raw_vals, iris, cc_changes, lines, matched, preds in _process_extract_window(
                            window_batches, pattern_normalized, pattern_raw, collect_top_props, n_workers,
                            value_map, all_raw_values, all_iris, country_code_changes, predicates_found
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
            if lock_path:
                n_workers, _runs, _cpu = get_shared_workers(lock_path, share=0.8, override=workers)
            else:
                n_workers = workers or 1
            for vmap, raw_vals, iris, cc_changes, lines, matched, preds in _process_extract_window(
                window_batches, pattern_normalized, pattern_raw, collect_top_props, n_workers,
                value_map, all_raw_values, all_iris, country_code_changes, predicates_found
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
                    predicates = re.findall(r'<([^>]+)>', line)
                    if len(predicates) >= 1:
                        predicate = predicates[0]
                        if collect_top_props:
                            predicates_found[predicate] += 1
                        predicate_normalized = normalize_predicate_for_match(predicate, pattern_raw)
                        if pattern_normalized not in predicate_normalized:
                            continue
                    else:
                        continue
                    
                    matched_lines += 1
                    match = re.match(r'(\S+)\s+<([^>]+)>\s+"([^"]+)"', line)
                    if match:
                        subject = match.group(1)
                        value = match.group(3)
                        value_for_norm = value
                        if wdc_value_is_wd_iri:
                            wd_iri = extract_wd_entity_iri(value)
                            if wd_iri:
                                value_for_norm = wd_iri
                        all_raw_values.add(value)
                        all_iris.add(subject)
                        value_normalized = normalize_for_matching(value_for_norm)
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

def fetch_wikidata_values(wikidata_property, wkd_class=None, wkd_prop_class=None, entity_iris=None):
    """Récupère les valeurs depuis Wikidata pour une propriété donnée, avec filtre de classe optionnel"""
    print_color(f"\n🌐 Récupération des valeurs Wikidata ({wikidata_property})...", Colors.BLUE)
    
    prop = normalize_wikidata_property(wikidata_property)
    
    sparql = SPARQLWrapper(WIKIDATA_ENDPOINT)
    sparql.setReturnFormat(JSON)
    sparql.setTimeout(300)
    
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
    
    values_filter = ""
    if entity_iris:
        values = " ".join(f"<{uri}>" for uri in entity_iris)
        values_filter = f"VALUES ?entity {{ {values} }}\n"
    query = f"""
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
    
    sparql.setQuery(query)
    
    try:
        results = sparql.query().convert()
        
        # {value_normalized: [(original_value, wikidata_uri), ...]}
        value_map = defaultdict(list)
        all_raw_values = set()
        
        for result in results["results"]["bindings"]:
            value = result["value"]["value"]
            entity_uri = result["entity"]["value"]
            
            all_raw_values.add(value)
            
            value_normalized = normalize_for_matching(value)
            
            # Appliquer la normalisation des codes pays
            value_normalized = normalize_country_code(value_normalized)
            
            if value_normalized:
                value_map[value_normalized].append((value, entity_uri))
        
        print_color(f"✅ {len(all_raw_values)} valeurs brutes distinctes", Colors.GREEN)
        print_color(f"✅ {len(value_map)} valeurs normalisées distinctes", Colors.GREEN)
        
        total_entities = sum(len(entries) for entries in value_map.values())
        print_color(f"✅ {total_entities} entités Wikidata", Colors.GREEN)
        
        # Exemples
        print(f"\n📋 Exemples Wikidata (5 premiers):")
        for i, (norm, entries) in enumerate(list(value_map.items())[:5]):
            orig, uri = entries[0]
            print(f"   {i+1}. '{orig}' → '{norm}' (len={len(norm)})")
        
        return value_map
        
    except Exception as e:
        print_color(f"❌ Erreur: {e}", Colors.RED)
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

def _process_exact_window(chunks, wikidata_map, min_length, n_workers):
    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futures = [ex.submit(_exact_worker, (chunk, wikidata_map, min_length)) for chunk in chunks]
        for fut in as_completed(futures):
            yield fut.result()

def _process_fuzzy_window(chunks, wikidata_map, wikidata_norms, min_length, n_workers):
    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futures = [ex.submit(_fuzzy_worker, (chunk, wikidata_map, wikidata_norms, min_length)) for chunk in chunks]
        for fut in as_completed(futures):
            yield fut.result()

def fuzzy_link(wdc_map, wikidata_map, parallel=True, workers=None, lock_path=None):
    """
    Lie les entités WDC et Wikidata via fuzzy matching
    Compare sur la longueur du plus court des deux
    """
    print_color(f"\n🔗 Linking WDC ↔ Wikidata...", Colors.CYAN)
    print("   Stratégie: Matching exact")
    # Fuzzy min-len removed permanently
    
    MIN_LENGTH = 8  # ISRC standard = 12 chars, on tolère jusqu'à 8
    
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
            n_workers, _runs, _cpu = get_shared_workers(lock_path, share=0.8, override=workers)
        else:
            n_workers = workers or 1
        chunk_size = max(1, len(wdc_items) // max(1, n_workers))
        chunks = [wdc_items[i:i+chunk_size] for i in range(0, len(wdc_items), chunk_size)]
        idx = 0
        while idx < len(chunks):
            if lock_path:
                n_workers, _runs, _cpu = get_shared_workers(lock_path, share=0.8, override=workers)
            else:
                n_workers = workers or 1
            window_size = max(1, n_workers * 2)
            window = chunks[idx:idx+window_size]
            for matches_part, wdc_matched_part in _process_exact_window(window, wikidata_map, MIN_LENGTH, n_workers):
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
        matched_wd_values_norm = set(normalize_for_matching(v) for v in matched_wd_values_raw if v)

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
    parser.add_argument("--wdc-type", help="WDC rdf:type IRI or class name (e.g., http://schema.org/Country or Country) used to filter top-props")
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
        workers_default, runs, cpu = compute_shared_workers(lock_path, share=0.8)
        print(f"Workers:             {workers_default} (80% CPU partagé / {runs} runs, CPU={cpu})")
    
    full_graph_file = data_dir / f"{class_name}_full_graph.nq"
    use_full_graph = full_graph_file.exists()
    if not use_full_graph:
        candidates = sorted(data_dir.glob("*full_graph.nq"))
        if candidates:
            full_graph_file = candidates[0]
            use_full_graph = True
    
    # 1. Déterminer la source locale si disponible
    decompressed_files = []
    graph_file = work_dir / f"{class_name}.nq"
    if use_full_graph:
        graph_file = full_graph_file
        decompressed_files = [full_graph_file]
        print_color(f"  ✅ Full graph détecté: {full_graph_file}", Colors.GREEN)
    else:
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
        type_filter_iris = None
        if args.wdc_type:
            type_filter_iris = [normalize_wdc_type(args.wdc_type)]
        else:
            # default to schema.org/<ClassName> (http + https)
            type_filter_iris = [
                f"<http://schema.org/{class_name}>",
                f"<https://schema.org/{class_name}>",
            ]
        top_props_file = work_dir / "top-props.txt"
        if top_props_file.exists():
            top_props_file.unlink()
        if use_full_graph:
            scan_top_props_from_files(
                [graph_file],
                top_n=1000,
                parallel=True,
                workers=workers_override,
                lock_path=lock_path,
                progress_every=100,
                output_file=top_props_file,
                type_filter_iris=type_filter_iris,
            )
        else:
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
    if use_full_graph:
        wdc_map, matched_count = extract_unique_iris_from_graph(
            graph_file,
            pattern,
            collect_top_props=args.top_props,
            top_n=100,
            parallel=True,
            workers=workers_override,
            lock_path=lock_path,
            progress_every=100,
            wdc_value_is_wd_iri=args.wdc_value_is_wikidata,
        )
    else:
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
