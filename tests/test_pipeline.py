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
        assert "<http://schema.org/TestClass>" in set(kwargs.get("type_filter_iris") or [])
        assert "<https://schema.org/TestClass>" in set(kwargs.get("type_filter_iris") or [])
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


def test_filter_links_one_to_one_drops_ambiguous_endpoints():
    wdc_entities = [
        "wdc:1",
        "wdc:2",
        "wdc:3",
        "wdc:4",
    ]
    wd_entities = [
        "http://www.wikidata.org/entity/Q10",
        "http://www.wikidata.org/entity/Q10",  # duplicate WD endpoint
        "http://www.wikidata.org/entity/Q11",
        "http://www.wikidata.org/entity/Q12",
    ]
    wdc_values = ["A", "B", "C", "D"]
    wd_values = ["A", "B", "C", "D"]

    out_wdc, out_wd, out_wdc_vals, out_wd_vals, report = pipeline._filter_links_one_to_one(
        wdc_entities, wd_entities, wdc_values, wd_values
    )

    assert out_wdc == ["wdc:3", "wdc:4"]
    assert out_wd == [
        "http://www.wikidata.org/entity/Q11",
        "http://www.wikidata.org/entity/Q12",
    ]
    assert out_wdc_vals == ["C", "D"]
    assert out_wd_vals == ["C", "D"]
    assert report["links_before"] == 4
    assert report["links_after"] == 2
    assert report["filtered_out_links"] == 2
    assert report["ambiguous_wikidata_entities"] == 1
    assert report["ambiguous_wdc_entities"] == 0


