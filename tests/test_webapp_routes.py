import importlib
import json
from pathlib import Path

from fastapi.testclient import TestClient


def _write_variant_files(variant_dir: Path):
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / "ent_links").write_text(
        "wdc_iri\twikidata_uri\n"
        "http://example.org/wdc/entity1\thttp://www.wikidata.org/entity/Q515\n"
        "http://example.org/wdc/entity2\thttp://www.wikidata.org/entity/Q64\n",
        encoding="utf-8",
    )
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


def _make_build_tree(build_root: Path):
    build_root.mkdir(parents=True, exist_ok=True)
    (build_root / "BUILD_DONE").write_text("2026-02-12 12:00:00", encoding="utf-8")
    (build_root / "BUILD_CONFIG.json").write_text(
        json.dumps(
            {
                "class_name": "TestClass",
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
    _write_variant_files(build_root / "with_link_code")
    _write_variant_files(build_root / "without_link_code")


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

    rows = [dict(r) for r in web_main.db.list_wdc_classes()]
    assert any(row["class_name"] == "TestClass" for row in rows)


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
        "max_depth": "-1",
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


def test_builds_render_and_download(monkeypatch, test_wdc_classes):
    client, web_main = _client_with_test_classes(monkeypatch, test_wdc_classes)
    build_name = "beam_20260212_120000"
    build_root = Path("data") / "TestClass" / build_name
    _make_build_tree(build_root)

    with client:
        home = client.get("/")
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

    job_id = web_main.db.insert_job({"class_name": "TestClass"})
    web_main.db.update_job(job_id, status="done", result_path=str(build_root.resolve()))

    with client:
        resp = client.post(f"/builds/TestClass/{build_name}/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert not build_root.exists()
    assert web_main.db.get_job(job_id) is None


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
    assert any(b["build_name"] == build_name for b in payload["builds"])
    assert payload["job_count"] >= 2
    assert running_job_id in payload["active_job_ids"]
    assert done_job_id not in payload["active_job_ids"]

    jobs = {j["id"]: j for j in payload["jobs"]}
    assert jobs[running_job_id]["status"] == "running"
    assert jobs[running_job_id]["outputs"]["build_done"] is False
    assert isinstance(jobs[running_job_id]["subjobs"], list)
    assert jobs[done_job_id]["status"] == "done"
    assert jobs[done_job_id]["outputs"]["build_done"] is True
