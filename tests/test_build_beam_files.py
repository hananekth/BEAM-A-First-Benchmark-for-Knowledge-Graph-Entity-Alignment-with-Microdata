from pathlib import Path
from types import SimpleNamespace

from scripts import build_beam_files as build


def test_transform_triple_normalizes_wikidata_uri_tokens():
    s, p, o = build.transform_triple(
        "<http://www.wikidata.org/entity/Q42>",
        "<http://www.wikidata.org/prop/direct/P31>",
        "<http://www.wikidata.org/entity/Q5>",
        lowercase=True,
    )
    assert s == "http://www.wikidata.org/entity/q42"
    assert p == "http://www.wikidata.org/prop/direct/p31"
    assert o == "http://www.wikidata.org/entity/q5"


def test_canonical_wd_entity_uri_normalizes_lowercase_ids():
    assert build.canonical_wd_entity_uri("http://www.wikidata.org/entity/q574") == "http://www.wikidata.org/entity/Q574"
    assert build.canonical_wd_entity_uri("http://www.wikidata.org/entity/P31") == "http://www.wikidata.org/entity/P31"


def test_canonical_wd_link_entity_uri_accepts_wiki_or_entity_forms():
    assert (
        build.canonical_wd_link_entity_uri("https://www.wikidata.org/wiki/Q174224")
        == "http://www.wikidata.org/entity/Q174224"
    )
    assert (
        build.canonical_wd_link_entity_uri("<http://www.wikidata.org/entity/q918195>")
        == "http://www.wikidata.org/entity/Q918195"
    )
    assert (
        build.canonical_wd_link_entity_uri("https://m.wikidata.org/wiki/Special:EntityPage/Q64?uselang=en")
        == "http://www.wikidata.org/entity/Q64"
    )


def test_write_links_outputs_wdc_iri_and_canonical_wikidata_iri(tmp_path):
    out = tmp_path / "ent_links"
    build.write_links(
        str(out),
        ["<https://example.org/wdc/e1>"],
        ["https://www.wikidata.org/wiki/Q64"],
        dedupe=True,
    )
    assert out.read_text(encoding="utf-8") == "https://example.org/wdc/e1\thttp://www.wikidata.org/entity/Q64\n"


def test_append_wdc_labels_descriptions_strips_literal_suffixes_and_matches_iris():
    class_dir = Path("Download") / "TestClass"
    class_dir.mkdir(parents=True, exist_ok=True)
    wdc_part = class_dir / "part_0001.nq"
    wdc_part.write_text(
        "<http://example.org/entity/b> <http://www.w3.org/2000/01/rdf-schema#label> \"Entity B\"@en-gb .\n"
        "<http://example.org/entity/b> <http://schema.org/description> \"Description B\"^^<http://www.w3.org/2001/XMLSchema#string> .\n"
        "<http://schema.org/url> <http://www.w3.org/2000/01/rdf-schema#label> \"URL label\"@en .\n",
        encoding="utf-8",
    )

    out_dir = Path("data") / "TestClass" / "beam_test" / "with_link_code"
    out_dir.mkdir(parents=True, exist_ok=True)
    attr_path = out_dir / "attr_triples_1"
    rel_path = out_dir / "rel_triples_1"
    attr_path.write_text(
        "<http://example.org/entity/a>\t<http://schema.org/name>\t\"Alice\"\n",
        encoding="utf-8",
    )
    rel_path.write_text(
        "<http://example.org/entity/a>\t<http://schema.org/url>\t<http://example.org/entity/b>\n",
        encoding="utf-8",
    )

    build.append_wdc_labels_descriptions(str(attr_path), str(rel_path), [str(wdc_part)])
    content = attr_path.read_text(encoding="utf-8")

    assert "<http://example.org/entity/b>\t<http://www.w3.org/2000/01/rdf-schema#label>\t\"Entity B\"" in content
    assert "<http://example.org/entity/b>\t<http://schema.org/description>\t\"Description B\"" in content
    assert "<http://schema.org/url>\t<http://www.w3.org/2000/01/rdf-schema#label>\t\"URL label\"" in content
    assert "@en" not in content
    assert "^^<http://" not in content


