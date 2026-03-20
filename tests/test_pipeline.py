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
    assert config["wdc_pattern_search_in"] == "predicate"
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


def test_generate_benchmark_strict_duplicate_key_filter_writes_reports(monkeypatch):
    _write_test_parts("TestClass")
    _install_test_stubs(monkeypatch)

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
            "strict_duplicate_key_filter": True,
        },
        workers=1,
    )

    out_dir = Path(result["out_dir"])
    report_path = out_dir / "WDC_DUPLICATE_KEY_FILTER_REPORT.json"
    decisions_path = out_dir / "WDC_DUPLICATE_KEY_FILTER_DECISIONS.tsv"
    build_cfg = json.loads((out_dir / "BUILD_CONFIG.json").read_text(encoding="utf-8"))
    build_stats = json.loads((out_dir / "BUILD_STATS.json").read_text(encoding="utf-8"))

    assert report_path.exists()
    assert decisions_path.exists()
    assert build_cfg["strict_duplicate_key_filter"] is True
    assert build_stats["strict_duplicate_key_filter"] is True
    assert build_stats["links_after_strict_duplicate_key_filter"] >= 0
    assert isinstance(build_stats.get("links_by_source_after_filter"), list)


def test_generate_benchmark_prefilters_wikidata_iata_with_wdc_values(monkeypatch):
    class_name = "TestClassIataPrefilter"
    _write_test_parts(class_name)

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_extra_strip_chars", lambda chars: None)
    monkeypatch.setattr(pipeline.align, "parse_strip_list", lambda text: {" ", "-", "."})
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)

    monkeypatch.setattr(
        pipeline.align,
        "extract_unique_iris_from_files",
        lambda *args, **kwargs: (
            {
                "abc": [("ABC", "http://example.org/wdc/entity1")],
                "def": [("DEF", "http://example.org/wdc/entity2")],
            },
            2,
        ),
    )

    fetch_calls = []

    def fetch_wikidata_values(wikidata_property, wkd_class, wkd_prop_class, **kwargs):
        fetch_calls.append(
            {
                "property": wikidata_property,
                "class": wkd_class,
                "prop_class": wkd_prop_class,
                "value_candidates": list(kwargs.get("value_candidates") or []),
            }
        )
        return {"abc": [("ABC", "http://www.wikidata.org/entity/Q1")]}

    monkeypatch.setattr(pipeline.align, "fetch_wikidata_values", fetch_wikidata_values)
    monkeypatch.setattr(
        pipeline.align,
        "fuzzy_link",
        lambda *args, **kwargs: (
            [
                {
                    "wdc_iri": "http://example.org/wdc/entity1",
                    "wikidata_uri": "http://www.wikidata.org/entity/Q1",
                    "wdc_value": "ABC",
                    "wiki_value": "ABC",
                    "method": "exact",
                }
            ],
            {"abc"},
        ),
    )

    def export_results(matches, wdc_values_matched, wdc_map, wikidata_map, output_dir, **kwargs):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "wdc_wikidata_links.tsv").write_text(
            "wdc_iri\twikidata_uri\twdc_value\twiki_value\tmethod\tmin_len\n"
            "http://example.org/wdc/entity1\thttp://www.wikidata.org/entity/Q1\tABC\tABC\texact\t3\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(pipeline.align, "export_results", export_results)
    monkeypatch.setattr(
        pipeline.build,
        "read_links",
        lambda *args, **kwargs: (
            ["http://example.org/wdc/entity1"],
            ["http://www.wikidata.org/entity/Q1"],
            ["ABC"],
            ["ABC"],
        ),
    )

    def run_pipeline_stub(args, wdc_entities, wd_entities_raw, wdc_values, wd_values, out_dir, *rest, **kwargs):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "ent_links").write_text(
            "wdc_iri\twikidata_uri\n"
            "http://example.org/wdc/entity1\thttp://www.wikidata.org/entity/Q1\n",
            encoding="utf-8",
        )
        (out / "attr_triples_1").write_text("s\tp\to\n", encoding="utf-8")
        (out / "rel_triples_1").write_text("s\tp\to\n", encoding="utf-8")
        (out / "attr_triples_2").write_text("s\tp\to\n", encoding="utf-8")
        (out / "rel_triples_2").write_text("s\tp\to\n", encoding="utf-8")
        (out / "prop_stats_wdc.tsv").write_text("property\tcount\nname\t2\n", encoding="utf-8")
        (out / "prop_stats_wd.tsv").write_text("property\tcount\nP31\t2\n", encoding="utf-8")

    monkeypatch.setattr(pipeline.build, "run_pipeline", run_pipeline_stub)

    pipeline.generate_benchmark(
        {
            "class_name": class_name,
            "parts_spec": "all",
            "matching_mode": "property",
            "wdc_predicate_pattern": "iata",
            "wikidata_property": "P297",
            "wkd_class": "Q1248784",
            "ignore_chars": "spaces;-;.",
            "use_local_only": True,
            "force_align": True,
        },
        workers=1,
    )

    assert len(fetch_calls) == 1
    assert fetch_calls[0]["property"] == "P297"
    assert fetch_calls[0]["class"] == "Q1248784"
    assert fetch_calls[0]["value_candidates"] == ["ABC", "DEF"]


