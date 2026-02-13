#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beam import db


TEST_CLASS_NAME = "TestClass"
TEST_WIKIDATA_CLASS = "Q34770"  # language


def _part_1() -> str:
    return (
        "_:tc_portuguese <http://schema.org/name> \"Portuguese\" .\n"
        "_:tc_portuguese <http://schema.org/description> \"Portuguese language sample node\" .\n"
        "_:tc_portuguese <http://schema.org/relatedLink> _:tc_vietnamese .\n"
    )


def _part_2() -> str:
    return (
        "_:tc_vietnamese <http://schema.org/name> \"Vietnamese\" .\n"
        "_:tc_vietnamese <http://schema.org/description> \"Vietnamese language sample node\" .\n"
        "_:tc_vietnamese <http://schema.org/relatedLink> _:tc_indonesian .\n"
    )


def _part_3() -> str:
    return (
        "_:tc_indonesian <http://schema.org/name> \"Indonesian\" .\n"
        "_:tc_indonesian <http://schema.org/description> \"Indonesian language sample node\" .\n"
        "_:tc_indonesian <http://schema.org/relatedLink> _:tc_portuguese .\n"
    )


PARTS = {
    "part_0001.nq": _part_1,
    "part_0002.nq": _part_2,
    "part_0003.nq": _part_3,
}


def install_testclass(target_dir: Path, force: bool = False):
    class_dir = target_dir / TEST_CLASS_NAME
    class_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for file_name, producer in PARTS.items():
        fp = class_dir / file_name
        if fp.exists() and not force:
            continue
        fp.write_text(producer(), encoding="utf-8")
        written.append(fp)

    if written:
        cache_dir = class_dir / "align_cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)

    total_size = 0
    for fp in class_dir.glob("part_*.nq"):
        try:
            total_size += fp.stat().st_size
        except Exception:
            pass

    db.init_db()
    db.upsert_wdc_classes(
        [
            {
                "class_name": TEST_CLASS_NAME,
                "num_parts": len(list(class_dir.glob("part_*.nq"))),
                "size_human": _format_size(total_size),
            }
        ]
    )

    return class_dir, written


def _format_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    n = float(max(0, num_bytes))
    for unit in units:
        if n < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{num_bytes} B"


def main():
    parser = argparse.ArgumentParser(description="Create a tiny local TestClass dataset for quick BEAM runs.")
    parser.add_argument("--target", default="Download", help="Root download directory (default: Download)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing part files")
    args = parser.parse_args()

    class_dir, written = install_testclass(Path(args.target), force=args.force)
    print(f"[OK] {TEST_CLASS_NAME} ready at: {class_dir}")
    print(f"[OK] Equivalent Wikidata class: {TEST_WIKIDATA_CLASS} (language)")
    print("[OK] Suggested quick settings:")
    print("     class_name=TestClass")
    print("     parts_spec=all")
    print("     wdc_predicate_pattern=name")
    print("     wikidata_property=rdfs:label")
    print("     wdc_value_is_wikidata=false")
    print("     wkd_class=Q34770")
    print("     max_depth=0")
    print("     use_local_only=true")
    if written:
        print("[OK] Files written:")
        for fp in written:
            print(f"     - {fp.name}")
    else:
        print("[OK] No files rewritten (already present). Use --force to refresh.")


if __name__ == "__main__":
    main()