def test_write_prop_stats_wdc_resolves_property_labels():
    class_dir = Path("Download") / "TestClass"
    class_dir.mkdir(parents=True, exist_ok=True)
    wdc_part = class_dir / "part_0002.nq"
    wdc_part.write_text(
        "<http://schema.org/url> <http://www.w3.org/2000/01/rdf-schema#label> \"URL label\"@en .\n"
        "<http://schema.org/url> <http://schema.org/description> \"URL description\"@en .\n",
        encoding="utf-8",
    )

    out_dir = Path("data") / "TestClass" / "beam_test_stats" / "with_link_code"
    out_dir.mkdir(parents=True, exist_ok=True)
    attr_path = out_dir / "attr_triples_1"
    rel_path = out_dir / "rel_triples_1"
    stats_path = out_dir / "prop_stats_wdc.tsv"

    attr_path.write_text(
        "<http://example.org/entity/a>\t<http://schema.org/name>\t\"Alice\"\n",
        encoding="utf-8",
    )
    rel_path.write_text(
        "<http://example.org/entity/a>\t<http://schema.org/url>\t<http://example.org/entity/b>\n",
        encoding="utf-8",
    )

    build.write_prop_stats_wdc(str(stats_path), str(attr_path), str(rel_path), [str(wdc_part)])
    rows = stats_path.read_text(encoding="utf-8").splitlines()

    assert rows[0] == "predicate\tcount\tlabel\tdescription"
    assert any(
        row.startswith("<http://schema.org/url>\t1\tURL label\tURL description")
        for row in rows[1:]
    )


def test_append_labels_descriptions_enriches_wikidata_entities_and_props(monkeypatch):
    out_dir = Path("data") / "TestClass" / "beam_test_wd_labels" / "without_link_code"
    out_dir.mkdir(parents=True, exist_ok=True)
    attr_path = out_dir / "attr_triples_2"
    rel_path = out_dir / "rel_triples_2"
    attr_path.write_text(
        "<http://www.wikidata.org/entity/Q1>\t<http://schema.org/name>\t\"Entity one\"\n",
        encoding="utf-8",
    )
    rel_path.write_text(
        "<http://www.wikidata.org/entity/Q1>\t<http://www.wikidata.org/prop/direct/P31>\t<http://www.wikidata.org/entity/Q5>\n",
        encoding="utf-8",
    )

    def _fake_fetch(uris, endpoint, language, batch_size, sleep_s, timeout, retries, backoff):
        assert "http://www.wikidata.org/entity/Q1" in uris
        assert "http://www.wikidata.org/entity/Q5" in uris
        assert "http://www.wikidata.org/entity/P31" in uris
        return [
            ("http://www.wikidata.org/entity/Q1", "http://www.w3.org/2000/01/rdf-schema#label", "\"Entity 1\""),
            ("http://www.wikidata.org/entity/Q1", "http://schema.org/description", "\"Entity one desc\""),
            ("http://www.wikidata.org/entity/Q5", "http://www.w3.org/2000/01/rdf-schema#label", "\"human\""),
            ("http://www.wikidata.org/entity/P31", "http://www.w3.org/2000/01/rdf-schema#label", "\"instance of\""),
            ("http://www.wikidata.org/entity/P31", "http://schema.org/description", "\"property desc\""),
        ]

    monkeypatch.setattr(build, "fetch_wd_labels_descriptions", _fake_fetch)

    build.append_labels_descriptions(
        str(attr_path),
        str(rel_path),
        endpoint="https://query.wikidata.org/sparql",
        language="en",
        batch_size=50,
        sleep_s=0,
        timeout=30,
        retries=1,
        backoff=2,
        lowercase_wd=True,
    )
    content = attr_path.read_text(encoding="utf-8")

    assert "http://www.wikidata.org/entity/q1\thttp://www.w3.org/2000/01/rdf-schema#label\t\"Entity 1\"" in content
    assert "http://www.wikidata.org/entity/q1\thttp://schema.org/description\t\"Entity one desc\"" in content
    assert "http://www.wikidata.org/entity/q5\thttp://www.w3.org/2000/01/rdf-schema#label\t\"human\"" in content
    assert "http://www.wikidata.org/prop/direct/p31\thttp://www.w3.org/2000/01/rdf-schema#label\t\"instance of\"" in content
    assert "http://www.wikidata.org/prop/direct/p31\thttp://schema.org/description\t\"property desc\"" in content


