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
    assert not align._is_retryable_query_error(Exception("invalid query syntax"))
