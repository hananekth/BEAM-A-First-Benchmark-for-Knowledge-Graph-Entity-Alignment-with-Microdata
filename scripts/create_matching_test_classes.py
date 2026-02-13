#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beam import db


@dataclass(frozen=True)
class TestClassSpec:
    class_name: str
    predicate_uri: str
    predicate_hint: str
    wikidata_property: str
    wkd_class: str
    wdc_value_is_wikidata: bool
    values: tuple[str, ...]


SPECS = (
    TestClassSpec(
        class_name="TestClassLabel",
        predicate_uri="http://schema.org/name",
        predicate_hint="name",
        wikidata_property="rdfs:label",
        wkd_class="Q34770",
        wdc_value_is_wikidata=False,
        values=("Portuguese", "Vietnamese", "Indonesian"),
    ),
    TestClassSpec(
        class_name="TestClassIdentifier",
        predicate_uri="http://schema.org/eidr",
        predicate_hint="eidr",
        wikidata_property="wdt:P2704",
        wkd_class="Q11424",
        wdc_value_is_wikidata=False,
        values=(
            "10.5240/BFFE-118C-FD54-C5ED-5389-I",
            "10.5240/967C-2F9A-D813-DA04-DFD2-E",
            "10.5240/2A80-10C0-2A62-04B2-A005-D",
        ),
    ),
    TestClassSpec(
        class_name="TestClassWikidataUrl",
        predicate_uri="http://schema.org/url",
        predicate_hint="url",
        wikidata_property="wdt:P31",
        wkd_class="Q515",
        wdc_value_is_wikidata=True,
        values=(
            "http://www.wikidata.org/entity/Q90",
            "http://www.wikidata.org/entity/Q64",
            "http://www.wikidata.org/entity/Q1490",
        ),
    ),
    TestClassSpec(
        class_name="TestClassWikidataSameAs",
        predicate_uri="http://www.w3.org/2002/07/owl#sameAs",
        predicate_hint="sameas",
        wikidata_property="wdt:P31",
        wkd_class="Q6256",
        wdc_value_is_wikidata=True,
        values=(
            "http://www.wikidata.org/entity/Q142",
            "http://www.wikidata.org/entity/Q183",
            "http://www.wikidata.org/entity/Q17",
        ),
    ),
)


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


def _part_content(spec: TestClassSpec, idx: int) -> str:
    subject = f"_:tc_{spec.class_name.lower()}_{idx:03d}"
    next_subject = f"_:tc_{spec.class_name.lower()}_{(idx + 1) % len(spec.values):03d}"
    value = spec.values[idx]
    return (
        f'{subject} <{spec.predicate_uri}> "{value}" .\n'
        f'{subject} <http://schema.org/description> "{spec.class_name} node {idx + 1}" .\n'
        f"{subject} <http://schema.org/relatedLink> {next_subject} .\n"
    )


def install_classes(target_root: Path, force: bool = False):
    db_rows = []
    created_files: list[Path] = []
    for spec in SPECS:
        class_dir = target_root / spec.class_name
        class_dir.mkdir(parents=True, exist_ok=True)

        part_files = []
        class_created_files: list[Path] = []
        for i in range(len(spec.values)):
            fp = class_dir / f"part_{i + 1:04d}.nq"
            if force or not fp.exists():
                fp.write_text(_part_content(spec, i), encoding="utf-8")
                created_files.append(fp)
                class_created_files.append(fp)
            part_files.append(fp)

        if class_created_files:
            cache_dir = class_dir / "align_cache"
            if cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)

        total_size = 0
        for fp in part_files:
            try:
                total_size += fp.stat().st_size
            except Exception:
                pass

        db_rows.append(
            {
                "class_name": spec.class_name,
                "num_parts": len(part_files),
                "size_human": _fmt_size(total_size),
            }
        )

    db.init_db()
    db.upsert_wdc_classes(db_rows)
    return db_rows, created_files


def main():
    parser = argparse.ArgumentParser(
        description="Create multiple local test classes for label/identifier/url/sameAs matching presets."
    )
    parser.add_argument("--target", default="Download", help="Download root directory (default: Download)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing part files")
    args = parser.parse_args()

    rows, files = install_classes(Path(args.target), force=args.force)
    print("[OK] Matching test classes installed:")
    for r in rows:
        print(f"     - {r['class_name']} ({r['num_parts']} parts, {r['size_human']})")
    if files:
        print(f"[OK] Files written: {len(files)}")
    else:
        print("[OK] No files rewritten (already present). Use --force to refresh.")

    print("[OK] Preset mapping:")
    print("     TestClassLabel -> name + rdfs:label + Q34770")
    print("     TestClassIdentifier -> eidr + wdt:P2704 + Q11424")
    print("     TestClassWikidataUrl -> url + wdt:P31 + Q515 + wdc_value_is_wikidata=true")
    print("     TestClassWikidataSameAs -> sameas + wdt:P31 + Q6256 + wdc_value_is_wikidata=true")


if __name__ == "__main__":
    main()