def test_generate_benchmark_forwards_wdc_pattern_search_in(monkeypatch):
    class_name = "TestClassSearchIn"
    _write_test_parts(class_name)

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_extra_strip_chars", lambda chars: None)
    monkeypatch.setattr(pipeline.align, "parse_strip_list", lambda text: {" ", "-", "."})
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)

    seen = {"search_in": None}

    def extract_unique_iris_from_files(files, pattern, **kwargs):
        seen["search_in"] = kwargs.get("search_in")
        return (
            {"alpha node": [("Alpha Node", "http://example.org/wdc/entity1")]},
            1,
        )

    monkeypatch.setattr(pipeline.align, "extract_unique_iris_from_files", extract_unique_iris_from_files)
    monkeypatch.setattr(
        pipeline.align,
        "fetch_wikidata_values",
        lambda *args, **kwargs: {"alpha node": [("Alpha Node", "http://www.wikidata.org/entity/Q1")]},
    )
    monkeypatch.setattr(
        pipeline.align,
        "fuzzy_link",
        lambda *args, **kwargs: (
            [
                {
                    "wdc_iri": "http://example.org/wdc/entity1",
                    "wikidata_uri": "http://www.wikidata.org/entity/Q1",
                    "wdc_value": "Alpha Node",
                    "wiki_value": "Alpha Node",
                    "method": "exact",
                }
            ],
            {"alpha node"},
        ),
    )

    def export_results(matches, wdc_values_matched, wdc_map, wikidata_map, output_dir, **kwargs):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "wdc_wikidata_links.tsv").write_text(
            "wdc_iri\twikidata_uri\twdc_value\twiki_value\tmethod\tmin_len\n"
            "http://example.org/wdc/entity1\thttp://www.wikidata.org/entity/Q1\tAlpha Node\tAlpha Node\texact\t3\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(pipeline.align, "export_results", export_results)
    monkeypatch.setattr(
        pipeline.build,
        "read_links",
        lambda *args, **kwargs: (
            ["http://example.org/wdc/entity1"],
            ["http://www.wikidata.org/entity/Q1"],
            ["Alpha Node"],
            ["Alpha Node"],
        ),
    )

    def run_pipeline_stub(args, wdc_entities, wd_entities_raw, wdc_values, wd_values, out_dir, *rest, **kwargs):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "ent_links").write_text(
            "wdc_iri\twikidata_uri\n"
            "http://example.org/wdc/entity1\thttp://www.wikidata.org/entity/Q1\n",
            encoding="utf-8",
        )
        (out / "attr_triples_1").write_text("s\tp\to\n", encoding="utf-8")
        (out / "rel_triples_1").write_text("s\tp\to\n", encoding="utf-8")
        (out / "attr_triples_2").write_text("s\tp\to\n", encoding="utf-8")
        (out / "rel_triples_2").write_text("s\tp\to\n", encoding="utf-8")
        (out / "prop_stats_wdc.tsv").write_text("property\tcount\nname\t2\n", encoding="utf-8")
        (out / "prop_stats_wd.tsv").write_text("property\tcount\nP31\t2\n", encoding="utf-8")

    monkeypatch.setattr(pipeline.build, "run_pipeline", run_pipeline_stub)

    pipeline.generate_benchmark(
        {
            "class_name": class_name,
            "parts_spec": "all",
            "matching_mode": "property",
            "wdc_predicate_pattern": "ror.org",
            "wdc_pattern_search_in": "value",
            "wikidata_property": "rdfs:label",
            "wkd_class": "Q515",
            "ignore_chars": "spaces;-;.",
            "use_local_only": True,
            "force_align": True,
        },
        workers=1,
    )

    assert seen["search_in"] == "value"


