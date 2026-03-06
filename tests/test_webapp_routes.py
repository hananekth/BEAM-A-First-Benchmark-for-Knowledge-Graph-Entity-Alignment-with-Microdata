import importlib
import json
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient


def _write_variant_files(variant_dir: Path, links_count: int = 2):
    variant_dir.mkdir(parents=True, exist_ok=True)
    ent_links_lines = ["wdc_iri\twikidata_uri\n"]
    for idx in range(max(0, int(links_count))):
        ent_links_lines.append(
            f"http://example.org/wdc/entity{idx + 1}\thttp://www.wikidata.org/entity/Q{515 + idx}\n"
        )
    (variant_dir / "ent_links").write_text("".join(ent_links_lines), encoding="utf-8")
    (variant_dir / "attr_triples_1").write_text("s\tp\to\n", encoding="utf-8")
    (variant_dir / "rel_triples_1").write_text("s\tp\to\n", encoding="utf-8")
    (variant_dir / "attr_triples_2").write_text("s\tp\to\n", encoding="utf-8")
    (variant_dir / "rel_triples_2").write_text("s\tp\to\n", encoding="utf-8")
    (variant_dir / "prop_stats_wdc.tsv").write_text(
        "property\tcount\nhttp://schema.org/name\t2\nhttp://schema.org/url\t2\n",
        encoding="utf-8",
    )
    (variant_dir / "prop_stats_wd.tsv").write_text(
        "property\tcount\nhttp://www.wikidata.org/prop/direct/P31\t2\n",
        encoding="utf-8",
    )


