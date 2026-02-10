#!/usr/bin/env python3
import argparse
import os
import re
import json
import time
import fcntl
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

PREFIXES = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "schema": "http://schema.org/",
    "wd": "http://www.wikidata.org/entity/",
    "wdt": "http://www.wikidata.org/prop/direct/",
}

# ---- Embed your SPARQL query here ----
SPARQL_QUERY = """
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX schema: <http://schema.org/>

SELECT ?p (COUNT(*) AS ?freq) WHERE {
  ?s ?p ?o .
}
GROUP BY ?p
ORDER BY DESC(?freq)
LIMIT 100
"""
# -------------------------------------

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


def expand_term(term, prefixes=None):
    prefixes = prefixes or PREFIXES
    if term is None:
        return None
    if term.startswith("?"):
        return term
    if term.startswith("<") and term.endswith(">"):
        return term
    if term.startswith('"'):
        return term
    if ":" in term:
        prefix, local = term.split(":", 1)
        if prefix in prefixes:
            return f"<{prefixes[prefix]}{local}>"
    return f"<{term}>"


def match_pattern(triple, pattern, binding):
    s, p, o = triple
    ps, pp, po = pattern
    for term, value in ((ps, s), (pp, p), (po, o)):
        if term.startswith("?"):
            bound = binding.get(term)
            if bound is None:
                binding[term] = value
            elif bound != value:
                return False
        else:
            if term != value:
                return False
    return True


def iter_triples(files, progress_every=0):
    total_bytes = sum(os.path.getsize(p) for p in files)
    done_bytes = 0
    start_ts = time.time()
    if progress_every:
        print(f"⏳ Progress: 0.0% | ETA: N/A", flush=True)
    for path in files:
        file_base = done_bytes
        bytes_read = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                bytes_read += len(line)
                parsed = parse_nq_or_nt(line)
                if parsed:
                    yield parsed
                if progress_every and bytes_read % progress_every == 0:
                    done_bytes = file_base + bytes_read
                    elapsed = time.time() - start_ts
                    rate = done_bytes / elapsed if elapsed > 0 else 0
                    rem = (total_bytes - done_bytes) / rate if rate > 0 else 0
                    pct = 0 if total_bytes <= 0 else (done_bytes / total_bytes) * 100
                    print(f"\r⏳ Progress: {pct:5.1f}% | ETA: {int(rem)}s", end='', flush=True)
        done_bytes = file_base + bytes_read
    if progress_every:
        print(f"\r⏳ Progress: 100.0% | ETA: 0s", flush=True)


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


def _count_props_in_file(path, pattern):
    counts = {}
    size = os.path.getsize(path)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            parsed = parse_nq_or_nt(line)
            if not parsed:
                continue
            s, p, o = parsed
            # only match if triple pattern matches
            b = {}
            if match_pattern((s, p, o), pattern, b):
                counts[p] = counts.get(p, 0) + 1
    return counts, size


def parse_sparql(query_text):
    prefixes = dict(PREFIXES)
    select_vars = []
    where_patterns = []
    limit = None
    group_by = []
    order_by = None  # ("var", "asc"/"desc")
    aggregates = {}  # var -> ("count", "*")

    for line in query_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("prefix"):
            # PREFIX pfx: <iri>
            parts = line.split()
            if len(parts) >= 3:
                pfx = parts[1].rstrip(":")
                iri = parts[2].strip("<>")
                prefixes[pfx] = iri
        if line.lower().startswith("limit"):
            try:
                limit = int(line.split()[1])
            except Exception:
                limit = None
        if line.lower().startswith("group by"):
            parts = line.split()
            group_by = [p for p in parts[2:] if p.startswith("?")]
        if line.lower().startswith("order by"):
            parts = line.split()
            if len(parts) >= 3 and parts[2].startswith("?"):
                order_by = (parts[2], "asc")
            if len(parts) >= 4 and parts[2].lower().startswith("desc"):
                order_by = (parts[3].strip("()"), "desc")

    # Extract SELECT vars (and aggregates)
    m = re.search(r"\bselect\b\s+(.*?)\s+\bwhere\b", query_text, flags=re.I | re.S)
    if m:
        select_chunk = m.group(1)
        # detect COUNT(*) AS ?var
        for agg in re.finditer(r"COUNT\s*\(\s*\*\s*\)\s+AS\s+(\?\w+)", select_chunk, flags=re.I):
            aggregates[agg.group(1)] = ("count", "*")
        select_vars = []
        select_vars.extend(re.findall(r"\?\w+", select_chunk))

    # Extract WHERE block (simple triple patterns)
    m = re.search(r"\bwhere\b\s*\{(.*?)\}", query_text, flags=re.I | re.S)
    if m:
        body = m.group(1)
        for stmt in body.split("."):
            stmt = stmt.strip()
            if not stmt:
                continue
            head = stmt.split()[0].lower() if stmt.split() else ""
            if head in {"filter", "optional", "bind", "values"}:
                continue
            parts = stmt.split()
            if len(parts) >= 3:
                s, p, o = parts[0], parts[1], " ".join(parts[2:])
                where_patterns.append((expand_term(s, prefixes), expand_term(p, prefixes), expand_term(o, prefixes)))

    return select_vars, where_patterns, limit, group_by, order_by, aggregates


