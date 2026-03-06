import json
import os
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

WDC_STATS_URL = "https://webdatacommons.org/structureddata/2024-12/stats/schema_org_subsets.html"
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[1] / "catalog" / "wdc_classes_catalog.json"

_CLASS_LINK_RE = re.compile(r"schema\.org/[^/]+$", re.IGNORECASE)
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)", re.IGNORECASE)
_PARTS_RE = re.compile(r"\((\d+)\)")


def _resolve_catalog_path(catalog_path=None):
    override = str(os.environ.get("WDC_CLASSES_CATALOG_PATH") or "").strip()
    if catalog_path:
        return Path(catalog_path)
    if override:
        return Path(override)
    return DEFAULT_CATALOG_PATH


def _normalize_catalog_rows(rows):
    out = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        class_name = str(row.get("class_name") or "").strip()
        if not class_name or class_name in seen:
            continue
        num_parts = row.get("num_parts")
        try:
            if num_parts is not None and str(num_parts).strip() != "":
                num_parts = int(num_parts)
            else:
                num_parts = None
        except Exception:
            num_parts = None
        size_human = row.get("size_human")
        size_human = str(size_human).strip() if size_human is not None else None
        if size_human == "":
            size_human = None
        out.append(
            {
                "class_name": class_name,
                "num_parts": num_parts,
                "size_human": size_human,
            }
        )
        seen.add(class_name)
    return out


def load_wdc_classes_catalog(catalog_path=None):
    path = _resolve_catalog_path(catalog_path)
    if not path.exists() or not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8") or "[]")
    if isinstance(payload, dict):
        rows = payload.get("classes")
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    return _normalize_catalog_rows(rows)


def save_wdc_classes_catalog(rows, catalog_path=None):
    path = _resolve_catalog_path(catalog_path)
    normalized = _normalize_catalog_rows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(normalized, ensure_ascii=True, indent=2)
    path.write_text(payload + "\n", encoding="utf-8")
    return path


def _is_class_anchor(a):
    if not a or not a.get("href"):
        return False
    if not _CLASS_LINK_RE.search(a["href"]):
        return False
    text = a.get_text(strip=True)
    if not text:
        return False
    if text.startswith("http://") or text.startswith("https://"):
        return False
    if "/" in text:
        return False
    return True


def _next_class_anchor(start):
    for el in start.next_elements:
        if getattr(el, "name", None) == "a" and _is_class_anchor(el):
            return el
    return None


def _scan_segment(class_anchor):
    parts = None
    size_human = None
    download_link = None

    stop_at = _next_class_anchor(class_anchor)
    for el in class_anchor.next_elements:
        if el == stop_at:
            break
        if getattr(el, "name", None) == "a":
            href = el.get("href", "")
            if "data.dws.informatik.uni-mannheim.de" in href:
                if el.get_text(strip=True) == class_anchor.get_text(strip=True):
                    download_link = el
        if isinstance(el, str):
            if size_human is None:
                m = _SIZE_RE.search(el)
                if m:
                    size_human = f"{m.group(1)} {m.group(2).upper()}"
            if parts is None:
                m = _PARTS_RE.search(el)
                if m:
                    parts = int(m.group(1))

    # Prefer parts found nearest to download link if available
    if download_link:
        for el in download_link.previous_elements:
            if el == class_anchor:
                break
            if isinstance(el, str):
                m = _PARTS_RE.search(el)
                if m:
                    parts = int(m.group(1))
                    break

    return parts, size_human


def fetch_wdc_classes():
    resp = requests.get(WDC_STATS_URL, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    for a in soup.find_all("a", href=True):
        if not _is_class_anchor(a):
            continue
        class_name = a.get_text(strip=True)
        num_parts, size_human = _scan_segment(a)
        if num_parts is None and size_human is None:
            # Skip related-class links without download info
            continue
        results.append(
            {
                "class_name": class_name,
                "num_parts": num_parts,
                "size_human": size_human,
            }
        )

    # Deduplicate by class_name (keep first with parts if available)
    dedup = {}
    for r in results:
        name = r["class_name"]
        if name not in dedup:
            dedup[name] = r
        else:
            if dedup[name].get("num_parts") is None and r.get("num_parts") is not None:
                dedup[name] = r
    return list(dedup.values())
