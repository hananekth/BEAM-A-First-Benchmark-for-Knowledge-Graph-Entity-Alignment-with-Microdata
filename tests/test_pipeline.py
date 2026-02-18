import json
from pathlib import Path

import pytest

from beam import pipeline


def _write_test_parts(class_name="TestClass"):
    class_dir = Path("Download") / class_name
    class_dir.mkdir(parents=True, exist_ok=True)
    (class_dir / "part_0001.nq").write_text(
        "<http://example.org/wdc/entity1> <http://schema.org/name> \"Alpha Node\" .\n"
        "<http://example.org/wdc/entity1> <http://schema.org/relatedLink> <http://example.org/wdc/entity2> .\n",
        encoding="utf-8",
    )
    (class_dir / "part_0002.nq").write_text(
        "<http://example.org/wdc/entity2> <http://schema.org/name> \"Beta Node\" .\n"
        "<http://example.org/wdc/entity2> <http://schema.org/url> <http://www.wikidata.org/entity/Q515> .\n",
        encoding="utf-8",
    )
    return class_dir


def _install_test_stubs(monkeypatch):
    calls = {"build_out_dirs": [], "run_pipeline_calls": []}

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_extra_strip_chars", lambda chars: None)
    monkeypatch.setattr(pipeline.align, "parse_strip_list", lambda text: {" ", "-", "."})
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)

    def extract_unique_iris_from_files(files, pattern, **kwargs):
        assert pattern == "name"
        assert len(files) == 2
        return (
            {
                "alpha node": [("Alpha Node", "http://example.org/wdc/entity1")],
                "beta node": [("Beta Node", "http://example.org/wdc/entity2")],
            },
            2,
        )

    monkeypatch.setattr(pipeline.align, "extract_unique_iris_from_files", extract_unique_iris_from_files)

    monkeypatch.setattr(
        pipeline.align,
        "fetch_wikidata_values",
        lambda wikidata_property, wkd_class, wkd_prop_class: {
            "alpha node": [("Alpha Node", "http://www.wikidata.org/entity/Q1001")],
            "beta node": [("Beta Node", "http://www.wikidata.org/entity/Q1002")],
        },
    )

    def fuzzy_link(wdc_map, wikidata_map, **kwargs):
        assert len(wdc_map) == 2
        assert len(wikidata_map) == 2
        return (
            [
                {
                    "wdc_iri": "http://example.org/wdc/entity1",
                    "wikidata_uri": "http://www.wikidata.org/entity/Q1001",
                    "wdc_value": "Alpha Node",
                    "wiki_value": "Alpha Node",
                    "method": "exact",
                },
                {
                    "wdc_iri": "http://example.org/wdc/entity2",
                    "wikidata_uri": "http://www.wikidata.org/entity/Q1002",
                    "wdc_value": "Beta Node",
                    "wiki_value": "Beta Node",
                    "method": "exact",
                },
            ],
            {"alpha node", "beta node"},
        )

    monkeypatch.setattr(pipeline.align, "fuzzy_link", fuzzy_link)

    def export_results(matches, wdc_values_matched, wdc_map, wikidata_map, output_dir, **kwargs):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "wdc_wikidata_links.tsv").write_text(
            "wdc_iri\twikidata_uri\twdc_value\twiki_value\tmethod\tmin_len\n"
            "http://example.org/wdc/entity1\thttp://www.wikidata.org/entity/Q1001\tAlpha Node\tAlpha Node\texact\t3\n"
            "http://example.org/wdc/entity2\thttp://www.wikidata.org/entity/Q1002\tBeta Node\tBeta Node\texact\t3\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(pipeline.align, "export_results", export_results)

    def run_pipeline_stub(args, wdc_entities, wd_entities_raw, wdc_values, wd_values, out_dir, *rest, **kwargs):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        calls["build_out_dirs"].append(str(out))
        calls["run_pipeline_calls"].append(
            {
                "out_dir": str(out),
                "wdc_exclude_prop_patterns": set(rest[3]),
                "wd_exclude_props": set(rest[4]),
                "wd_raw_cache_path": kwargs.get("wd_raw_cache_path"),
            }
        )
        (out / "ent_links").write_text(
            "wdc_iri\twikidata_uri\n"
            "http://example.org/wdc/entity1\thttp://www.wikidata.org/entity/Q1001\n",
            encoding="utf-8",
        )
        (out / "attr_triples_1").write_text("s\tp\to\n", encoding="utf-8")
        (out / "rel_triples_1").write_text("s\tp\to\n", encoding="utf-8")
        (out / "attr_triples_2").write_text("s\tp\to\n", encoding="utf-8")
        (out / "rel_triples_2").write_text("s\tp\to\n", encoding="utf-8")
        (out / "prop_stats_wdc.tsv").write_text("property\tcount\nname\t2\n", encoding="utf-8")
        (out / "prop_stats_wd.tsv").write_text("property\tcount\nP31\t2\n", encoding="utf-8")

    monkeypatch.setattr(pipeline.build, "run_pipeline", run_pipeline_stub)
    return calls


def test_generate_benchmark_with_local_test_parts(monkeypatch):
    _write_test_parts("TestClass")
    calls = _install_test_stubs(monkeypatch)

    result = pipeline.generate_benchmark(
        {
            "class_name": "TestClass",
            "parts_spec": "all",
            "wdc_predicate_pattern": "name",
            "wikidata_property": "P31",
            "wkd_class": "Q515",
            "ignore_chars": "spaces;-;.",
            "wdc_value_is_wikidata": False,
            "max_depth": -1,
            "use_local_only": True,
            "force_align": True,
        },
        workers=1,
    )

    assert result["class_name"] == "TestClass"
    assert result["reused_align"] is False
    assert Path(result["links_tsv"]).exists()
    assert Path(result["align_dir"], "ALIGN_DONE").exists()

    out_dir = Path(result["out_dir"])
    assert out_dir.exists()
    assert (out_dir / "BUILD_DONE").exists()
    assert (out_dir / "with_link_code").exists()
    assert (out_dir / "without_link_code").exists()
    assert len(calls["build_out_dirs"]) == 2

    config = json.loads((out_dir / "BUILD_CONFIG.json").read_text(encoding="utf-8"))
    assert config["class_name"] == "TestClass"
    assert config["parts_count"] == 2
    assert [p["name"] for p in config["parts_manifest"]] == ["part_0001.nq", "part_0002.nq"]

    assert len(calls["run_pipeline_calls"]) == 2
    without = calls["run_pipeline_calls"][0]
    with_link = calls["run_pipeline_calls"][1]
    assert without["wdc_exclude_prop_patterns"] == {"name"}
    assert any("wikidata.org/prop/direct/p31" in p for p in without["wd_exclude_props"])
    assert without["wd_raw_cache_path"].endswith(".wd_raw_triples.tsv")
    assert with_link["wdc_exclude_prop_patterns"] == set()
    assert with_link["wd_exclude_props"] == set()
    assert with_link["wd_raw_cache_path"] == without["wd_raw_cache_path"]


def test_generate_benchmark_rejects_missing_required_values():
    with pytest.raises(pipeline.PipelineError, match="wikidata_property is required"):
        pipeline.generate_benchmark(
            {
                "class_name": "TestClass",
                "parts_spec": "all",
                "wdc_predicate_pattern": "name",
                "wdc_value_is_wikidata": False,
            }
        )

    with pytest.raises(pipeline.PipelineError, match="wkd_class is required"):
        pipeline.generate_benchmark(
            {
                "class_name": "TestClass",
                "parts_spec": "all",
                "wdc_predicate_pattern": "url",
                "wdc_value_is_wikidata": True,
            }
        )


def test_generate_benchmark_build_only_requires_cached_align(monkeypatch):
    _write_test_parts("TestClass")
    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)

    with pytest.raises(pipeline.PipelineError, match="Cached align not found"):
        pipeline.generate_benchmark(
            {
                "class_name": "TestClass",
                "parts_spec": "all",
                "wdc_predicate_pattern": "name",
                "wikidata_property": "P31",
                "wkd_class": "Q515",
                "use_local_only": True,
                "require_cached_align": True,
            }
        )


