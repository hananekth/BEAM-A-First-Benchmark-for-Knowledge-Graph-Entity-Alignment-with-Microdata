import re
import requests
from bs4 import BeautifulSoup

WDC_STATS_URL = "https://webdatacommons.org/structureddata/2024-12/stats/schema_org_subsets.html"

_CLASS_LINK_RE = re.compile(r"schema\.org/[^/]+$", re.IGNORECASE)
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)", re.IGNORECASE)
_PARTS_RE = re.compile(r"\((\d+)\)")


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