def _make_build_tree(build_root: Path, links_count: int = 2, class_name: str = "TestClass"):
    build_root.mkdir(parents=True, exist_ok=True)
    (build_root / "BUILD_DONE").write_text("2026-02-12 12:00:00", encoding="utf-8")
    (build_root / "BUILD_CONFIG.json").write_text(
        json.dumps(
            {
                "matching_mode": "property",
                "class_name": class_name,
                "parts_spec": "all",
                "wdc_predicate_pattern": "name",
                "wikidata_property": "rdfs:label",
                "wkd_class": "Q515",
                "ignore_chars": "spaces;-;.",
                "force_align": False,
                "use_local_only": True,
                "force_one_to_one_links": False,
                "dedup_wdc_exact_subgraph_by_link_value": False,
                "build_name": build_root.name,
                "result_path": str(build_root),
                "parts_count": 2,
                "parts_total_size_human": "2.0 MB",
                "parts_manifest": [
                    {"name": "part_0001.nq", "size_human": "1.0 MB"},
                    {"name": "part_0002.nq", "size_human": "1.0 MB"},
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_variant_files(build_root / "with_link_code", links_count=links_count)
    _write_variant_files(build_root / "without_link_code", links_count=links_count)


def _write_link_explorer_variant(variant_dir: Path):
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / "ent_links").write_text(
        "wdc_iri\twikidata_uri\n"
        "http://example.org/wdc/entity1\thttp://www.wikidata.org/entity/Q100\n",
        encoding="utf-8",
    )
    (variant_dir / "attr_triples_1").write_text(
        'http://example.org/wdc/entity1\thttp://schema.org/name\t"Alpha City"\n'
        'http://example.org/wdc/entity1\thttp://schema.org/telephone\t"+33 1 23 45 67"\n',
        encoding="utf-8",
    )
    (variant_dir / "rel_triples_1").write_text(
        "http://example.org/wdc/entity1\thttp://schema.org/sameAs\thttp://www.wikidata.org/entity/Q100\n",
        encoding="utf-8",
    )
    (variant_dir / "attr_triples_2").write_text(
        'http://www.wikidata.org/entity/Q100\thttp://www.w3.org/2000/01/rdf-schema#label\t"Alpha City"\n'
        'http://www.wikidata.org/entity/Q100\thttp://www.wikidata.org/prop/direct/P1329\t"+331234567"\n',
        encoding="utf-8",
    )
    (variant_dir / "rel_triples_2").write_text(
        "http://www.wikidata.org/entity/Q100\thttp://www.wikidata.org/prop/direct/P31\thttp://www.wikidata.org/entity/Q515\n",
        encoding="utf-8",
    )
    (variant_dir / "prop_stats_wdc.tsv").write_text(
        "property\tcount\nhttp://schema.org/name\t1\nhttp://schema.org/telephone\t1\n",
        encoding="utf-8",
    )
    (variant_dir / "prop_stats_wd.tsv").write_text(
        "predicate\tcount\tlabel\tdescription\n"
        "http://www.w3.org/2000/01/rdf-schema#label\t1\tlabel\titem label\n"
        "http://www.wikidata.org/prop/direct/p1329\t1\tphone number\ttelephone number of subject\n"
        "http://www.wikidata.org/prop/direct/p31\t1\tinstance of\tthat class of which this subject is a particular example\n",
        encoding="utf-8",
    )


def _write_link_explorer_value_fallback_variant(variant_dir: Path):
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / "ent_links").write_text(
        "wdc_iri\twikidata_uri\n"
        "http://example.org/wdc/entity-snarc\thttp://www.wikidata.org/entity/Q145892\n",
        encoding="utf-8",
    )
    (variant_dir / "attr_triples_1").write_text(
        'http://example.org/wdc/entity-snarc\thttp://example.org/vocab/snarcRef\t"SNARC-7788"\n',
        encoding="utf-8",
    )
    (variant_dir / "rel_triples_1").write_text("", encoding="utf-8")
    (variant_dir / "attr_triples_2").write_text(
        'http://www.wikidata.org/entity/Q145892\thttp://www.wikidata.org/prop/direct/P12749\t"SNARC7788"\n',
        encoding="utf-8",
    )
    (variant_dir / "rel_triples_2").write_text("", encoding="utf-8")
    (variant_dir / "prop_stats_wdc.tsv").write_text(
        "predicate\tcount\tlabel\tdescription\n"
        "http://example.org/vocab/snarcRef\t1\tSNARC ref\tcustom id in source catalog\n",
        encoding="utf-8",
    )
    (variant_dir / "prop_stats_wd.tsv").write_text(
        "predicate\tcount\tlabel\tdescription\n"
        "http://www.wikidata.org/prop/direct/P12749\t1\tSNARC ID\tunique identifier for people, places and organisations represented in Welsh collections\n",
        encoding="utf-8",
    )


def _write_link_explorer_value_fallback_multivalue_variant(variant_dir: Path):
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / "ent_links").write_text(
        "wdc_iri\twikidata_uri\n"
        "http://example.org/wdc/entity-snarc-multi\thttp://www.wikidata.org/entity/Q145892\n",
        encoding="utf-8",
    )
    (variant_dir / "attr_triples_1").write_text(
        'http://example.org/wdc/entity-snarc-multi\thttp://example.org/vocab/snarcRef\t"SNARC-7788"\n',
        encoding="utf-8",
    )
    (variant_dir / "rel_triples_1").write_text("", encoding="utf-8")
    (variant_dir / "attr_triples_2").write_text(
        'http://www.wikidata.org/entity/Q145892\thttp://www.wikidata.org/prop/direct/P12749\t"SNARC7788"\n'
        'http://www.wikidata.org/entity/Q145892\thttp://www.wikidata.org/prop/direct/P12749\t"SNARC-0000"\n'
        'http://www.wikidata.org/entity/Q145892\thttp://www.wikidata.org/prop/direct/P12749\t"SNARC-9999"\n',
        encoding="utf-8",
    )
    (variant_dir / "rel_triples_2").write_text("", encoding="utf-8")
    (variant_dir / "prop_stats_wdc.tsv").write_text(
        "predicate\tcount\tlabel\tdescription\n"
        "http://example.org/vocab/snarcRef\t1\tSNARC ref\tcustom id in source catalog\n",
        encoding="utf-8",
    )
    (variant_dir / "prop_stats_wd.tsv").write_text(
        "predicate\tcount\tlabel\tdescription\n"
        "http://www.wikidata.org/prop/direct/P12749\t3\tSNARC ID\tunique identifier for people, places and organisations represented in Welsh collections\n",
        encoding="utf-8",
    )


def _write_link_explorer_weak_numeric_variant(variant_dir: Path):
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / "ent_links").write_text(
        "wdc_iri\twikidata_uri\n"
        "http://example.org/wdc/entity-num\thttp://www.wikidata.org/entity/Q999\n",
        encoding="utf-8",
    )
    (variant_dir / "attr_triples_1").write_text(
        'http://example.org/wdc/entity-num\thttp://schema.org/aggregateRating\t"6"\n',
        encoding="utf-8",
    )
    (variant_dir / "rel_triples_1").write_text("", encoding="utf-8")
    (variant_dir / "attr_triples_2").write_text(
        'http://www.wikidata.org/entity/Q999\thttp://schema.org/sitelinks\t"6"\n',
        encoding="utf-8",
    )
    (variant_dir / "rel_triples_2").write_text("", encoding="utf-8")
    (variant_dir / "prop_stats_wdc.tsv").write_text(
        "predicate\tcount\tlabel\tdescription\n"
        "http://schema.org/aggregateRating\t1\taggregate rating\taverage rating\n",
        encoding="utf-8",
    )
    (variant_dir / "prop_stats_wd.tsv").write_text(
        "predicate\tcount\tlabel\tdescription\n"
        "http://schema.org/sitelinks\t1\tsitelinks\tnumber of sitelinks\n",
        encoding="utf-8",
    )


def _client_with_test_classes(monkeypatch, test_wdc_classes):
    import beam.db as beam_db
    import webapp.main as web_main

    catalog_path = Path("wdc_classes_catalog.test.json")
    catalog_path.write_text(json.dumps(list(test_wdc_classes)), encoding="utf-8")
    monkeypatch.setenv("WDC_CLASSES_CATALOG_PATH", str(catalog_path))

    importlib.reload(web_main)
    monkeypatch.setattr(web_main.db, "DB_PATH", beam_db.DB_PATH)
    monkeypatch.setattr(web_main, "fetch_wdc_classes", lambda: list(test_wdc_classes))
    return TestClient(web_main.app), web_main


