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


def test_parse_strip_list_supports_named_tokens():
    chars = align.parse_strip_list("spaces;dot;semicolon;hyphen;comma;slash;underscore")
    assert " " in chars
    assert "\t" in chars
    assert "." in chars
    assert ";" in chars
    assert "-" in chars
    assert "," in chars
    assert "/" in chars
    assert "_" in chars


def test_normalize_for_matching_removes_only_configured_tokens():
    align.set_extra_strip_chars(align.parse_strip_list("spaces;-;."))
    try:
        assert align.normalize_for_matching("+33 (0)4 78-03.47;00") == "+33(0)4780347;00"
    finally:
        align.set_extra_strip_chars([])


def test_normalize_for_matching_special_chars_keeps_only_alnum():
    align.set_extra_strip_chars(align.parse_strip_list("special-chars"))
    try:
        assert align.normalize_for_matching("+33 (0)4 78-03.47;AB") == "3304780347ab"
    finally:
        align.set_extra_strip_chars([])


def test_normalize_for_phone_matching_keeps_plus_and_digits_only():
    align.set_extra_strip_chars(align.parse_strip_list("spaces;-;."))
    try:
        assert align.normalize_for_phone_matching("+33 (0)4 78-03.47;00") == "+330478034700"
    finally:
        align.set_extra_strip_chars([])


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


def test_extract_unique_iris_supports_multiple_predicate_patterns(tmp_path):
    part = tmp_path / "part_0.nq"
    part.write_text(
        "<http://example.org/s1> <http://schema.org/sameAs> \"A\" <http://example.org/g> .\n"
        "<http://example.org/s2> <http://schema.org/url> \"B\" <http://example.org/g> .\n"
        "<http://example.org/s3> <http://schema.org/name> \"C\" <http://example.org/g> .\n",
        encoding="utf-8",
    )

    value_map, matched_count = align.extract_unique_iris_from_files(
        [part],
        pattern="sameAs, url",
        collect_top_props=False,
        parallel=False,
        progress_every=0,
        wdc_value_is_wd_iri=False,
    )

    assert matched_count == 2
    all_values = {v for entries in value_map.values() for (v, _s) in entries}
    assert all_values == {"A", "B"}


def test_extract_unique_iris_supports_value_pattern_search(tmp_path):
    part = tmp_path / "part_0.nq"
    part.write_text(
        "<http://example.org/s1> <http://schema.org/sameAs> <https://ror.org/04pf8en64> <http://example.org/g> .\n"
        '<http://example.org/s2> <http://schema.org/name> "Plain name" <http://example.org/g> .\n',
        encoding="utf-8",
    )

    value_map, matched_count = align.extract_unique_iris_from_files(
        [part],
        pattern="ror.org",
        collect_top_props=False,
        parallel=False,
        progress_every=0,
        wdc_value_is_wd_iri=False,
        search_in="value",
    )

    assert matched_count == 1
    all_values = {v for entries in value_map.values() for (v, _s) in entries}
    assert all_values == {"https://ror.org/04pf8en64"}


def test_predicate_matching_is_case_insensitive_even_when_value_normalization_disabled():
    align.set_normalization(False)
    try:
        prepared = align.prepare_predicate_patterns("sameas")
        assert align.predicate_matches_prepared_patterns("http://schema.org/sameAs", prepared) is True
        assert align.predicate_matches_prepared_patterns("https://schema.org/sameAs", prepared) is True
    finally:
        align.set_normalization(True)


def test_extract_unique_iris_filters_subjects_by_wdc_type(tmp_path):
    part = tmp_path / "part_0.nq"
    part.write_text(
        "<http://example.org/a1> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://schema.org/Airport> <http://example.org/g> .\n"
        "<http://example.org/c1> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://schema.org/City> <http://example.org/g> .\n"
        "<http://example.org/a1> <http://schema.org/iataCode> \"ORY\" <http://example.org/g> .\n"
        "<http://example.org/c1> <http://schema.org/iataCode> \"XXX\" <http://example.org/g> .\n",
        encoding="utf-8",
    )

    value_map, matched_count = align.extract_unique_iris_from_files(
        [part],
        pattern="iatacode",
        collect_top_props=False,
        parallel=False,
        progress_every=0,
        wdc_value_is_wd_iri=False,
        type_filter_iris=["<http://schema.org/Airport>", "<https://schema.org/Airport>"],
    )

    assert matched_count == 1
    all_values = {v for entries in value_map.values() for (v, _s) in entries}
    assert all_values == {"ORY"}


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


def test_fetch_wikidata_values_batches_large_entity_values(monkeypatch):
    monkeypatch.setenv("WIKIDATA_CACHE_DISABLED", "1")
    monkeypatch.setenv("WIKIDATA_ENTITY_BATCH_SIZE", "2")

    class _Resp:
        def __init__(self, text):
            self.text = text

        @staticmethod
        def raise_for_status():
            return None

    calls = {"n": 0}

    def _fake_post(*args, **kwargs):
        calls["n"] += 1
        query = str((kwargs or {}).get("data", {}).get("query", ""))
        # Return one row per batch to verify batching path is used.
        if "Q1" in query:
            payload = (
                '{"results":{"bindings":[{"entity":{"value":"http://www.wikidata.org/entity/Q1"},'
                '"value":{"value":"http://www.wikidata.org/entity/Q1"}}]}}'
            )
        elif "Q3" in query:
            payload = (
                '{"results":{"bindings":[{"entity":{"value":"http://www.wikidata.org/entity/Q3"},'
                '"value":{"value":"http://www.wikidata.org/entity/Q3"}}]}}'
            )
        else:
            payload = '{"results":{"bindings":[]}}'
        return _Resp(payload)

    monkeypatch.setattr(align.requests, "post", _fake_post)
    result = align.fetch_wikidata_values(
        None,
        wkd_class="Q5",
        wkd_prop_class=None,
        entity_iris=[
            "http://www.wikidata.org/entity/Q1",
            "http://www.wikidata.org/entity/Q2",
            "http://www.wikidata.org/entity/Q3",
        ],
    )

    assert calls["n"] == 2  # 3 entities with batch size 2 => 2 SPARQL requests
    q1_norm = align.normalize_for_matching("http://www.wikidata.org/entity/Q1")
    q3_norm = align.normalize_for_matching("http://www.wikidata.org/entity/Q3")
    assert q1_norm in result
    assert q3_norm in result


