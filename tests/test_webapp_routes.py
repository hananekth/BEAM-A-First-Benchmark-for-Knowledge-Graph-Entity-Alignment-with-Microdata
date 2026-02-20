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
                "class_name": class_name,
                "parts_spec": "all",
                "wdc_predicate_pattern": "name",
                "wikidata_property": "rdfs:label",
                "wkd_class": "Q515",
                "ignore_chars": "spaces;-;.",
                "wdc_value_is_wikidata": False,
                "force_align": False,
                "use_local_only": True,
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


def _client_with_test_classes(monkeypatch, test_wdc_classes):
    import beam.db as beam_db
    import webapp.main as web_main

    importlib.reload(web_main)
    monkeypatch.setattr(web_main.db, "DB_PATH", beam_db.DB_PATH)
    monkeypatch.setattr(web_main, "fetch_wdc_classes", lambda: list(test_wdc_classes))
    return TestClient(web_main.app), web_main


def test_index_populates_testclass_list(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    with client:
        resp = client.get("/")

    assert resp.status_code == 200
    assert "TestClass" in resp.text
    soup = BeautifulSoup(resp.text, "html.parser")
    mode_select = soup.find("select", {"id": "matching-mode-select"})
    assert mode_select is not None
    mode_values = {opt.get("value", "") for opt in mode_select.find_all("option")}
    assert {"label", "identifier", "telephone", "wikidata_url"} <= mode_values
    assert soup.find("div", {"id": "ready-checklist"}) is not None
    assert soup.find("input", {"id": "history-search-input"}) is not None
    assert soup.find("select", {"id": "history-sort-select"}) is not None
    assert soup.find("form", {"id": "purge-low-links-form"}) is not None
    preset_select = soup.find("select", {"id": "preset-select"})
    assert preset_select is not None
    preset_values = {opt.get("value", "") for opt in preset_select.find_all("option")}
    assert "testclass_quick" not in preset_values

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


def test_refresh_classes_updates_cache(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    with client:
        resp = client.get("/refresh_classes", follow_redirects=False)

    assert resp.status_code == 303
    rows = [dict(r) for r in web_main.db.list_wdc_classes()]
    assert {r["class_name"] for r in rows} >= {"TestClass", "TestClassTwo"}


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
        resp = client.get("/")

    assert resp.status_code == 200
    assert "TestClass" in resp.text
    rows = [dict(r) for r in web_main.db.list_wdc_classes()]
    test_row = [r for r in rows if r["class_name"] == "TestClass"]
    assert len(test_row) == 1
    assert test_row[0]["num_parts"] == 2


def test_create_job_persists_params(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    form = {
        "class_name": "  TestClass  ",
        "parts_spec": "  all  ",
        "wdc_predicate_pattern": "  name  ",
        "wikidata_property": "  P31  ",
        "wkd_class": "  Q515  ",
        "ignore_chars": "  spaces;-;.  ",
        "wdc_value_is_wikidata": "",
        "force_align": "",
        "use_local_only": "",
    }
    with client:
        resp = client.post("/jobs", data=form, follow_redirects=False)

    assert resp.status_code == 303
    jobs = web_main.db.list_jobs(limit=1)
    assert len(jobs) == 1
    params = json.loads(jobs[0]["params_json"])
    assert params["class_name"] == "TestClass"
    assert params["parts_spec"] == "all"
    assert params["wdc_predicate_pattern"] == "name"
    assert params["wikidata_property"] == "P31"


def test_create_job_requires_wikidata_property_when_not_url_mode(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    form = {
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
        "class_name": "TestClass",
        "parts_spec": "all",
        "wdc_predicate_pattern": "sameAs",
        "wikidata_property": "rdfs:label",
        "wkd_class": "Q486972",
        "ignore_chars": "spaces;-;.",
        "wdc_value_is_wikidata": "on",
    }
    with client:
        resp = client.post("/jobs", data=form, follow_redirects=False)

    assert resp.status_code == 303
    jobs = web_main.db.list_jobs(limit=1)
    assert len(jobs) == 1
    params = json.loads(jobs[0]["params_json"])
    assert params["wdc_value_is_wikidata"] is True
    assert params["wikidata_property"] == ""
    assert params["wkd_class"] == "Q486972"


def test_create_job_sameas_mode_auto_enables_url_mode(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    form = {
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
    assert params["wdc_value_is_wikidata"] is True
    assert params["wikidata_property"] == ""
    assert params["ignore_chars"] == ""


def test_create_job_sameas_mode_requires_wikidata_class(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    form = {
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
                "wdc_predicate_pattern": "name",
                "ignore_chars": "spaces;-;.",
                "wdc_value_is_wikidata": "false",
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