def run_query(files, query_text, progress_every=0):
    select, where, limit, group_by, order_by, aggregates = parse_sparql(query_text)
    if not select or not where:
        raise SystemExit("[ERR] Query must have SELECT and WHERE with triple patterns.")

    patterns = list(where)

    # Fast path: COUNT(*) GROUP BY ?p over single triple pattern
    if aggregates and ("?p" in group_by) and len(patterns) == 1:
        s, p, o = patterns[0]
        if p == "?p":
            # Parallel over files
            counts = {}
            total_bytes = sum(os.path.getsize(p) for p in files)
            done_bytes = 0
            start_ts = time.time()
            if progress_every:
                print(f"⏳ Progress: 0.0% | ETA: N/A", flush=True)
            n_workers, _runs, _cpu = compute_shared_workers(Path("Download") / ".workers.lock", share=0.8)
            with ProcessPoolExecutor(max_workers=n_workers) as ex:
                futures = [ex.submit(_count_props_in_file, p, patterns[0]) for p in files]
                for fut in as_completed(futures):
                    local, size = fut.result()
                    for pred, cnt in local.items():
                        counts[pred] = counts.get(pred, 0) + cnt
                    done_bytes += size
                    if progress_every:
                        elapsed = time.time() - start_ts
                        rate = done_bytes / elapsed if elapsed > 0 else 0
                        rem = (total_bytes - done_bytes) / rate if rate > 0 else 0
                        pct = 0 if total_bytes <= 0 else (done_bytes / total_bytes) * 100
                        print(f"\r⏳ Progress: {pct:5.1f}% | ETA: {int(rem)}s", end='', flush=True)
            if progress_every:
                print(f"\r⏳ Progress: 100.0% | ETA: 0s", flush=True)
            rows = []
            for pred, cnt in counts.items():
                row = {"?p": pred}
                for agg_var in aggregates:
                    row[agg_var] = str(cnt)
                rows.append(row)
            if order_by and order_by[0] in aggregates:
                rows.sort(key=lambda r: int(r.get(order_by[0], "0")), reverse=(order_by[1] == "desc"))
            if limit:
                rows = rows[:limit]
            return rows, select

    # Naive join: scan files for each pattern in order
    bindings = [{}]
    for idx, pat in enumerate(patterns):
        new_bindings = []
        for triple in iter_triples(files, progress_every=progress_every):
            for b in bindings:
                bcopy = dict(b)
                if match_pattern(triple, pat, bcopy):
                    new_bindings.append(bcopy)
        bindings = new_bindings
        if not bindings:
            break

    # Build results
    results = []
    for b in bindings:
        row = {var: b.get(var, "") for var in select}
        results.append(row)
        if limit and len(results) >= limit:
            break
    # ORDER BY / LIMIT (non-aggregate)
    if order_by and order_by[0] in select:
        rows_sorted = sorted(results, key=lambda r: r.get(order_by[0], ""), reverse=(order_by[1] == "desc"))
        results = rows_sorted
    if limit:
        results = results[:limit]
    return results, select


def print_table(rows, columns):
    if not rows:
        print("No results.")
        return
    print("\t".join(columns))
    for r in rows:
        print("\t".join(r.get(c, "") for c in columns))


def main():
    parser = argparse.ArgumentParser(description="Run an embedded SPARQL-like query on Download/<Class>/part_*")
    parser.add_argument("class_name")
    args = parser.parse_args()

    base = Path("Download") / args.class_name
    if not base.exists():
        raise SystemExit(f"[ERR] Missing folder: {base}")

    parts = sorted(base.glob("part_*"))
    if not parts:
        raise SystemExit(f"[ERR] No part_* files found in {base}")

    rows, select_vars = run_query(parts, SPARQL_QUERY, progress_every=5_000_000)

    out_dir = base
    json_path = out_dir / "query_results.json"
    tsv_path = out_dir / "query_results.tsv"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {"head": {"vars": select_vars}, "results": {"bindings": rows}},
            f,
            indent=2,
        )
    with open(tsv_path, "w", encoding="utf-8") as f:
        f.write("\t".join(select_vars) + "\n")
        for r in rows:
            f.write("\t".join(r.get(c, "") for c in select_vars) + "\n")

    print_table(rows, select_vars)
    print(f"\n✅ Saved: {json_path}")
    print(f"✅ Saved: {tsv_path}")


if __name__ == "__main__":
    main()