def test_write_prop_stats_resolves_wikidata_prop_labels_with_bracketed_preds(monkeypatch):
    out_dir = Path("data") / "TestClass" / "beam_test_wd_stats" / "without_link_code"
    out_dir.mkdir(parents=True, exist_ok=True)
    attr_path = out_dir / "attr_triples_2"
    rel_path = out_dir / "rel_triples_2"
    stats_path = out_dir / "prop_stats_wd.tsv"

    attr_path.write_text("", encoding="utf-8")
    rel_path.write_text(
        "<http://www.wikidata.org/entity/Q1>\t<http://www.wikidata.org/prop/direct/P31>\t<http://www.wikidata.org/entity/Q5>\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        build,
        "fetch_wd_label_desc_map",
        lambda uris, endpoint, language, batch_size, sleep_s, timeout, retries, backoff: {
            "http://www.wikidata.org/entity/P31": {"label": "instance of", "desc": "class membership"}
        },
    )

    build.write_prop_stats(
        str(stats_path),
        str(attr_path),
        str(rel_path),
        endpoint="https://query.wikidata.org/sparql",
        language="en",
        batch_size=50,
        sleep_s=0,
        timeout=30,
        retries=1,
        backoff=2,
    )
    rows = stats_path.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "predicate\tcount\tlabel\tdescription"
    assert any(
        row.startswith("<http://www.wikidata.org/prop/direct/P31>\t1\tinstance of\tclass membership")
        for row in rows[1:]
    )


def test_fetch_wd_label_desc_map_fills_fallbacks(monkeypatch):
    monkeypatch.setattr(
        build,
        "fetch_wd_labels_descriptions",
        lambda uris, endpoint, language, batch_size, sleep_s, timeout, retries, backoff: [
            ("http://www.wikidata.org/entity/Q1", "http://www.w3.org/2000/01/rdf-schema#label", "\"Universe\"")
        ],
    )

    labels = build.fetch_wd_label_desc_map(
        {"http://www.wikidata.org/entity/Q1", "http://www.wikidata.org/entity/Q2"},
        endpoint="https://query.wikidata.org/sparql",
        language="en",
        batch_size=50,
        sleep_s=0,
        timeout=30,
        retries=1,
        backoff=2,
    )
    assert labels["http://www.wikidata.org/entity/Q1"]["label"] == "Universe"
    assert labels["http://www.wikidata.org/entity/Q1"]["desc"] == "Universe"
    assert labels["http://www.wikidata.org/entity/Q2"]["label"] == "Q2"
    assert labels["http://www.wikidata.org/entity/Q2"]["desc"] == "Q2"


def test_write_wikidata_from_sparql_excludes_props_case_insensitively(monkeypatch):
    out_dir = Path("data") / "TestClass" / "beam_test_exclude_case" / "without_link_code"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_attr = out_dir / "attr_triples_2"
    out_rel = out_dir / "rel_triples_2"

    def _fake_construct(*args, **kwargs):
        yield 1, (
            "<http://www.wikidata.org/entity/Q1>",
            "<http://www.wikidata.org/prop/direct/P31>",
            "<http://www.wikidata.org/entity/Q5>",
        )
        yield 1, None

    monkeypatch.setattr(build, "sparql_construct", _fake_construct)

    build.write_wikidata_from_sparql(
        endpoint="https://query.wikidata.org/sparql",
        subjects=["http://www.wikidata.org/entity/Q1"],
        out_attr_path=str(out_attr),
        out_rel_path=str(out_rel),
        lowercase_wd=True,
        language="en",
        batch_size=50,
        sleep_s=0.0,
        timeout=30,
        retries=1,
        backoff=2.0,
        exclude_props={"http://www.wikidata.org/prop/direct/p31"},
    )

    assert out_attr.read_text(encoding="utf-8") == ""
    assert out_rel.read_text(encoding="utf-8") == ""