def test_generate_benchmark_resume_build_reuses_out_dir(monkeypatch):
    class_name = "TestClassResume"
    _write_test_parts(class_name)

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_extra_strip_chars", lambda chars: None)
    monkeypatch.setattr(pipeline.align, "parse_strip_list", lambda text: {" ", "-", "."})
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)

    align_params = {
        "class_name": class_name,
        "parts_spec": "all",
        "pattern": "name",
        "wikidata_property": "P31",
        "wkd_class": "Q515",
        "ignore_chars": None,
        "wdc_value_is_wikidata": False,
    }
    cache_hash = pipeline._config_hash(align_params)
    cache_dir = Path("Download") / class_name / "align_cache" / cache_hash
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "wdc_wikidata_links.tsv").write_text(
        "wdc_iri\twikidata_uri\twdc_value\twiki_value\tmethod\tmin_len\n"
        "http://example.org/wdc/entity1\thttp://www.wikidata.org/entity/Q1001\tAlpha Node\tAlpha Node\texact\t3\n",
        encoding="utf-8",
    )
    (cache_dir / "ALIGN_DONE").write_text("ok\n", encoding="utf-8")

    monkeypatch.setattr(
        pipeline.build,
        "read_links",
        lambda *args, **kwargs: (
            ["http://example.org/wdc/entity1"],
            ["http://www.wikidata.org/entity/Q1001"],
            ["Alpha Node"],
            ["Alpha Node"],
        ),
    )

    run_pipeline_calls = []

    def run_pipeline_stub(args, wdc_entities, wd_entities_raw, wdc_values, wd_values, out_dir, *rest, **kwargs):
        run_pipeline_calls.append({"out_dir": out_dir, "resume": bool(args.resume)})
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(pipeline.build, "run_pipeline", run_pipeline_stub)

    resume_out_dir = Path("data") / class_name / "beam_resume_target"
    resume_out_dir.mkdir(parents=True, exist_ok=True)
    checkpoints = []

    result = pipeline.generate_benchmark(
        {
            "class_name": class_name,
            "parts_spec": "all",
            "wdc_predicate_pattern": "name",
            "wikidata_property": "P31",
            "wkd_class": "Q515",
            "wdc_value_is_wikidata": False,
            "use_local_only": True,
            "require_cached_align": True,
            "resume_build": True,
            "resume_out_dir": str(resume_out_dir),
        },
        workers=1,
        on_checkpoint=lambda payload: checkpoints.append(payload),
    )

    assert result["reused_align"] is True
    assert result["out_dir"] == str(resume_out_dir)
    assert (resume_out_dir / "BUILD_DONE").exists()
    assert len(run_pipeline_calls) == 2
    assert all(call["resume"] is True for call in run_pipeline_calls)
    assert checkpoints
    assert checkpoints[0]["kind"] == "build_started"
    assert checkpoints[0]["out_dir"] == str(resume_out_dir)