def test_dedup_links_exact_wdc_subgraph_by_link_value_collapses_identical_bnode_mentions(tmp_path):
    part = tmp_path / "part_0.nq"
    part.write_text(
        "\n".join(
            [
                # Subject A
                "_:a <http://schema.org/name> \"Fachhochschule Kiel\" .",
                "_:a <http://schema.org/telephone> \"+494312100\" .",
                "_:a <http://schema.org/email> \"info@fh-kiel.de\" .",
                "_:a <http://schema.org/address> _:a_addr .",
                "_:a_addr <http://schema.org/postalCode> \"24118\" .",
                "_:a_addr <http://schema.org/addressCountry> \"DE\" .",
                # Subject B (same content, different bnode ids)
                "_:b <http://schema.org/name> \"Fachhochschule Kiel\" .",
                "_:b <http://schema.org/telephone> \"+494312100\" .",
                "_:b <http://schema.org/email> \"info@fh-kiel.de\" .",
                "_:b <http://schema.org/address> _:b_addr .",
                "_:b_addr <http://schema.org/postalCode> \"24118\" .",
                "_:b_addr <http://schema.org/addressCountry> \"DE\" .",
                # Subject C (same linking value but different address => should not dedup)
                "_:c <http://schema.org/name> \"Fachhochschule Kiel\" .",
                "_:c <http://schema.org/telephone> \"+494312100\" .",
                "_:c <http://schema.org/email> \"info@fh-kiel.de\" .",
                "_:c <http://schema.org/address> _:c_addr .",
                "_:c_addr <http://schema.org/postalCode> \"99999\" .",
                "_:c_addr <http://schema.org/addressCountry> \"DE\" .",
                # Subject D (different linking value)
                "_:d <http://schema.org/name> \"Other\" .",
                "_:d <http://schema.org/telephone> \"+33123456\" .",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    wdc_entities = ["_:a", "_:b", "_:c", "_:d"]
    wd_entities = [
        "http://www.wikidata.org/entity/Q1",
        "http://www.wikidata.org/entity/Q1",
        "http://www.wikidata.org/entity/Q1",
        "http://www.wikidata.org/entity/Q2",
    ]
    wdc_values = ["+494312100", "+494312100", "+494312100", "+33123456"]
    wd_values = ["+494312100", "+494312100", "+494312100", "+33123456"]

    out_wdc, out_wd, out_wdc_vals, out_wd_vals, report = pipeline._dedup_links_exact_wdc_subgraph_by_link_value(
        [str(part)],
        wdc_entities,
        wd_entities,
        wdc_values,
        wd_values,
    )

    assert len(out_wdc) == 3
    assert len(out_wd) == 3
    assert len(out_wdc_vals) == 3
    assert len(out_wd_vals) == 3
    assert set(out_wdc) == {"_:a", "_:c", "_:d"} or set(out_wdc) == {"_:b", "_:c", "_:d"}
    assert report["links_before"] == 4
    assert report["links_after"] == 3
    assert report["filtered_out_links"] == 1
    assert report["multi_link_value_groups"] == 1
    assert report["subjects_profiled"] == 3
    assert report["exact_duplicate_clusters"] == 1


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


def test_generate_benchmark_wikidata_mode_fails_when_no_wd_urls(monkeypatch):
    class_name = "TestClassNoWdUrls"
    _write_test_parts(class_name)

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)
    monkeypatch.setattr(
        pipeline.align,
        "extract_unique_iris_from_files",
        lambda *args, **kwargs: ({}, 10),
    )

    def _must_not_fetch(*args, **kwargs):
        raise AssertionError("fetch_wikidata_values should not be called when no Wikidata URLs were extracted")

    monkeypatch.setattr(pipeline.align, "fetch_wikidata_values", _must_not_fetch)

    with pytest.raises(pipeline.PipelineError, match="No Wikidata URLs extracted from WDC values"):
        pipeline.generate_benchmark(
            {
                "class_name": class_name,
                "parts_spec": "all",
                "wdc_predicate_pattern": "sameAs",
                "wikidata_property": None,
                "wkd_class": "Q515",
                "wdc_value_is_wikidata": True,
                "use_local_only": True,
                "force_align": True,
            },
            workers=1,
        )


def test_generate_benchmark_wikidata_mode_fails_when_class_filter_has_no_hits(monkeypatch):
    class_name = "TestClassNoWdClassHits"
    _write_test_parts(class_name)

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)
    monkeypatch.setattr(
        pipeline.align,
        "extract_unique_iris_from_files",
        lambda *args, **kwargs: (
            {
                "httpwwwwikidataorgentityq515": [
                    ("https://www.wikidata.org/wiki/Q515", "http://example.org/wdc/entity1")
                ]
            },
            1,
        ),
    )
    monkeypatch.setattr(pipeline.align, "fetch_wikidata_values", lambda *args, **kwargs: {})

    with pytest.raises(
        pipeline.PipelineError,
        match="No Wikidata entities matched class filter",
    ):
        pipeline.generate_benchmark(
            {
                "class_name": class_name,
                "parts_spec": "all",
                "wdc_predicate_pattern": "sameAs",
                "wikidata_property": None,
                "wkd_class": "Q110879422",
                "wdc_value_is_wikidata": True,
                "use_local_only": True,
                "force_align": True,
            },
            workers=1,
        )


def test_generate_benchmark_with_property_mapping_rules(monkeypatch):
    class_name = "TestClassRules"
    _write_test_parts(class_name)

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_extra_strip_chars", lambda chars: None)
    monkeypatch.setattr(pipeline.align, "parse_strip_list", lambda text: {" ", "-", "."})
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)

    extract_calls = []
    fetch_calls = []

    def extract_unique_iris_from_files(files, pattern, **kwargs):
        extract_calls.append(pattern)
        if pattern == "name":
            return ({"alpha node": [("Alpha Node", "http://example.org/wdc/entity1")]}, 1)
        if pattern == "iata":
            return ({"abc": [("ABC", "http://example.org/wdc/entity1")]}, 1)
        return ({}, 0)

    monkeypatch.setattr(pipeline.align, "extract_unique_iris_from_files", extract_unique_iris_from_files)

    def fetch_wikidata_values(wikidata_property, wkd_class, wkd_prop_class):
        fetch_calls.append(wikidata_property)
        if wikidata_property == "rdfs:label":
            return {"alpha node": [("Alpha Node", "http://www.wikidata.org/entity/Q1001")]}
        if wikidata_property == "P238":
            return {"abc": [("ABC", "http://www.wikidata.org/entity/Q1001")]}
        return {}

    monkeypatch.setattr(pipeline.align, "fetch_wikidata_values", fetch_wikidata_values)

    def fuzzy_link(wdc_map, wikidata_map, **kwargs):
        if "alpha node" in wdc_map:
            return (
                [
                    {
                        "wdc_iri": "http://example.org/wdc/entity1",
                        "wikidata_uri": "http://www.wikidata.org/entity/Q1001",
                        "wdc_value": "Alpha Node",
                        "wiki_value": "Alpha Node",
                        "method": "exact",
                    }
                ],
                {"alpha node"},
            )
        if "abc" in wdc_map:
            return (
                [
                    {
                        "wdc_iri": "http://example.org/wdc/entity1",
                        "wikidata_uri": "http://www.wikidata.org/entity/Q1001",
                        "wdc_value": "ABC",
                        "wiki_value": "ABC",
                        "method": "exact",
                    }
                ],
                {"abc"},
            )
        return ([], set())

    monkeypatch.setattr(pipeline.align, "fuzzy_link", fuzzy_link)

    def export_results(matches, wdc_values_matched, wdc_map, wikidata_map, output_dir, **kwargs):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        with (out / "wdc_wikidata_links.tsv").open("w", encoding="utf-8") as f:
            f.write("wdc_iri\twikidata_uri\twdc_value\twiki_value\tmethod\tmin_len\n")
            for m in matches:
                f.write(
                    f"{m['wdc_iri']}\t{m['wikidata_uri']}\t{m['wdc_value']}\t{m['wiki_value']}\t{m['method']}\t1\n"
                )

    monkeypatch.setattr(pipeline.align, "export_results", export_results)

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

    def run_pipeline_stub(args, wdc_entities, wd_entities_raw, wdc_values, wd_values, out_dir, *rest, **kwargs):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
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

    result = pipeline.generate_benchmark(
        {
            "class_name": class_name,
            "parts_spec": "all",
            "matching_mode": "property",
            "wdc_predicate_pattern": "",
            "property_mapping_rules": "name => rdfs:label\niata => P238",
            "wikidata_property": "",
            "wkd_class": "Q1248784",
            "ignore_chars": "spaces;-;.",
            "use_local_only": True,
            "force_align": True,
        },
        workers=1,
    )

    assert result["class_name"] == class_name
    assert extract_calls == ["name", "iata"]
    assert fetch_calls == ["rdfs:label", "P238"]

    out_dir = Path(result["out_dir"])
    cfg = json.loads((out_dir / "BUILD_CONFIG.json").read_text(encoding="utf-8"))
    assert cfg["property_mapping_rules"] == "name => rdfs:label\niata => P238"