def test_write_wikidata_from_sparql_deduplicates_subjects(monkeypatch):
    out_dir = Path("data") / "TestClass" / "beam_test_wd_dedupe" / "without_link_code"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_attr = out_dir / "attr_triples_2"
    out_rel = out_dir / "rel_triples_2"
    seen = {}

    def _fake_construct(endpoint, subjects, language, batch_size, sleep_s, timeout, retries, backoff, start_batch, session=None):
        seen["subjects"] = list(subjects)
        for idx, subj in enumerate(subjects, start=1):
            yield idx, (
                f"<{subj}>",
                "<http://www.wikidata.org/prop/direct/P31>",
                "<http://www.wikidata.org/entity/Q5>",
            )
            yield idx, None

    monkeypatch.setattr(build, "sparql_construct", _fake_construct)

    build.write_wikidata_from_sparql(
        endpoint="https://query.wikidata.org/sparql",
        subjects=[
            "http://www.wikidata.org/entity/Q1",
            "http://www.wikidata.org/entity/Q1",
            "http://www.wikidata.org/entity/Q2",
        ],
        out_attr_path=str(out_attr),
        out_rel_path=str(out_rel),
        lowercase_wd=True,
        language="en",
        batch_size=50,
        sleep_s=0.0,
        timeout=30,
        retries=1,
        backoff=2.0,
    )

    assert seen["subjects"] == [
        "http://www.wikidata.org/entity/Q1",
        "http://www.wikidata.org/entity/Q2",
    ]


def test_write_wikidata_from_sparql_reuses_raw_cache(monkeypatch):
    out_dir = Path("data") / "TestClass" / "beam_test_wd_cache" / "without_link_code"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_attr_1 = out_dir / "attr_triples_2.first"
    out_rel_1 = out_dir / "rel_triples_2.first"
    out_attr_2 = out_dir / "attr_triples_2.second"
    out_rel_2 = out_dir / "rel_triples_2.second"
    raw_cache = out_dir / ".wd_raw_cache.tsv"
    calls = {"count": 0}

    def _fake_construct(endpoint, subjects, language, batch_size, sleep_s, timeout, retries, backoff, start_batch, session=None):
        calls["count"] += 1
        yield 1, (
            "<http://www.wikidata.org/entity/Q1>",
            "<http://www.wikidata.org/prop/direct/P31>",
            "<http://www.wikidata.org/entity/Q5>",
        )
        yield 1, (
            "<http://www.wikidata.org/entity/Q1>",
            "<http://www.w3.org/2000/01/rdf-schema#label>",
            "\"Entity One\"@en",
        )
        yield 1, None

    monkeypatch.setattr(build, "sparql_construct", _fake_construct)

    build.write_wikidata_from_sparql(
        endpoint="https://query.wikidata.org/sparql",
        subjects=["http://www.wikidata.org/entity/Q1"],
        out_attr_path=str(out_attr_1),
        out_rel_path=str(out_rel_1),
        lowercase_wd=True,
        language="en",
        batch_size=50,
        sleep_s=0.0,
        timeout=30,
        retries=1,
        backoff=2.0,
        raw_triples_cache_path=str(raw_cache),
    )

    assert calls["count"] == 1
    assert raw_cache.exists()

    def _fail_construct(*args, **kwargs):
        raise AssertionError("sparql_construct should not be called when cache exists")

    monkeypatch.setattr(build, "sparql_construct", _fail_construct)

    build.write_wikidata_from_sparql(
        endpoint="https://query.wikidata.org/sparql",
        subjects=["http://www.wikidata.org/entity/Q1"],
        out_attr_path=str(out_attr_2),
        out_rel_path=str(out_rel_2),
        lowercase_wd=True,
        language="en",
        batch_size=50,
        sleep_s=0.0,
        timeout=30,
        retries=1,
        backoff=2.0,
        raw_triples_cache_path=str(raw_cache),
    )

    assert out_attr_1.read_text(encoding="utf-8") == out_attr_2.read_text(encoding="utf-8")
    assert out_rel_1.read_text(encoding="utf-8") == out_rel_2.read_text(encoding="utf-8")