def test_generate_benchmark_reuses_wdc_extract_cache_across_endpoints(monkeypatch):
    class_name = "TestClassWdcCrossEndpointCache"
    _write_test_parts(class_name)

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_extra_strip_chars", lambda chars: None)
    monkeypatch.setattr(pipeline.align, "parse_strip_list", lambda text: {" ", "-", "."})
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)

    extract_calls = {"count": 0}

    def extract_unique_iris_from_files(files, pattern, **kwargs):
        extract_calls["count"] += 1
        assert pattern == "iata"
        return ({"abc": [("ABC", "http://example.org/wdc/entity1")]}, 1)

    monkeypatch.setattr(pipeline.align, "extract_unique_iris_from_files", extract_unique_iris_from_files)

    def fetch_target_values(*_args, **_kwargs):
        return {"abc": [("ABC", "http://target.example/entity/T1")]}

    monkeypatch.setattr(pipeline.align, "fetch_target_values", fetch_target_values)
    monkeypatch.setattr(pipeline.align, "fetch_wikidata_values", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        pipeline.align,
        "fuzzy_link",
        lambda *args, **kwargs: (
            [
                {
                    "wdc_iri": "http://example.org/wdc/entity1",
                    "wikidata_uri": "http://target.example/entity/T1",
                    "wdc_value": "ABC",
                    "wiki_value": "ABC",
                    "method": "exact",
                }
            ],
            {"abc"},
        ),
    )

    def export_results(matches, wdc_values_matched, wdc_map, wikidata_map, output_dir, **kwargs):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "wdc_wikidata_links.tsv").write_text(
            "wdc_iri\twikidata_uri\twdc_value\twiki_value\tmethod\tmin_len\n"
            "http://example.org/wdc/entity1\thttp://target.example/entity/T1\tABC\tABC\texact\t3\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(pipeline.align, "export_results", export_results)
    monkeypatch.setattr(
        pipeline.build,
        "read_links",
        lambda *args, **kwargs: (
            ["http://example.org/wdc/entity1"],
            ["http://target.example/entity/T1"],
            ["ABC"],
            ["ABC"],
        ),
    )

    def run_pipeline_stub(args, wdc_entities, wd_entities_raw, wdc_values, wd_values, out_dir, *rest, **kwargs):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "ent_links").write_text(
            "wdc_iri\twikidata_uri\n"
            "http://example.org/wdc/entity1\thttp://target.example/entity/T1\n",
            encoding="utf-8",
        )
        (out / "attr_triples_1").write_text("s\tp\to\n", encoding="utf-8")
        (out / "rel_triples_1").write_text("s\tp\to\n", encoding="utf-8")
        (out / "attr_triples_2").write_text("s\tp\to\n", encoding="utf-8")
        (out / "rel_triples_2").write_text("s\tp\to\n", encoding="utf-8")
        (out / "prop_stats_wdc.tsv").write_text("property\tcount\nname\t1\n", encoding="utf-8")
        (out / "prop_stats_wd.tsv").write_text("property\tcount\nP31\t1\n", encoding="utf-8")

    monkeypatch.setattr(pipeline.build, "run_pipeline", run_pipeline_stub)

    params_base = {
        "class_name": class_name,
        "parts_spec": "all",
        "matching_mode": "property",
        "wdc_predicate_pattern": "iata",
        "wdc_pattern_search_in": "predicate",
        "target_property": "P238",
        "ignore_chars": "spaces;-;.",
        "use_local_only": True,
    }

    pipeline.generate_benchmark(
        {
            **params_base,
            "target_endpoint": "dbpedia",
            "force_align": True,
        },
        workers=1,
    )
    pipeline.generate_benchmark(
        {
            **params_base,
            "target_endpoint": "yago",
            "force_align": False,
        },
        workers=1,
    )

    # Second run must reuse WDC extraction cache even with a different target endpoint.
    assert extract_calls["count"] == 1