def test_index_populates_testclass_list(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    with client:
        resp = client.get("/")

    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    mode_select = soup.find("select", {"id": "matching-mode-select"})
    assert mode_select is not None
    mode_values = {opt.get("value", "") for opt in mode_select.find_all("option")}
    assert {"property", "sameas"} <= mode_values
    assert "identifier" not in mode_values
    pattern_list = soup.find("div", {"id": "wdc-pattern-list"})
    assert pattern_list is not None
    pattern_hidden_input = soup.find("input", {"id": "wdc-pattern-input"})
    assert pattern_hidden_input is not None
    assert pattern_hidden_input.get("type") == "hidden"
    pattern_add_btn = soup.find("button", {"id": "wdc-pattern-add-btn"})
    assert pattern_add_btn is not None
    assert soup.find("div", {"id": "ready-checklist"}) is not None
    assert soup.find("input", {"id": "history-search-input"}) is not None
    assert soup.find("select", {"id": "history-sort-select"}) is not None
    assert soup.find("form", {"id": "purge-low-links-form"}) is not None
    preset_select = soup.find("select", {"id": "preset-select"})
    assert preset_select is not None
    preset_values = {opt.get("value", "") for opt in preset_select.find_all("option")}
    assert "testclass_quick" not in preset_values
    class_select = soup.find("select", {"id": "class-name-select"})
    assert class_select is not None
    class_values = {opt.get("value", "") for opt in class_select.find_all("option")}
    assert "TestClass" not in class_values

    rows = [dict(r) for r in web_main.db.list_wdc_classes()]
    assert any(row["class_name"] == "TestClass" for row in rows)


def test_index_no_preset_defaults_parts_spec_to_all(monkeypatch, test_wdc_classes):
    client, _web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    with client:
        resp = client.get("/")

    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    parts_input = soup.find("input", {"id": "parts-spec-input"})
    assert parts_input is not None
    assert parts_input.get("value") == "all"


def test_index_keeps_selected_preset_in_dropdown(monkeypatch, test_wdc_classes):
    client, _web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    with client:
        resp = client.get("/?test_mode=1&preset=testclass_quick")

    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    select = soup.find("select", {"id": "preset-select"})
    assert select is not None
    selected = select.find("option", {"value": "testclass_quick"})
    assert selected is not None
    assert selected.has_attr("selected")


def test_index_test_mode_shows_test_presets_only(monkeypatch, test_wdc_classes):
    client, _web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    with client:
        resp = client.get("/?test_mode=1")

    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    preset_select = soup.find("select", {"id": "preset-select"})
    assert preset_select is not None
    preset_values = {opt.get("value", "") for opt in preset_select.find_all("option")}
    assert "testclass_quick" in preset_values
    assert "code_movie" not in preset_values
    class_select = soup.find("select", {"id": "class-name-select"})
    assert class_select is not None
    class_values = {opt.get("value", "") for opt in class_select.find_all("option")}
    assert "TestClass" in class_values


def test_index_normal_mode_hides_test_jobs_and_history(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)

    test_build_name = "beam_20260212_hidden_test"
    prod_build_name = "beam_20260212_visible_prod"
    test_build_root = Path("data") / "TestClass" / test_build_name
    prod_build_root = Path("data") / "City" / prod_build_name
    _make_build_tree(test_build_root, class_name="TestClass")
    _make_build_tree(prod_build_root, class_name="City")

    test_job_id = web_main.db.insert_job({"class_name": "TestClass", "parts_spec": "all"})
    web_main.db.update_job(test_job_id, status="error", error_message="test job")
    prod_job_id = web_main.db.insert_job({"class_name": "City", "parts_spec": "all"})
    web_main.db.update_job(prod_job_id, status="error", error_message="prod job")

    with client:
        resp = client.get("/")

    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    build_cards = soup.select("#build-list .build")
    assert build_cards
    build_classes = [c.get("data-class-name", "") for c in build_cards]
    assert all(not cls.lower().startswith("testclass") for cls in build_classes)
    assert any(cls == "City" for cls in build_classes)

    job_cards = soup.select(".job[data-job-id]")
    assert job_cards
    job_classes = [c.get("data-class-name", "") for c in job_cards]
    assert all(not cls.lower().startswith("testclass") for cls in job_classes)
    assert any(cls == "City" for cls in job_classes)
    class_select = soup.find("select", {"id": "class-name-select"})
    assert class_select is not None
    class_values = {opt.get("value", "") for opt in class_select.find_all("option")}
    assert "TestClass" not in class_values


def test_refresh_classes_updates_cache(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    with client:
        resp = client.get("/refresh_classes", follow_redirects=False)

    assert resp.status_code == 303
    rows = [dict(r) for r in web_main.db.list_wdc_classes()]
    assert {r["class_name"] for r in rows} >= {"TestClass", "TestClassTwo"}


def test_refresh_classes_failure_keeps_existing_cache(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    web_main.db.upsert_wdc_classes(
        [
            {"class_name": "CachedOnly", "num_parts": 7, "size_human": "7.0 MB"},
        ]
    )
    monkeypatch.setattr(web_main, "fetch_wdc_classes", lambda: (_ for _ in ()).throw(RuntimeError("offline")))

    with client:
        resp = client.get("/refresh_classes", follow_redirects=False)

    assert resp.status_code == 303
    location = resp.headers.get("location") or ""
    assert "form_error=" in location
    rows = [dict(r) for r in web_main.db.list_wdc_classes()]
    assert any(r["class_name"] == "CachedOnly" for r in rows)


def test_index_discovers_local_testclass_parts(monkeypatch):
    import beam.db as beam_db
    import webapp.main as web_main

    class_dir = Path("Download") / "TestClass"
    class_dir.mkdir(parents=True, exist_ok=True)
    (class_dir / "part_0001.nq").write_text(
        "<http://example.org/testclass/entity/paris> <http://schema.org/url> \"http://www.wikidata.org/entity/Q90\" .\n",
        encoding="utf-8",
    )
    (class_dir / "part_0002.nq").write_text(
        "<http://example.org/testclass/entity/berlin> <http://schema.org/url> \"http://www.wikidata.org/entity/Q64\" .\n",
        encoding="utf-8",
    )

    importlib.reload(web_main)
    monkeypatch.setattr(web_main.db, "DB_PATH", beam_db.DB_PATH)
    monkeypatch.setattr(web_main, "fetch_wdc_classes", lambda: [])
    client = TestClient(web_main.app)
    with client:
        resp = client.get("/?test_mode=1")

    assert resp.status_code == 200
    assert "TestClass" in resp.text
    rows = [dict(r) for r in web_main.db.list_wdc_classes()]
    test_row = [r for r in rows if r["class_name"] == "TestClass"]
    assert len(test_row) == 1
    assert test_row[0]["num_parts"] == 2


def test_create_job_persists_params(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    form = {
        "matching_mode": "property",
        "class_name": "  TestClass  ",
        "parts_spec": "  all  ",
        "wdc_predicate_pattern": "  name  ",
        "wikidata_property": "  P31  ",
        "wkd_class": "  Q515  ",
        "ignore_chars": "  spaces;-;.  ",
        "force_align": "",
        "use_local_only": "",
        "force_one_to_one_links": "on",
        "dedup_wdc_exact_subgraph_by_link_value": "on",
    }
    with client:
        resp = client.post("/jobs", data=form, follow_redirects=False)

    assert resp.status_code == 303
    jobs = web_main.db.list_jobs(limit=1)
    assert len(jobs) == 1
    params = json.loads(jobs[0]["params_json"])
    assert params["class_name"] == "TestClass"
    assert params["parts_spec"] == "all"
    assert params["matching_mode"] == "property"
    assert params["wdc_predicate_pattern"] == "name"
    assert params["wikidata_property"] == "P31"
    assert params["force_one_to_one_links"] is True
    assert params["dedup_wdc_exact_subgraph_by_link_value"] is True


def test_create_job_requires_wikidata_property_when_not_url_mode(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    form = {
        "matching_mode": "property",
        "class_name": "TestClass",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "",
        "wkd_class": "Q515",
        "ignore_chars": "spaces;-;.",
    }
    with client:
        resp = client.post("/jobs", data=form, follow_redirects=False)

    assert resp.status_code == 303
    assert "form_error=" in (resp.headers.get("location") or "")
    jobs = web_main.db.list_jobs(limit=10)
    assert jobs == []


def test_create_job_url_mode_clears_wikidata_property(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    form = {
        "matching_mode": "sameas",
        "class_name": "TestClass",
        "parts_spec": "all",
        "wdc_predicate_pattern": "sameAs",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q486972",
        "ignore_chars": "spaces;-;.",
    }
    with client:
        resp = client.post("/jobs", data=form, follow_redirects=False)

    assert resp.status_code == 303
    jobs = web_main.db.list_jobs(limit=1)
    assert len(jobs) == 1
    params = json.loads(jobs[0]["params_json"])
    assert params["matching_mode"] == "sameas"
    assert params["wikidata_property"] == ""
    assert params["wkd_class"] == "Q486972"


def test_create_job_sameas_pattern_does_not_auto_enable_url_mode(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    form = {
        "matching_mode": "property",
        "class_name": "TestClass",
        "parts_spec": "all",
        "wdc_predicate_pattern": "sameAs",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q486972",
        "ignore_chars": "spaces;-;.",
    }
    with client:
        resp = client.post("/jobs", data=form, follow_redirects=False)

    assert resp.status_code == 303
    jobs = web_main.db.list_jobs(limit=1)
    assert len(jobs) == 1
    params = json.loads(jobs[0]["params_json"])
    assert params["matching_mode"] == "property"
    assert params["wikidata_property"] == "rdfs:label"
    assert params["ignore_chars"] == "spaces;-;."


def test_create_job_sameas_list_pattern_does_not_auto_enable_url_mode(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    form = {
        "matching_mode": "property",
        "class_name": "TestClass",
        "parts_spec": "all",
        "wdc_predicate_pattern": "sameAs, url",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q486972",
        "ignore_chars": "spaces;-;.",
    }
    with client:
        resp = client.post("/jobs", data=form, follow_redirects=False)

    assert resp.status_code == 303
    jobs = web_main.db.list_jobs(limit=1)
    assert len(jobs) == 1
    params = json.loads(jobs[0]["params_json"])
    assert params["matching_mode"] == "property"
    assert params["wikidata_property"] == "rdfs:label"
    assert params["ignore_chars"] == "spaces;-;."
    assert params["wdc_predicate_pattern"] == "sameAs, url"


def test_create_job_url_mode_requires_wikidata_class(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    form = {
        "matching_mode": "sameas",
        "class_name": "TestClass",
        "parts_spec": "all",
        "wdc_predicate_pattern": "sameAs",
        "wikidata_property": "rdfs:label",
        "wkd_class": "",
        "ignore_chars": "spaces;-;.",
    }
    with client:
        resp = client.post("/jobs", data=form, follow_redirects=False)

    assert resp.status_code == 303
    assert "form_error=" in (resp.headers.get("location") or "")
    assert web_main.db.list_jobs(limit=10) == []


def test_preflight_api_reports_matches(monkeypatch, test_wdc_classes):
    client, _web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    class_dir = Path("Download") / "TestClass"
    class_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        '<http://example.org/e1> <http://schema.org/name> "Paris" .\n',
        '<http://example.org/e2> <http://schema.org/name> "Berlin" .\n',
        '<http://example.org/e3> <http://schema.org/name> "Madrid" .\n',
        '<http://example.org/e4> <http://schema.org/name> "Rome" .\n',
        '<http://example.org/e5> <http://schema.org/name> "Lisbon" .\n',
        '<http://example.org/e6> <http://schema.org/name> "Vienna" .\n',
    ]
    (class_dir / "part_0001.nq").write_text("".join(lines), encoding="utf-8")

    with client:
        resp = client.get(
            "/api/preflight",
            params={
                "class_name": "TestClass",
                "parts_spec": "all",
                "matching_mode": "property",
                "wdc_predicate_pattern": "name",
                "ignore_chars": "spaces;-;.",
                "use_local_only": "true",
                "scan_limit_lines": "10000",
            },
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["matched_triples"] >= 6
    assert payload["distinct_values"] >= 6
    assert payload["risk"] == "low"
    assert payload["selected_files_count"] == 1


def test_create_job_does_not_block_high_risk_preflight(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    class_dir = Path("Download") / "TestClass"
    class_dir.mkdir(parents=True, exist_ok=True)
    (class_dir / "part_0001.nq").write_text(
        '<http://example.org/e1> <http://schema.org/url> "https://example.org/a" .\n',
        encoding="utf-8",
    )
    form = {
        "class_name": "TestClass",
        "parts_spec": "all",
        "wdc_predicate_pattern": "name",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q515",
        "ignore_chars": "spaces;-;.",
        "use_local_only": "on",
    }
    with client:
        resp = client.post("/jobs", data=form, follow_redirects=False)

    assert resp.status_code == 303
    jobs = web_main.db.list_jobs(limit=1)
    assert len(jobs) == 1


def test_builds_render_and_download(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120000"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)

    with client:
        home = client.get("/?test_mode=1")
        zipped = client.get(f"/builds/TestClass/{build_name}/download")

    assert home.status_code == 200
    assert "Entity Links" in home.text
    assert "Parts Used" in home.text
    assert "part_0001.nq" in home.text

    assert zipped.status_code == 200
    assert zipped.headers["content-type"].startswith("application/zip")


def test_history_card_exposes_build_detail_url(monkeypatch, test_wdc_classes):
    client, _web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120010"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)

    with client:
        resp = client.get("/?test_mode=1")

    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.select_one("#build-list .build[data-class-name='TestClass']")
    assert card is not None
    assert card.get("data-build-name") == build_name
    assert card.get("data-build-detail-url") == f"/builds/TestClass/{build_name}?test_mode=1"

    open_btn = card.select_one(".js-toggle-build")
    assert open_btn is not None
    assert open_btn.get("title") == "Open details"


def test_build_detail_page_renders_existing_build(monkeypatch, test_wdc_classes):
    client, _web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120011"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)

    with client:
        resp = client.get(f"/builds/TestClass/{build_name}?test_mode=1")

    assert resp.status_code == 200
    assert "<title>Build Detail</title>" in resp.text
    assert "Back to dashboard" in resp.text
    assert "/?test_mode=1" in resp.text
    assert "Variant: with_link_code" in resp.text
    assert "Parts Used" in resp.text


def test_build_detail_page_missing_build_redirects_to_index(monkeypatch, test_wdc_classes):
    client, _web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)

    with client:
        resp = client.get("/builds/TestClass/beam_missing?test_mode=1", follow_redirects=False)

    assert resp.status_code == 303
    location = resp.headers.get("location", "")
    assert location.startswith("/?test_mode=1&")
    assert "form_error=Build+not+found." in location


def test_link_explorer_page_and_api(monkeypatch, test_wdc_classes):
    client, _web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120012"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)
    _write_link_explorer_variant(build_root / "with_link_code")

    with client:
        page = client.get(f"/builds/TestClass/{build_name}/links?test_mode=1")
        links_resp = client.get(f"/api/builds/TestClass/{build_name}/links?variant=with_link_code")
        detail_resp = client.get(f"/api/builds/TestClass/{build_name}/link?variant=with_link_code&idx=0")
        node_resp = client.get(
            f"/api/builds/TestClass/{build_name}/node",
            params={
                "variant": "with_link_code",
                "side": "wdc",
                "node": "http://example.org/wdc/entity1",
            },
        )

    assert page.status_code == 200
    assert "Link Explorer" in page.text
    assert "Equivalent properties (WDC -> Wikidata)" in page.text
    assert "Simple view: property equivalents + recursive IRI tree" in page.text
    assert "IRI WDC" not in page.text
    assert "IRI Wikidata" not in page.text

    assert links_resp.status_code == 200
    links_payload = links_resp.json()
    assert links_payload["ok"] is True
    assert links_payload["variant"] == "with_link_code"
    assert links_payload["total"] >= 1
    assert links_payload["rows"][0]["wdc_iri"] == "http://example.org/wdc/entity1"

    assert detail_resp.status_code == 200
    detail_payload = detail_resp.json()
    assert detail_payload["ok"] is True
    detail = detail_payload["detail"]
    assert detail["wdc_iri"] == "http://example.org/wdc/entity1"
    assert detail["wikidata_uri"] == "http://www.wikidata.org/entity/Q100"
    assert any(
        row.get("wdc_short_property") == "name" and row.get("wikidata_short_property") == "label"
        for row in detail.get("property_matches", [])
    )
    assert any(
        str(row.get("wikidata_short_property", "")).lower() == "p1329"
        and row.get("wikidata_property_label") == "phone number"
        for row in detail.get("property_matches", [])
    )

    assert node_resp.status_code == 200
    node_payload = node_resp.json()
    assert node_payload["ok"] is True
    assert node_payload["node"]["side"] == "wdc"
    assert node_payload["node"]["node"] == "http://example.org/wdc/entity1"
    assert node_payload["node"]["summary_label"] == "Alpha City"
    assert node_payload["node"]["attr_count"] >= 1


def test_link_explorer_falls_back_to_wikidata_property_meta(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120013"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)
    _write_link_explorer_variant(build_root / "with_link_code")

    (build_root / "with_link_code" / "prop_stats_wd.tsv").write_text(
        "predicate\tcount\tlabel\tdescription\n"
        "http://www.wikidata.org/prop/direct/p1329\t1\t\t\n",
        encoding="utf-8",
    )

    def fake_wikidata_meta(prop_id, language="en"):
        if prop_id == "P1329":
            return "phone number", "telephone number of subject"
        return "", ""

    monkeypatch.setattr(web_main, "_fetch_wikidata_property_meta", fake_wikidata_meta)

    with client:
        detail_resp = client.get(f"/api/builds/TestClass/{build_name}/link?variant=with_link_code&idx=0")

    assert detail_resp.status_code == 200
    detail = detail_resp.json()["detail"]
    assert any(
        str(row.get("wikidata_short_property", "")).lower() == "p1329"
        and row.get("wikidata_property_label") == "phone number"
        and row.get("wikidata_property_description") == "telephone number of subject"
        for row in detail.get("property_matches", [])
    )


def test_link_explorer_node_summary_local_and_wikidata_entity_fallback(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120017"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)
    _write_link_explorer_variant(build_root / "with_link_code")

    (build_root / "with_link_code" / "attr_triples_2").write_text(
        'http://www.wikidata.org/entity/Q100\thttp://www.wikidata.org/prop/direct/P1329\t"+331234567"\n'
        '_:b1\thttp://www.w3.org/2000/01/rdf-schema#label\t"Nested blank node"\n'
        '_:b1\thttp://schema.org/description\t"nested description from local triples"\n',
        encoding="utf-8",
    )
    (build_root / "with_link_code" / "rel_triples_2").write_text(
        "http://www.wikidata.org/entity/Q100\thttp://www.wikidata.org/prop/direct/P527\t_:b1\n",
        encoding="utf-8",
    )

    def fake_wikidata_entity_meta(entity_id, language="en"):
        if entity_id == "Q100":
            return "Alpha City WD", "city in fallback metadata"
        return "", ""

    monkeypatch.setattr(web_main, "_fetch_wikidata_entity_meta", fake_wikidata_entity_meta)

    with client:
        wd_resp = client.get(
            f"/api/builds/TestClass/{build_name}/node",
            params={
                "variant": "with_link_code",
                "side": "wd",
                "node": "http://www.wikidata.org/entity/Q100",
            },
        )
        blank_resp = client.get(
            f"/api/builds/TestClass/{build_name}/node",
            params={
                "variant": "with_link_code",
                "side": "wd",
                "node": "_:b1",
            },
        )

    assert wd_resp.status_code == 200
    wd_node = wd_resp.json()["node"]
    assert wd_node["summary_label"] == "Alpha City WD"
    assert wd_node["summary_description"] == "city in fallback metadata"

    assert blank_resp.status_code == 200
    blank_node = blank_resp.json()["node"]
    assert blank_node["summary_label"] == "Nested blank node"
    assert blank_node["summary_description"] == "nested description from local triples"


def test_link_explorer_aligns_by_values_when_property_names_do_not_match(monkeypatch, test_wdc_classes):
    client, _web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120014"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)
    _write_link_explorer_value_fallback_variant(build_root / "with_link_code")

    with client:
        detail_resp = client.get(f"/api/builds/TestClass/{build_name}/link?variant=with_link_code&idx=0")

    assert detail_resp.status_code == 200
    detail = detail_resp.json()["detail"]
    assert any(
        row.get("wdc_short_property") == "snarcRef"
        and str(row.get("wikidata_short_property", "")).lower() == "p12749"
        and row.get("match_reason") == "value_fallback"
        for row in detail.get("property_matches", [])
    )


def test_link_explorer_aligns_by_values_when_wikidata_property_has_multiple_values(monkeypatch, test_wdc_classes):
    client, _web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120015"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)
    _write_link_explorer_value_fallback_multivalue_variant(build_root / "with_link_code")

    with client:
        detail_resp = client.get(f"/api/builds/TestClass/{build_name}/link?variant=with_link_code&idx=0")

    assert detail_resp.status_code == 200
    detail = detail_resp.json()["detail"]
    assert any(
        row.get("wdc_short_property") == "snarcRef"
        and str(row.get("wikidata_short_property", "")).lower() == "p12749"
        and row.get("match_reason") == "value_fallback"
        for row in detail.get("property_matches", [])
    )


def test_link_explorer_does_not_align_on_weak_numeric_value_only(monkeypatch, test_wdc_classes):
    client, _web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120016"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)
    _write_link_explorer_weak_numeric_variant(build_root / "with_link_code")

    with client:
        detail_resp = client.get(f"/api/builds/TestClass/{build_name}/link?variant=with_link_code&idx=0")

    assert detail_resp.status_code == 200
    detail = detail_resp.json()["detail"]
    assert not any(
        row.get("wdc_short_property") == "aggregateRating"
        and str(row.get("wikidata_short_property", "")).lower() == "sitelinks"
        for row in detail.get("property_matches", [])
    )


def test_delete_build_removes_directory_and_job_rows(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120001"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)

    job_id_abs = web_main.db.insert_job({"class_name": "TestClass"})
    web_main.db.update_job(job_id_abs, status="done", result_path=str(build_root.resolve()))
    job_id_rel = web_main.db.insert_job({"class_name": "TestClass"})
    web_main.db.update_job(job_id_rel, status="done", result_path=str(build_root))
    job_id_dot_rel = web_main.db.insert_job({"class_name": "TestClass"})
    web_main.db.update_job(job_id_dot_rel, status="done", result_path=f"./{build_root}")

    with client:
        resp = client.post(f"/builds/TestClass/{build_name}/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert not build_root.exists()
    assert web_main.db.get_job(job_id_abs) is None
    assert web_main.db.get_job(job_id_rel) is None
    assert web_main.db.get_job(job_id_dot_rel) is None


def test_purge_low_links_builds_removes_only_under_threshold(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    low_build_name = "beam_20260212_lowlinks"
    high_build_name = "beam_20260212_highlinks"
    low_build_root = Path("data") / "TestClass" / low_build_name
    high_build_root = Path("data") / "TestClass" / high_build_name
    _make_build_tree(low_build_root, links_count=2)
    _make_build_tree(high_build_root, links_count=12)

    low_job_id = web_main.db.insert_job({"class_name": "TestClass"})
    web_main.db.update_job(low_job_id, status="done", result_path=str(low_build_root.resolve()))
    low_job_rel_id = web_main.db.insert_job({"class_name": "TestClass"})
    web_main.db.update_job(low_job_rel_id, status="done", result_path=str(low_build_root))
    high_job_id = web_main.db.insert_job({"class_name": "TestClass"})
    web_main.db.update_job(high_job_id, status="done", result_path=str(high_build_root.resolve()))

    with client:
        resp = client.post("/builds/purge_low_links", data={"max_links": "10"}, follow_redirects=False)

    assert resp.status_code == 303
    assert "purged=1" in (resp.headers.get("location") or "")
    assert not low_build_root.exists()
    assert high_build_root.exists()
    assert web_main.db.get_job(low_job_id) is None
    assert web_main.db.get_job(low_job_rel_id) is None
    assert web_main.db.get_job(high_job_id) is not None


def test_rerun_build_from_card_queues_new_job(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120123"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)

    with client:
        resp = client.post(f"/builds/TestClass/{build_name}/rerun", follow_redirects=False)

    assert resp.status_code == 303
    jobs = web_main.db.list_jobs(limit=1)
    assert len(jobs) == 1
    params = json.loads(jobs[0]["params_json"])
    assert params["class_name"] == "TestClass"
    assert params["parts_spec"] == "all"
    assert params["wdc_predicate_pattern"] == "name"
    assert params["wikidata_property"] == "rdfs:label"


def test_rerun_build_from_card_handles_insert_error(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120124"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)

    monkeypatch.setattr(web_main.db, "insert_job", lambda _params: (_ for _ in ()).throw(RuntimeError("db is locked")))

    with client:
        resp = client.post(f"/builds/TestClass/{build_name}/rerun", follow_redirects=False)

    assert resp.status_code == 303
    loc = resp.headers.get("location", "")
    assert "form_error=" in loc
    assert "Cannot+rerun+build%3A+db+is+locked" in loc


def test_dashboard_api_returns_live_jobs_and_builds(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120777"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)

    running_job_id = web_main.db.insert_job({"class_name": "TestClass", "parts_spec": "all"})
    web_main.db.update_job(
        running_job_id,
        status="running",
        phase="build",
        progress_text="building...",
        progress_pct=55.0,
    )
    web_main.db.update_subjob_by_type(
        running_job_id,
        "build",
        status="running",
        progress_text="build step",
        current_step="build_wd",
    )

    done_job_id = web_main.db.insert_job({"class_name": "TestClass"})
    web_main.db.update_job(done_job_id, status="done", result_path=str(build_root.resolve()))

    with client:
        resp = client.get("/api/dashboard")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["build_count"] >= 1
    build_entry = next((b for b in payload["builds"] if b["build_name"] == build_name), None)
    assert build_entry is not None
    assert any(g.get("title") == "Input" for g in build_entry.get("config_groups", []))
    assert build_entry["with_link"]["sample_links"]
    assert build_entry["with_link"]["top_wdc_props"]
    assert build_entry["with_link"]["top_wd_props"]
    assert isinstance(build_entry["with_link"]["qa_warnings"], list)
    assert payload["job_count"] >= 2
    assert running_job_id in payload["active_job_ids"]
    assert done_job_id not in payload["active_job_ids"]
    assert running_job_id in payload["visible_job_ids"]
    assert done_job_id not in payload["visible_job_ids"]

    jobs = {j["id"]: j for j in payload["jobs"]}
    assert jobs[running_job_id]["status"] == "running"
    assert jobs[running_job_id]["outputs"]["build_done"] is False
    assert isinstance(jobs[running_job_id]["subjobs"], list)
    assert jobs[done_job_id]["status"] == "done"
    assert jobs[done_job_id]["outputs"]["build_done"] is True


def test_dashboard_api_filters_test_mode(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)

    test_build_name = "beam_20260212_test"
    prod_build_name = "beam_20260212_prod"
    test_build_root = Path("data") / "TestClass" / test_build_name
    prod_build_root = Path("data") / "City" / prod_build_name
    _make_build_tree(test_build_root, class_name="TestClass")
    _make_build_tree(prod_build_root, class_name="City")

    test_job_id = web_main.db.insert_job({"class_name": "TestClass", "parts_spec": "all"})
    web_main.db.update_job(test_job_id, status="running")
    prod_job_id = web_main.db.insert_job({"class_name": "City", "parts_spec": "all"})
    web_main.db.update_job(prod_job_id, status="running")

    with client:
        test_resp = client.get("/api/dashboard?test_mode=1")
        prod_resp = client.get("/api/dashboard?test_mode=0")

    assert test_resp.status_code == 200
    assert prod_resp.status_code == 200
    test_payload = test_resp.json()
    prod_payload = prod_resp.json()

    assert any(b["class_name"] == "TestClass" and b["build_name"] == test_build_name for b in test_payload["builds"])
    assert all(b["class_name"] != "City" for b in test_payload["builds"])
    assert test_job_id in [j["id"] for j in test_payload["jobs"]]
    assert prod_job_id not in [j["id"] for j in test_payload["jobs"]]

    assert any(b["class_name"] == "City" and b["build_name"] == prod_build_name for b in prod_payload["builds"])
    assert all(b["class_name"] != "TestClass" for b in prod_payload["builds"])
    assert prod_job_id in [j["id"] for j in prod_payload["jobs"]]
    assert test_job_id not in [j["id"] for j in prod_payload["jobs"]]


def test_dashboard_api_keeps_failed_job_visible_when_no_build_output(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)

    failed_no_build_job_id = web_main.db.insert_job({"class_name": "City", "parts_spec": "1"})
    web_main.db.update_job(
        failed_no_build_job_id,
        status="error",
        result_path=None,
        error_message="No alignments found (0); build skipped.",
    )

    done_with_build_job_id = web_main.db.insert_job({"class_name": "City", "parts_spec": "1"})
    fake_build = Path("data") / "City" / "beam_20260216_150000"
    _make_build_tree(fake_build)
    web_main.db.update_job(done_with_build_job_id, status="done", result_path=str(fake_build.resolve()))

    with client:
        resp = client.get("/api/dashboard")

    assert resp.status_code == 200
    payload = resp.json()
    assert failed_no_build_job_id in payload["visible_job_ids"]
    assert done_with_build_job_id not in payload["visible_job_ids"]


def test_dashboard_api_normalizes_legacy_done_skipped_build_to_error(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)

    reason = "No alignments found (0); build skipped."
    job_id = web_main.db.insert_job({"class_name": "City", "parts_spec": "1"})
    web_main.db.update_job(
        job_id,
        status="done",
        phase="build",
        progress_text=reason,
        error_message=None,
        result_path=None,
    )
    web_main.db.update_subjob_by_type(job_id, "align", status="done")
    web_main.db.update_subjob_by_type(
        job_id,
        "build",
        status="done",
        current_step="skipped",
        progress_text=reason,
    )

    with client:
        resp = client.get("/api/dashboard")

    assert resp.status_code == 200
    payload = resp.json()
    assert job_id in payload["visible_job_ids"]
    row = next(j for j in payload["jobs"] if j["id"] == job_id)
    assert row["status"] == "error"
    assert "no alignments found" in (row.get("error_message") or "").lower()


def test_dashboard_api_hides_done_jobs_with_missing_result_path(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)

    missing_path_job_id = web_main.db.insert_job({"class_name": "TestClass", "parts_spec": "all"})
    web_main.db.update_job(
        missing_path_job_id,
        status="done",
        result_path=str((Path("data") / "TestClass" / "beam_missing_12345").resolve()),
        progress_text="done",
    )

    with client:
        resp = client.get("/api/dashboard?test_mode=1")

    assert resp.status_code == 200
    payload = resp.json()
    assert missing_path_job_id not in payload["visible_job_ids"]


def test_class_parts_api_reports_downloaded_and_missing(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    class_dir = Path("Download") / "TestClass"
    class_dir.mkdir(parents=True, exist_ok=True)
    (class_dir / "part_0001.nq").write_text("<s> <p> <o> .\n", encoding="utf-8")
    (class_dir / "part_0003.nq").write_text("<s> <p> <o> .\n", encoding="utf-8")

    monkeypatch.setattr(web_main, "_discover_online_part_numbers", lambda class_name: ([1, 2, 3, 4], None))

    with client:
        resp = client.get("/api/class_parts/TestClass")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["class_name"] == "TestClass"
    assert payload["downloaded_part_numbers"] == [1, 3]
    assert payload["not_downloaded_online_part_numbers"] == [2, 4]
    assert payload["downloaded_parts_count"] == 2
    assert payload["not_downloaded_online_parts_count"] == 2


def test_class_parts_api_infers_missing_from_catalog_count(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    web_main.db.upsert_wdc_classes(
        [
            {
                "class_name": "Movie",
                "num_parts": 13,
                "size_human": "24.9 GB",
            }
        ]
    )

    class_dir = Path("Download") / "Movie"
    class_dir.mkdir(parents=True, exist_ok=True)
    for i in range(12):
        (class_dir / f"part_{i:04d}.nq").write_text("<s> <p> <o> .\n", encoding="utf-8")
    (class_dir / "part_0999.nq").write_text("<s> <p> <o> .\n", encoding="utf-8")

    monkeypatch.setattr(web_main, "_discover_online_part_numbers", lambda class_name: (list(range(12)), None))

    with client:
        resp = client.get("/api/class_parts/Movie")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["class_name"] == "Movie"
    assert payload["class_num_parts"] == 13
    assert payload["online_available_count"] == 13
    assert payload["online_available_ranges"] == "0-12"
    assert payload["downloaded_part_ranges"] == "0-11"
    assert payload["not_downloaded_online_part_ranges"] == "12"
    assert payload["not_downloaded_online_part_numbers"] == [12]
    assert payload["local_only_part_numbers"] == [999]