def test_write_wikidata_from_sparql_keeps_only_linked_entities_in_relations(monkeypatch):
    out_dir = Path("data") / "TestClass" / "beam_test_wd_linked_only" / "without_link_code"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_attr = out_dir / "attr_triples_2"
    out_rel = out_dir / "rel_triples_2"

    def _fake_construct(*args, **kwargs):
        yield 1, (
            "<http://www.wikidata.org/entity/Q1>",
            "<http://www.wikidata.org/prop/direct/P31>",
            "<http://www.wikidata.org/entity/Q5>",
        )
        yield 1, (
            "<http://www.wikidata.org/entity/Q1>",
            "<http://www.wikidata.org/prop/direct/P31>",
            "<http://www.wikidata.org/entity/Q999>",
        )
        yield 1, None

    monkeypatch.setattr(build, "sparql_construct", _fake_construct)

    build.write_wikidata_from_sparql(
        endpoint="https://query.wikidata.org/sparql",
        subjects=["http://www.wikidata.org/entity/Q1"],
        out_attr_path=str(out_attr),
        out_rel_path=str(out_rel),
        lowercase_wd=True,
        language="en",
        batch_size=50,
        sleep_s=0.0,
        timeout=30,
        retries=1,
        backoff=2.0,
        linked_entity_iris=[
            "http://www.wikidata.org/entity/Q1",
            "http://www.wikidata.org/entity/Q5",
        ],
    )

    rel_text = out_rel.read_text(encoding="utf-8")
    assert "http://www.wikidata.org/entity/q5" in rel_text
    assert "http://www.wikidata.org/entity/q999" not in rel_text


def test_run_pipeline_keeps_only_linked_entities_for_wdc_and_wikidata(monkeypatch, tmp_path):
    wdc_nq = tmp_path / "wdc.nq"
    wd_nq = tmp_path / "wd.nq"
    out_dir = tmp_path / "beam_out"

    wdc_nq.write_text(
        "<http://example.org/wdc/a> <http://schema.org/name> \"A\" .\n"
        "<http://example.org/wdc/a> <http://schema.org/relatedLink> <http://example.org/wdc/b> .\n"
        "<http://example.org/wdc/a> <http://schema.org/relatedLink> <http://example.org/wdc/c> .\n"
        "<http://example.org/wdc/b> <http://schema.org/name> \"B\" .\n"
        "<http://example.org/wdc/c> <http://schema.org/name> \"C\" .\n",
        encoding="utf-8",
    )
    wd_nq.write_text(
        "<http://www.wikidata.org/entity/Q1> <http://www.wikidata.org/prop/direct/P31> <http://www.wikidata.org/entity/Q5> .\n"
        "<http://www.wikidata.org/entity/Q1> <http://www.wikidata.org/prop/direct/P31> <http://www.wikidata.org/entity/Q999> .\n"
        "<http://www.wikidata.org/entity/Q5> <http://www.w3.org/2000/01/rdf-schema#label> \"Human\"@en .\n"
        "<http://www.wikidata.org/entity/Q999> <http://www.w3.org/2000/01/rdf-schema#label> \"Ghost\"@en .\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(build, "append_wdc_labels_descriptions", lambda *args, **kwargs: None)
    monkeypatch.setattr(build, "write_prop_stats_wdc", lambda *args, **kwargs: None)
    monkeypatch.setattr(build, "write_prop_stats", lambda *args, **kwargs: None)

    args = SimpleNamespace(
        wdc_nq=[str(wdc_nq)],
        wd_nq=str(wd_nq),
        dedupe_links=False,
        max_depth=-1,
        progress_every=0,
        wd_prop_min_count=0,
        no_wd_labels=True,
        sparql_url="https://query.wikidata.org/sparql",
        lang="en",
        batch_size=50,
        sleep=0.0,
        timeout=30,
        retries=1,
        backoff=2.0,
        resume=False,
        state_file=None,
        linked_only_entities=True,
    )

    build.run_pipeline(
        args,
        wdc_entities=[
            "<http://example.org/wdc/a>",
            "<http://example.org/wdc/b>",
        ],
        wd_entities_raw=[
            "http://www.wikidata.org/entity/Q1",
            "http://www.wikidata.org/entity/Q5",
        ],
        wdc_values=[],
        wd_values=[],
        out_dir=str(out_dir),
        wdc_mask_values=None,
        wd_mask_values=None,
        wdc_exclude_props=set(),
        wdc_exclude_prop_patterns=set(),
        wd_exclude_props=set(),
        replace_map={},
        lowercase_wd=True,
        add_wd_labels=False,
    )

    rel_wdc = (out_dir / "rel_triples_1").read_text(encoding="utf-8")
    attr_wdc = (out_dir / "attr_triples_1").read_text(encoding="utf-8")
    rel_wd = (out_dir / "rel_triples_2").read_text(encoding="utf-8")

    assert "http://example.org/wdc/c" not in rel_wdc
    assert "http://example.org/wdc/c" not in attr_wdc
    assert "http://www.wikidata.org/entity/q999" not in rel_wd
    assert "http://www.wikidata.org/entity/q5" in rel_wd
