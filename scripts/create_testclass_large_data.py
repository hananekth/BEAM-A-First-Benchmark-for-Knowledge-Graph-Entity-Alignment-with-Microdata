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


CLASS_NAME = "TestClassLarge"
WIKIDATA_CLASS = "Q34770"  # language
WIKIDATA_PROPERTY = "rdfs:label"
PREDICATE_HINT = "name"
DEFAULT_PARTS = 24
DEFAULT_NOISE_LINES_PER_PART = 12000

LANGUAGE_NAMES = [
    "Portuguese",
    "Vietnamese",
    "Indonesian",
    "Norwegian",
    "Bulgarian",
    "Hungarian",
    "Slovenian",
    "Estonian",
    "Lithuanian",
    "Croatian",
    "Serbian",
    "Macedonian",
    "Ukrainian",
    "Armenian",
    "Belarusian",
    "Azerbaijani",
    "Romanian",
    "Icelandic",
    "Mongolian",
    "Filipino",
    "Albanian",
    "Galician",
    "Cantonese",
    "Mandarin",
]


def _build_part_lines(index: int, name: str, total: int, noise_lines_per_part: int) -> str:
    node = f"_:tcl_{index:03d}"
    next_node = f"_:tcl_{(index + 1) % total:03d}"
    head = (
        f'{node} <http://schema.org/name> "{name}" .\n'
        f'{node} <http://schema.org/description> "{name} language node for benchmark testing" .\n'
        f"{node} <http://schema.org/relatedLink> {next_node} .\n"
    )
    filler_subject = f"_:tcl_noise_{index:03d}"
    filler_line = f'{filler_subject} <http://schema.org/description> "TestClassLarge filler triple for runtime control." .\n'
    if noise_lines_per_part <= 0:
        return head
    return head + (filler_line * noise_lines_per_part)


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


def install_large_testclass(
    target_dir: Path,
    force: bool = False,
    parts: int = DEFAULT_PARTS,
    noise_lines_per_part: int = DEFAULT_NOISE_LINES_PER_PART,
):
    class_dir = target_dir / CLASS_NAME
    class_dir.mkdir(parents=True, exist_ok=True)

    written = []
    total = max(1, int(parts))
    for i in range(total):
        language_name = LANGUAGE_NAMES[i % len(LANGUAGE_NAMES)]
        file_name = f"part_{i + 1:04d}.nq"
        fp = class_dir / file_name
        if fp.exists() and not force:
            continue
        fp.write_text(_build_part_lines(i, language_name, total, int(noise_lines_per_part)), encoding="utf-8")
        written.append(fp)

    if written:
        cache_dir = class_dir / "align_cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)

    part_files = sorted(class_dir.glob("part_*.nq"))
    total_size = 0
    for fp in part_files:
        try:
            total_size += fp.stat().st_size
        except Exception:
            pass

    db.init_db()
    db.upsert_wdc_classes(
        [
            {
                "class_name": CLASS_NAME,
                "num_parts": len(part_files),
                "size_human": _format_size(total_size),
            }
        ]
    )

    return class_dir, written, len(part_files), total_size


def main():
    parser = argparse.ArgumentParser(
        description="Create a larger local TestClassLarge dataset intended for 2-5 minute benchmark runs."
    )
    parser.add_argument("--target", default="Download", help="Root download directory (default: Download)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing part files")
    parser.add_argument(
        "--parts",
        type=int,
        default=DEFAULT_PARTS,
        help=f"Number of part files (default: {DEFAULT_PARTS})",
    )
    parser.add_argument(
        "--noise-lines-per-part",
        type=int,
        default=DEFAULT_NOISE_LINES_PER_PART,
        help=f"Additional filler triples per part to control runtime (default: {DEFAULT_NOISE_LINES_PER_PART})",
    )
    args = parser.parse_args()

    class_dir, written, parts_count, total_size = install_large_testclass(
        Path(args.target),
        force=args.force,
        parts=args.parts,
        noise_lines_per_part=args.noise_lines_per_part,
    )
    print(f"[OK] {CLASS_NAME} ready at: {class_dir}")
    print(f"[OK] Parts: {parts_count} | Size: {_format_size(total_size)}")
    print(f"[OK] Runtime tuning: parts={args.parts}, noise_lines_per_part={args.noise_lines_per_part}")
    print(f"[OK] Equivalent Wikidata class: {WIKIDATA_CLASS} (language)")
    print("[OK] Suggested settings:")
    print(f"     class_name={CLASS_NAME}")
    print("     parts_spec=all")
    print(f"     wdc_predicate_pattern={PREDICATE_HINT}")
    print(f"     wikidata_property={WIKIDATA_PROPERTY}")
    print("     wdc_value_is_wikidata=false")
    print(f"     wkd_class={WIKIDATA_CLASS}")
    print("     max_depth=0")
    print("     force_align=true")
    print("     use_local_only=true")
    if written:
        print("[OK] Files written:")
        for fp in written[:5]:
            print(f"     - {fp.name}")
        if len(written) > 5:
            print(f"     - ... ({len(written) - 5} more)")
    else:
        print("[OK] No files rewritten (already present). Use --force to refresh.")


if __name__ == "__main__":
    main()
