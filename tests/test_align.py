from scripts import align


def test_fuzzy_link_exact_allows_short_values():
    wdc_map = {
        "fr": [("FR", "http://example.org/wdc/country_fr")],
        "jp": [("JP", "http://example.org/wdc/country_jp")],
    }
    wikidata_map = {
        "fr": [("FR", "http://www.wikidata.org/entity/Q142")],
        "jp": [("JP", "http://www.wikidata.org/entity/Q17")],
    }

    matches, matched_values = align.fuzzy_link(
        wdc_map,
        wikidata_map,
        parallel=False,
    )

    assert len(matches) == 2
    assert {m["method"] for m in matches} == {"exact"}
    assert matched_values == {"FR", "JP"}


def test_retryable_query_error_detection():
    assert align._is_retryable_query_error(Exception("IncompleteRead(123 bytes read)"))
    assert align._is_retryable_query_error(Exception("Remote disconnected while reading response"))
    assert align._is_retryable_query_error(Exception("Read timed out"))
    assert align._is_retryable_query_error(Exception("Invalid control character at line 2"))
    assert not align._is_retryable_query_error(Exception("invalid query syntax"))


def test_fetch_wikidata_values_handles_control_chars(monkeypatch):
    payload = (
        '{"results":{"bindings":[{"entity":{"value":"http://www.wikidata.org/entity/Q1"},'
        '"value":{"value":"alpha\x01beta"}}]}}'
    )

    class _Resp:
        text = payload

        @staticmethod
        def raise_for_status():
            return None

    def _fake_post(*args, **kwargs):
        return _Resp()

    monkeypatch.setattr(align.requests, "post", _fake_post)

    result = align.fetch_wikidata_values("rdfs:label", "Q5", None)

    assert "alphabeta" in result
    assert result["alphabeta"][0][1] == "http://www.wikidata.org/entity/Q1"


def test_extract_unique_iris_wikidata_mode_accepts_iri_objects(tmp_path):
    part = tmp_path / "part_0.nq"
    part.write_text(
        "<http://example.org/s1> <http://schema.org/sameAs> <https://www.wikidata.org/wiki/Q174224> <http://example.org/g> .\n"
        "_:b0 <http://schema.org/sameAs> <http://www.wikidata.org/entity/Q42> <http://example.org/g> .\n"
        "<http://example.org/s2> <http://schema.org/sameAs> \"https://www.wikidata.org/wiki/Q123\" <http://example.org/g> .\n"
        "<http://example.org/s3> <http://schema.org/sameAs> <https://en.wikipedia.org/wiki/Paris> <http://example.org/g> .\n"
        "<http://example.org/s4> <http://schema.org/name> \"not sameAs\" <http://example.org/g> .\n",
        encoding="utf-8",
    )

    value_map, matched_count = align.extract_unique_iris_from_files(
        [part],
        pattern="sameAs",
        collect_top_props=False,
        parallel=False,
        progress_every=0,
        wdc_value_is_wd_iri=True,
    )

    assert matched_count == 4
    all_values = {v for entries in value_map.values() for (v, _s) in entries}
    assert "https://www.wikidata.org/wiki/Q174224" in all_values
    assert "http://www.wikidata.org/entity/Q42" in all_values
    assert "https://www.wikidata.org/wiki/Q123" in all_values
    assert "https://en.wikipedia.org/wiki/Paris" not in all_values

    q174224_norm = align.normalize_country_code(
        align.normalize_for_matching("http://www.wikidata.org/entity/Q174224")
    )
    assert q174224_norm in value_map
    assert any(v == "https://www.wikidata.org/wiki/Q174224" for v, _s in value_map[q174224_norm])


def test_extract_unique_iris_literal_mode_ignores_iri_objects(tmp_path):
    part = tmp_path / "part_0.nq"
    part.write_text(
        "<http://example.org/s1> <http://schema.org/sameAs> <https://www.wikidata.org/wiki/Q174224> <http://example.org/g> .\n"
        "<http://example.org/s2> <http://schema.org/sameAs> \"Alpha\" <http://example.org/g> .\n",
        encoding="utf-8",
    )

    value_map, matched_count = align.extract_unique_iris_from_files(
        [part],
        pattern="sameAs",
        collect_top_props=False,
        parallel=False,
        progress_every=0,
        wdc_value_is_wd_iri=False,
    )

    assert matched_count == 2
    all_values = {v for entries in value_map.values() for (v, _s) in entries}
    assert all_values == {"Alpha"}


def test_extract_wd_entity_iri_accepts_wiki_variants():
    assert (
        align.extract_wd_entity_iri("https://www.wikidata.org/wiki/Q174224")
        == "http://www.wikidata.org/entity/Q174224"
    )
    assert (
        align.extract_wd_entity_iri("https://www.wikidata.org/wiki/Special:EntityPage/Q64")
        == "http://www.wikidata.org/entity/Q64"
    )
    assert (
        align.extract_wd_entity_iri("https://m.wikidata.org/wiki/Q90?uselang=en")
        == "http://www.wikidata.org/entity/Q90"
    )
    assert (
        align.extract_wd_entity_iri("wd:Q42")
        == "http://www.wikidata.org/entity/Q42"
    )
    assert align.extract_wd_entity_iri("https://en.wikipedia.org/wiki/Q90") is None


def test_fetch_wikidata_values_uses_persistent_cache(monkeypatch, tmp_path):
    cache_dir = tmp_path / "wd_cache"
    monkeypatch.setenv("WIKIDATA_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("WIKIDATA_CACHE_TTL_S", "3600")
    monkeypatch.setenv("WIKIDATA_CACHE_DISABLED", "0")

    payload = (
        '{"results":{"bindings":[{"entity":{"value":"http://www.wikidata.org/entity/Q1"},'
        '"value":{"value":"Alpha"}}]}}'
    )

    class _Resp:
        text = payload

        @staticmethod
        def raise_for_status():
            return None

    calls = {"n": 0}

    def _fake_post(*args, **kwargs):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(align.requests, "post", _fake_post)
    first = align.fetch_wikidata_values("rdfs:label", "Q5", None)
    assert first
    assert calls["n"] == 1

    def _fail_post(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(align.requests, "post", _fail_post)
    second = align.fetch_wikidata_values("rdfs:label", "Q5", None)
    assert second
    assert second == first