def test_fetch_wikidata_values_batches_large_value_candidates(monkeypatch):
    monkeypatch.setenv("WIKIDATA_CACHE_DISABLED", "1")
    monkeypatch.setenv("WIKIDATA_VALUE_BATCH_SIZE", "2")

    class _Resp:
        def __init__(self, text):
            self.text = text

        @staticmethod
        def raise_for_status():
            return None

    calls = {"n": 0, "queries": []}

    def _fake_post(*args, **kwargs):
        calls["n"] += 1
        query = str((kwargs or {}).get("data", {}).get("query", ""))
        calls["queries"].append(query)
        if '"ABC"' in query:
            payload = (
                '{"results":{"bindings":[{"entity":{"value":"http://www.wikidata.org/entity/Q1"},'
                '"value":{"value":"ABC"}}]}}'
            )
        elif '"GHI"' in query:
            payload = (
                '{"results":{"bindings":[{"entity":{"value":"http://www.wikidata.org/entity/Q3"},'
                '"value":{"value":"GHI"}}]}}'
            )
        else:
            payload = '{"results":{"bindings":[]}}'
        return _Resp(payload)

    monkeypatch.setattr(align.requests, "post", _fake_post)
    result = align.fetch_wikidata_values(
        "P297",
        wkd_class="Q1248784",
        wkd_prop_class=None,
        value_candidates=["ABC", "DEF", "GHI"],
    )

    assert calls["n"] == 2  # 3 values with batch size 2 => 2 SPARQL requests
    assert any("VALUES ?value" in q for q in calls["queries"])
    assert any("wdt:P297" in q for q in calls["queries"])
    assert any("wdt:P279* wd:Q1248784" in q for q in calls["queries"])
    assert align.normalize_for_matching("ABC") in result
    assert align.normalize_for_matching("GHI") in result


def test_fetch_wikidata_values_without_prop_requires_entity_list(monkeypatch):
    monkeypatch.setenv("WIKIDATA_CACHE_DISABLED", "1")
    calls = {"n": 0}

    def _fake_post(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("requests.post should not be called when entity_iris is empty")

    monkeypatch.setattr(align.requests, "post", _fake_post)
    result = align.fetch_wikidata_values(
        None,
        wkd_class="Q5",
        wkd_prop_class=None,
        entity_iris=[],
    )

    assert result == {}
    assert calls["n"] == 0


def test_normalize_target_property_maps_common_aliases_for_non_wikidata():
    assert align.normalize_target_property("P238", "dbpedia") == "dbp:iata"
    assert align.normalize_target_property("iata", "dbpedia") == "dbp:iata"
    assert align.normalize_target_property("P212", "dbpedia") == "dbo:isbn"
    assert align.normalize_target_property("telephone", "dbpedia") == "dbp:telephone"

    assert align.normalize_target_property("P238", "yago") == "schema:iataCode"
    assert align.normalize_target_property("isbn", "yago") == "schema:isbn"
    assert align.normalize_target_property("P1329", "yago") == "schema:telephone"


def test_fetch_target_values_supports_value_candidates_for_sameas(monkeypatch):
    captured = {}

    def _fake_runner(endpoint_url, query, headers, timeout_s, max_attempts, base_delay):
        captured["endpoint_url"] = endpoint_url
        captured["query"] = query
        return {
            "results": {
                "bindings": [
                    {
                        "entity": {"value": "http://dbpedia.org/resource/Torture_Museum,_Amsterdam"},
                        "value": {"value": "http://www.wikidata.org/entity/Q17146713"},
                    }
                ]
            }
        }

    monkeypatch.setattr(align, "_run_sparql_query_with_retry_to_endpoint", _fake_runner)

    out = align.fetch_target_values(
        target_property="owl:sameAs",
        target_class="dbo:Museum",
        target_endpoint="dbpedia",
        value_candidates=["http://www.wikidata.org/entity/Q17146713"],
    )

    assert "VALUES ?value" in captured["query"]
    assert "owl:sameAs" in captured["query"]
    assert "dbo:Museum" in captured["query"]
    norm = align.normalize_country_code(
        align.normalize_value_for_matching("http://www.wikidata.org/entity/Q17146713")
    )
    assert norm in out
    assert out[norm][0][1] == "http://dbpedia.org/resource/Torture_Museum,_Amsterdam"


def test_fetch_target_values_quotes_literal_value_candidates(monkeypatch):
    captured = {}

    def _fake_runner(endpoint_url, query, headers, timeout_s, max_attempts, base_delay):
        captured["query"] = query
        return {"results": {"bindings": []}}

    monkeypatch.setattr(align, "_run_sparql_query_with_retry_to_endpoint", _fake_runner)

    out = align.fetch_target_values(
        target_property="dbp:telephone",
        target_class="dbo:Hotel",
        target_endpoint="dbpedia",
        value_candidates=["+33 1 40 20 50 50"],
    )

    assert out == {}
    assert "VALUES ?value" in captured["query"]
    assert '"+33 1 40 20 50 50"' in captured["query"]
    assert "<+33 1 40 20 50 50>" not in captured["query"]