def test_generate_benchmark_property_rules_forward_per_pair_search_modes(monkeypatch):
    class_name = "TestClassRuleSearchModes"
    _write_test_parts(class_name)

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_extra_strip_chars", lambda chars: None)
    monkeypatch.setattr(pipeline.align, "parse_strip_list", lambda text: {" ", "-", "."})
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)

    extract_calls = []

    def extract_unique_iris_from_files(files, pattern, **kwargs):
        extract_calls.append((pattern, kwargs.get("search_in")))
        if pattern == "name":
            return ({"alpha": [("Alpha", "http://example.org/wdc/entity1")]}, 1)
        if pattern == "iata":
            return ({"abc": [("ABC", "http://example.org/wdc/entity1")]}, 1)
        return ({}, 0)

    monkeypatch.setattr(pipeline.align, "extract_unique_iris_from_files", extract_unique_iris_from_files)
    monkeypatch.setattr(
        pipeline.align,
        "fetch_wikidata_values",
        lambda prop, *_args, **_kwargs: (
            {"alpha": [("Alpha", "http://www.wikidata.org/entity/Q1")]}
            if prop == "rdfs:label"
            else {"abc": [("ABC", "http://www.wikidata.org/entity/Q1")]}
        ),
    )
    monkeypatch.setattr(
        pipeline.align,
        "fuzzy_link",
        lambda *args, **kwargs: (
            [
                {
                    "wdc_iri": "http://example.org/wdc/entity1",
                    "wikidata_uri": "http://www.wikidata.org/entity/Q1",
                    "wdc_value": "Alpha",
                    "wiki_value": "Alpha",
                    "method": "exact",
                }
            ],
            {"alpha"},
        ),
    )

    def export_results(matches, wdc_values_matched, wdc_map, wikidata_map, output_dir, **kwargs):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "wdc_wikidata_links.tsv").write_text(
            "wdc_iri\twikidata_uri\twdc_value\twiki_value\tmethod\tmin_len\n"
            "http://example.org/wdc/entity1\thttp://www.wikidata.org/entity/Q1\tAlpha\tAlpha\texact\t3\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(pipeline.align, "export_results", export_results)
    monkeypatch.setattr(
        pipeline.build,
        "read_links",
        lambda *args, **kwargs: (
            ["http://example.org/wdc/entity1"],
            ["http://www.wikidata.org/entity/Q1"],
            ["Alpha"],
            ["Alpha"],
        ),
    )
    monkeypatch.setattr(
        pipeline.build,
        "run_pipeline",
        lambda *args, **kwargs: None,
    )

    pipeline.generate_benchmark(
        {
            "class_name": class_name,
            "parts_spec": "all",
            "matching_mode": "property",
            "wdc_predicate_pattern": "",
            "wdc_pattern_search_in": "predicate",
            "property_mapping_rules": (
                'name,iata => rdfs:label,P238 || {"search_in":["value","predicate"]}'
            ),
            "wkd_class": "Q515",
            "ignore_chars": "spaces;-;.",
            "use_local_only": True,
            "force_align": True,
        },
        workers=1,
    )

    assert ("name", "value") in extract_calls
    assert ("iata", "predicate") in extract_calls


def test_strict_duplicate_key_filter_one_to_one_keeps_single_entity_for_duplicate_key(tmp_path):
    part = tmp_path / "part_0.nq"
    part.write_text(
        "\n".join(
            [
                "_:a <http://schema.org/name> \"Fachhochschule Kiel\" .",
                "_:a <http://schema.org/telephone> \"+494312100\" .",
                "_:a <http://schema.org/email> \"info@fh-kiel.de\" .",
                "_:a <http://schema.org/address> _:a_addr .",
                "_:a_addr <http://schema.org/postalCode> \"24118\" .",
                "_:a_addr <http://schema.org/addressCountry> \"DE\" .",
                "_:b <http://schema.org/name> \"Fachhochschule Kiel\" .",
                "_:b <http://schema.org/telephone> \"+494312100\" .",
                "_:b <http://schema.org/email> \"info@fh-kiel.de\" .",
                "_:b <http://schema.org/address> _:b_addr .",
                "_:b_addr <http://schema.org/postalCode> \"24118\" .",
                "_:b_addr <http://schema.org/addressCountry> \"DE\" .",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    wdc_entities = ["_:a", "_:b"]
    wd_entities = [
        "http://www.wikidata.org/entity/Q1",
        "http://www.wikidata.org/entity/Q2",
    ]
    wdc_values = ["KIEL", "KIEL"]
    wd_values = ["KIEL", "KIEL"]

    out_wdc, out_wd, out_wdc_vals, out_wd_vals, report, decisions = pipeline._apply_strict_duplicate_key_filter(
        [str(part)],
        wdc_entities,
        wd_entities,
        wdc_values,
        wd_values,
    )

    assert out_wdc == ["_:a"]
    assert out_wd == [wd_entities[0]]
    assert out_wdc_vals == [wdc_values[0]]
    assert out_wd_vals == [wd_values[0]]
    assert report["summary"]["links_before"] == 2
    assert report["summary"]["links_after"] == 1
    assert report["summary"]["removed_groups_count"] == 1
    assert len([r for r in decisions if r["decision"] == "remove"]) == 1


def test_strict_duplicate_key_filter_keeps_richest_entity_in_collision_group(tmp_path):
    part = tmp_path / "part_0.nq"
    part.write_text(
        "\n".join(
            [
                "_:a <http://schema.org/name> \"Alpha Airport\" .",
                "_:a <http://schema.org/telephone> \"+111\" .",
                "_:b <http://schema.org/name> \"Bravo Airport\" .",
                "_:b <http://schema.org/telephone> \"+999\" .",
                "_:c <http://schema.org/name> \"Charlie Airport\" .",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out_wdc, _out_wd, _out_wdc_vals, _out_wd_vals, report, decisions = pipeline._apply_strict_duplicate_key_filter(
        [str(part)],
        ["_:a", "_:b", "_:c"],
        [
            "http://www.wikidata.org/entity/Q1",
            "http://www.wikidata.org/entity/Q2",
            "http://www.wikidata.org/entity/Q3",
        ],
        ["AAA", "AAA", "CCC"],
        ["AAA", "AAA", "CCC"],
    )

    assert out_wdc == ["_:a", "_:c"]
    assert report["summary"]["removed_groups_count"] == 1
    assert report["summary"]["filtered_out_links"] == 1
    removed = [d for d in decisions if d["decision"] == "remove"]
    assert len(removed) == 1


def test_strict_duplicate_key_filter_normalization_is_stable_for_richness_selection(tmp_path):
    part = tmp_path / "part_0.nq"
    part.write_text(
        "\n".join(
            [
                "_:a <http://schema.org/name> \"Aéroport de Paris\"@fr .",
                "_:a <http://schema.org/dateModified> \"2026-01-01\"^^<http://www.w3.org/2001/XMLSchema#date> .",
                "_:b <http://schema.org/name> \"AEROPORT DE PARIS\"@en .",
                "_:b <http://schema.org/dateModified> \"2025-12-31\"^^<http://www.w3.org/2001/XMLSchema#date> .",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out_wdc, _out_wd, _out_wdc_vals, _out_wd_vals, report, _decisions = pipeline._apply_strict_duplicate_key_filter(
        [str(part)],
        ["_:a", "_:b"],
        ["http://www.wikidata.org/entity/Q1", "http://www.wikidata.org/entity/Q2"],
        ["PAR", "PAR"],
        ["PAR", "PAR"],
    )

    assert out_wdc == ["_:a"]
    assert report["summary"]["removed_groups_count"] == 1
    assert report["key_stats_after_filter"]["repeated_key_count"] == 0


def test_strict_duplicate_key_filter_report_contains_required_sections(tmp_path):
    part = tmp_path / "part_0.nq"
    part.write_text(
        "_:a <http://schema.org/name> \"Alpha\" .\n"
        "_:b <http://schema.org/name> \"Beta\" .\n",
        encoding="utf-8",
    )
    _out_wdc, _out_wd, _out_wdc_vals, _out_wd_vals, report, decisions = pipeline._apply_strict_duplicate_key_filter(
        [str(part)],
        ["_:a", "_:b"],
        ["http://www.wikidata.org/entity/Q1", "http://www.wikidata.org/entity/Q2"],
        ["AAA", "AAA"],
        ["AAA", "AAA"],
    )

    assert "summary" in report
    assert "kept_groups" in report
    assert "removed_groups" in report
    assert "entity_decisions" in report
    assert "examples" in report
    assert "key_stats_after_filter" in report
    assert isinstance(decisions, list)
    assert all("signature_hash" in row for row in decisions)


def test_strict_duplicate_key_filter_large_sample_runs(tmp_path):
    part = tmp_path / "part_0.nq"
    lines = []
    wdc_entities = []
    wd_entities = []
    wdc_values = []
    for i in range(200):
        subject = f"_:s{i}"
        lines.append(f'{subject} <http://schema.org/name> "Airport {i}" .')
        lines.append(f'{subject} <http://schema.org/identifier> "K{i:03d}" .')
        wdc_entities.append(subject)
        wd_entities.append(f"http://www.wikidata.org/entity/Q{i+1}")
        wdc_values.append(f"K{i:03d}")
    part.write_text("\n".join(lines) + "\n", encoding="utf-8")

    out_wdc, _out_wd, _out_wdc_vals, _out_wd_vals, report, _decisions = pipeline._apply_strict_duplicate_key_filter(
        [str(part)],
        wdc_entities,
        wd_entities,
        wdc_values,
        wdc_values,
    )
    assert len(out_wdc) == 200
    assert report["summary"]["links_after"] == 200


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
        "pattern_search_in": "predicate",
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


def test_generate_benchmark_sameas_non_wikidata_uses_value_candidates(monkeypatch):
    class_name = "TestClassSameAsNonWikidataCandidates"
    _write_test_parts(class_name)

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)
    monkeypatch.setattr(
        pipeline.align,
        "extract_unique_iris_from_files",
        lambda *args, **kwargs: (
            {
                "httpwwwwikidataorgentityq17146713": [
                    ("https://www.wikidata.org/wiki/Q17146713", "http://example.org/wdc/entity1")
                ]
            },
            1,
        ),
    )

    captured = {}

    def _fetch_target_values(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(pipeline.align, "fetch_target_values", _fetch_target_values)
    monkeypatch.setattr(
        pipeline.align,
        "fetch_wikidata_values",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not query Wikidata in non-wikidata endpoint mode")),
    )

    with pytest.raises(
        pipeline.PipelineError,
        match="No target entities matched class filter",
    ):
        pipeline.generate_benchmark(
            {
                "class_name": class_name,
                "parts_spec": "all",
                "matching_mode": "sameas",
                "wdc_predicate_pattern": "sameAs",
                "target_endpoint": "dbpedia",
                "target_class": "dbo:Museum",
                "use_local_only": True,
                "force_align": True,
            },
            workers=1,
        )

    assert captured["target_property"] == "owl:sameAs"
    assert captured["entity_iris"] is None
    vals = set(captured["value_candidates"])
    assert "http://www.wikidata.org/entity/Q17146713" in vals
    assert "https://www.wikidata.org/entity/Q17146713" in vals


def test_generate_benchmark_sameas_or_property_combines_matches(monkeypatch):
    class_name = "TestClassSameAsOrProperty"
    _write_test_parts(class_name)

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_extra_strip_chars", lambda chars: None)
    monkeypatch.setattr(pipeline.align, "parse_strip_list", lambda text: {" ", "-", "."})
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)

    def _extract(*_args, **kwargs):
        if kwargs.get("wdc_value_is_wd_iri"):
            return (
                {"k_same": [("https://www.wikidata.org/wiki/Q1", "http://example.org/wdc/entity_same")]},
                1,
            )
        return (
            {"k_prop": [("ABC", "http://example.org/wdc/entity_prop")]},
            1,
        )

    monkeypatch.setattr(pipeline.align, "extract_unique_iris_from_files", _extract)

    def _fetch_wikidata_values(prop=None, *_args, **kwargs):
        if prop is None and kwargs.get("entity_iris"):
            return {"k_same": [("http://www.wikidata.org/entity/Q1", "http://www.wikidata.org/entity/Q1")]}
        if prop:
            return {"k_prop": [("ABC", "http://www.wikidata.org/entity/Q2")]}
        return {}

    monkeypatch.setattr(pipeline.align, "fetch_wikidata_values", _fetch_wikidata_values)

    def _fuzzy_link(local_wdc_map, local_wd_map, **_kwargs):
        if "k_same" in local_wdc_map and "k_same" in local_wd_map:
            return (
                [
                    {
                        "wdc_iri": "http://example.org/wdc/entity_same",
                        "wikidata_uri": "http://www.wikidata.org/entity/Q1",
                        "wdc_value": "https://www.wikidata.org/wiki/Q1",
                        "wiki_value": "http://www.wikidata.org/entity/Q1",
                        "method": "exact",
                    }
                ],
                {"https://www.wikidata.org/wiki/Q1"},
            )
        if "k_prop" in local_wdc_map and "k_prop" in local_wd_map:
            return (
                [
                    {
                        "wdc_iri": "http://example.org/wdc/entity_prop",
                        "wikidata_uri": "http://www.wikidata.org/entity/Q2",
                        "wdc_value": "ABC",
                        "wiki_value": "ABC",
                        "method": "exact",
                    }
                ],
                {"ABC"},
            )
        return ([], set())

    monkeypatch.setattr(pipeline.align, "fuzzy_link", _fuzzy_link)

    def export_results(matches, *args, **_kwargs):
        output_dir = args[3]
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        lines = ["wdc_iri\twikidata_uri\twdc_value\twiki_value\tmethod\tmin_len\n"]
        for m in matches:
            lines.append(
                f"{m['wdc_iri']}\t{m['wikidata_uri']}\t{m['wdc_value']}\t{m['wiki_value']}\t{m['method']}\t1\n"
            )
        (out / "wdc_wikidata_links.tsv").write_text("".join(lines), encoding="utf-8")

    monkeypatch.setattr(pipeline.align, "export_results", export_results)
    monkeypatch.setattr(
        pipeline.build,
        "read_links",
        lambda *args, **kwargs: (
            ["http://example.org/wdc/entity_same", "http://example.org/wdc/entity_prop"],
            ["http://www.wikidata.org/entity/Q1", "http://www.wikidata.org/entity/Q2"],
            ["https://www.wikidata.org/wiki/Q1", "ABC"],
            ["http://www.wikidata.org/entity/Q1", "ABC"],
        ),
    )

    def run_pipeline_stub(args, wdc_entities, wd_entities_raw, wdc_values, wd_values, out_dir, *rest, **kwargs):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "ent_links").write_text(
            "wdc_iri\twikidata_uri\n"
            "http://example.org/wdc/entity_same\thttp://www.wikidata.org/entity/Q1\n"
            "http://example.org/wdc/entity_prop\thttp://www.wikidata.org/entity/Q2\n",
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
            "matching_mode": "sameas_or_property",
            "wdc_predicate_pattern": "sameAs",
            "wdc_pattern_search_in": "value",
            "target_property": "P238",
            "target_class": "Q5",
            "ignore_chars": "spaces;-;.",
            "use_local_only": True,
            "force_align": True,
        },
        workers=1,
    )

    links_tsv = Path(result["links_tsv"])
    rows = [ln for ln in links_tsv.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 3


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


def test_generate_benchmark_with_mixed_rule_modes(monkeypatch):
    class_name = "TestClassMixedRuleModes"
    _write_test_parts(class_name)

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_extra_strip_chars", lambda chars: None)
    monkeypatch.setattr(pipeline.align, "parse_strip_list", lambda text: {" ", "-", "."})
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)

    def extract_with_cache_stub(
        work_dir,
        class_name,
        parts_spec,
        decompressed_files,
        pattern,
        search_in,
        wdc_value_is_wd_iri,
        **kwargs,
    ):
        if pattern == "sameAsId" and wdc_value_is_wd_iri:
            return (
                {"q1": [("https://www.wikidata.org/wiki/Q1", "http://example.org/wdc/entity_same")]},
                1,
                False,
            )
        if pattern == "name" and not wdc_value_is_wd_iri:
            return (
                {"alpha node": [("Alpha Node", "http://example.org/wdc/entity_prop")]},
                1,
                False,
            )
        return ({}, 0, False)

    monkeypatch.setattr(pipeline, "_extract_wdc_values_with_cache", extract_with_cache_stub)

    def fetch_wikidata_values_stub(wikidata_property=None, wkd_class=None, wkd_prop_class=None, entity_iris=None, **kwargs):
        if entity_iris:
            return {"q1": [("http://www.wikidata.org/entity/Q1", "http://www.wikidata.org/entity/Q1")]}
        if wikidata_property == "rdfs:label":
            return {"alpha node": [("Alpha Node", "http://www.wikidata.org/entity/Q2")]}
        return {}

    monkeypatch.setattr(pipeline.align, "fetch_wikidata_values", fetch_wikidata_values_stub)

    def fuzzy_link_stub(wdc_map, wikidata_map, **kwargs):
        if "q1" in wdc_map:
            return (
                [
                    {
                        "wdc_iri": "http://example.org/wdc/entity_same",
                        "wikidata_uri": "http://www.wikidata.org/entity/Q1",
                        "wdc_value": "https://www.wikidata.org/wiki/Q1",
                        "wiki_value": "http://www.wikidata.org/entity/Q1",
                        "method": "exact",
                    }
                ],
                {"q1"},
            )
        if "alpha node" in wdc_map:
            return (
                [
                    {
                        "wdc_iri": "http://example.org/wdc/entity_prop",
                        "wikidata_uri": "http://www.wikidata.org/entity/Q2",
                        "wdc_value": "Alpha Node",
                        "wiki_value": "Alpha Node",
                        "method": "exact",
                    }
                ],
                {"alpha node"},
            )
        return ([], set())

    monkeypatch.setattr(pipeline.align, "fuzzy_link", fuzzy_link_stub)

    def export_results_stub(matches, wdc_values_matched, wdc_map, wikidata_map, output_dir, **kwargs):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        with (out / "wdc_wikidata_links.tsv").open("w", encoding="utf-8") as f:
            f.write("wdc_iri\twikidata_uri\twdc_value\twiki_value\tmethod\tmin_len\n")
            for m in matches:
                f.write(
                    f"{m['wdc_iri']}\t{m['wikidata_uri']}\t{m['wdc_value']}\t{m['wiki_value']}\t{m['method']}\t1\n"
                )

    monkeypatch.setattr(pipeline.align, "export_results", export_results_stub)

    monkeypatch.setattr(
        pipeline.build,
        "read_links",
        lambda *args, **kwargs: (
            ["http://example.org/wdc/entity_same", "http://example.org/wdc/entity_prop"],
            ["http://www.wikidata.org/entity/Q1", "http://www.wikidata.org/entity/Q2"],
            ["Q1", "Alpha Node"],
            ["Q1", "Alpha Node"],
        ),
    )

    def run_pipeline_stub(args, wdc_entities, wd_entities_raw, wdc_values, wd_values, out_dir, *rest, **kwargs):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "ent_links").write_text(
            "wdc_iri\twikidata_uri\n"
            "http://example.org/wdc/entity_same\thttp://www.wikidata.org/entity/Q1\n"
            "http://example.org/wdc/entity_prop\thttp://www.wikidata.org/entity/Q2\n",
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
            "property_mapping_rules": 'sameAsId => || {"mode":"sameas"}\nname => rdfs:label',
            "wikidata_property": "",
            "wkd_class": "Q5",
            "ignore_chars": "spaces;-;.",
            "use_local_only": True,
            "force_align": True,
        },
        workers=1,
    )

    links_tsv = Path(result["links_tsv"])
    rows = [ln for ln in links_tsv.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 3


def test_parse_property_mapping_rules_accepts_sameas_mode_without_target_property():
    rows = pipeline._parse_property_mapping_rules('sameAs => || {"mode":"sameas"}')
    assert len(rows) == 1
    assert rows[0]["mode"] == "sameas"
    assert rows[0]["pairs"] == [("sameAs", "")]


@pytest.mark.parametrize(
    "class_name,property_mapping_rules,extract_payloads,fetch_payloads,expected_extract_calls,expected_fetch_calls",
    [
        (
            "TestSinglePropNameRule",
            "name => rdfs:label",
            {
                "name": ({"alpha node": [("Alpha Node", "http://example.org/wdc/entity1")]}, 1),
            },
            {
                "rdfs:label": {"alpha node": [("Alpha Node", "http://www.wikidata.org/entity/Q1001")]},
            },
            ["name"],
            ["rdfs:label"],
        ),
        (
            "TestSinglePropCodeRule",
            "code => P528",
            {
                "code": ({"x-001": [("X-001", "http://example.org/wdc/entity2")]}, 1),
            },
            {
                "P528": {"x-001": [("X-001", "http://www.wikidata.org/entity/Q2002")]},
            },
            ["code"],
            ["P528"],
        ),
    ],
)
def test_generate_benchmark_property_mapping_single_prop_classes(
    monkeypatch,
    class_name,
    property_mapping_rules,
    extract_payloads,
    fetch_payloads,
    expected_extract_calls,
    expected_fetch_calls,
):
    _write_test_parts(class_name)

    monkeypatch.setattr(pipeline.align, "set_normalization", lambda enabled: None)
    monkeypatch.setattr(pipeline.align, "set_extra_strip_chars", lambda chars: None)
    monkeypatch.setattr(pipeline.align, "parse_strip_list", lambda text: {" ", "-", "."})
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)

    extract_calls = []
    fetch_calls = []

    def extract_unique_iris_from_files(files, pattern, **kwargs):
        extract_calls.append(pattern)
        return extract_payloads.get(pattern, ({}, 0))

    monkeypatch.setattr(pipeline.align, "extract_unique_iris_from_files", extract_unique_iris_from_files)

    def fetch_wikidata_values(wikidata_property, wkd_class, wkd_prop_class):
        fetch_calls.append(wikidata_property)
        return fetch_payloads.get(wikidata_property, {})

    monkeypatch.setattr(pipeline.align, "fetch_wikidata_values", fetch_wikidata_values)

    def fuzzy_link(wdc_map, wikidata_map, **kwargs):
        out = []
        matched = set()
        for norm, wdc_entries in (wdc_map or {}).items():
            wd_entries = (wikidata_map or {}).get(norm) or []
            if not wd_entries:
                continue
            for wdc_val, wdc_iri in wdc_entries:
                wd_val, wd_iri = wd_entries[0]
                out.append(
                    {
                        "wdc_iri": wdc_iri,
                        "wikidata_uri": wd_iri,
                        "wdc_value": wdc_val,
                        "wiki_value": wd_val,
                        "method": "exact",
                    }
                )
                matched.add(norm)
        return (out, matched)

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
            "property_mapping_rules": property_mapping_rules,
            "wikidata_property": "",
            "wkd_class": "Q1248784",
            "ignore_chars": "spaces;-;.",
            "use_local_only": True,
            "force_align": True,
        },
        workers=1,
    )

    assert result["class_name"] == class_name
    assert extract_calls == expected_extract_calls
    assert fetch_calls == expected_fetch_calls
    out_dir = Path(result["out_dir"])
    cfg = json.loads((out_dir / "BUILD_CONFIG.json").read_text(encoding="utf-8"))
    assert cfg["property_mapping_rules"] == property_mapping_rules


def test_generate_benchmark_property_mapping_multi_prop_with_per_pair_normalization(monkeypatch):
    class_name = "TestMultiPropPairNorms"
    _write_test_parts(class_name)

    normalize_enabled_calls = []
    normalize_specs = []
    extract_calls = []
    fetch_calls = []

    monkeypatch.setattr(
        pipeline.align,
        "set_normalization",
        lambda enabled: normalize_enabled_calls.append(bool(enabled)),
    )
    monkeypatch.setattr(pipeline.align, "set_extra_strip_chars", lambda chars: None)

    def parse_strip_list(text):
        spec = str(text or "")
        normalize_specs.append(spec)
        return {" "}

    monkeypatch.setattr(pipeline.align, "parse_strip_list", parse_strip_list)
    monkeypatch.setattr(pipeline.align, "set_cancel_checker", lambda checker: None)

    extract_payloads = {
        "name": ({"alpha node": [("Alpha Node", "http://example.org/wdc/entity1")]}, 1),
        "iata": ({"abc": [("ABC", "http://example.org/wdc/entity1")]}, 1),
        "telephone": ({"123456": [("123-456", "http://example.org/wdc/entity1")]}, 1),
    }
    fetch_payloads = {
        "rdfs:label": {"alpha node": [("Alpha Node", "http://www.wikidata.org/entity/Q1001")]},
        "P238": {"abc": [("ABC", "http://www.wikidata.org/entity/Q1001")]},
        "P1329": {"123456": [("123456", "http://www.wikidata.org/entity/Q1001")]},
    }

    def extract_unique_iris_from_files(files, pattern, **kwargs):
        extract_calls.append(pattern)
        return extract_payloads.get(pattern, ({}, 0))

    monkeypatch.setattr(pipeline.align, "extract_unique_iris_from_files", extract_unique_iris_from_files)

    def fetch_wikidata_values(wikidata_property, wkd_class, wkd_prop_class):
        fetch_calls.append(wikidata_property)
        return fetch_payloads.get(wikidata_property, {})

    monkeypatch.setattr(pipeline.align, "fetch_wikidata_values", fetch_wikidata_values)

    def fuzzy_link(wdc_map, wikidata_map, **kwargs):
        out = []
        matched = set()
        for norm, wdc_entries in (wdc_map or {}).items():
            wd_entries = (wikidata_map or {}).get(norm) or []
            if not wd_entries:
                continue
            for wdc_val, wdc_iri in wdc_entries:
                wd_val, wd_iri = wd_entries[0]
                out.append(
                    {
                        "wdc_iri": wdc_iri,
                        "wikidata_uri": wd_iri,
                        "wdc_value": wdc_val,
                        "wiki_value": wd_val,
                        "method": "exact",
                    }
                )
                matched.add(norm)
        return (out, matched)

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

    property_mapping_rules = (
        'name,iata,telephone => rdfs:label,P238,P1329 || ["spaces;dot","hyphen","slash"]'
    )
    result = pipeline.generate_benchmark(
        {
            "class_name": class_name,
            "parts_spec": "all",
            "matching_mode": "property",
            "wdc_predicate_pattern": "",
            "property_mapping_rules": property_mapping_rules,
            "wikidata_property": "",
            "wkd_class": "Q1248784",
            "ignore_chars": "spaces;-;.",
            "use_local_only": True,
            "force_align": True,
        },
        workers=1,
    )

    assert result["class_name"] == class_name
    assert extract_calls == ["name", "iata", "telephone"]
    assert fetch_calls == ["rdfs:label", "P238", "P1329"]
    assert normalize_specs == ["spaces;-;.", "spaces;dot", "hyphen", "slash", "spaces;-;."]
    assert normalize_enabled_calls.count(True) >= 5

    out_dir = Path(result["out_dir"])
    cfg = json.loads((out_dir / "BUILD_CONFIG.json").read_text(encoding="utf-8"))
    assert cfg["property_mapping_rules"] == property_mapping_rules
