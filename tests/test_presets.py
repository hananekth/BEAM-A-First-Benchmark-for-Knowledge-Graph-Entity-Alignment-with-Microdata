import importlib
import re


QID_RE = re.compile(r"^Q\d+$")
WD_PROP_RE = re.compile(r"^(wdt:)?P\d+$")


def test_webapp_presets_use_valid_wikidata_ids():
    import webapp.main as web_main

    importlib.reload(web_main)
    presets = web_main.PRESETS

    assert "testclass_quick" in presets
    assert "testclass_large_benchmark" in presets
    assert "testclass_label" in presets
    assert "testclass_identifier" in presets
    assert "testclass_wikidata_url" in presets
    assert "testclass_wikidata_sameas" in presets
    assert "property_movie" in presets
    assert "label_language" in presets
    assert "property_country_iso2" in presets
    assert "wikidata_link_city" in presets

    for key, preset in presets.items():
        assert preset["class_name"], f"{key} class_name should not be empty"
        if preset["wkd_class"]:
            assert QID_RE.match(preset["wkd_class"]), f"{key} has invalid wkd_class"
        prop = preset["wikidata_property"]
        if prop and prop != "rdfs:label":
            assert WD_PROP_RE.match(prop), f"{key} has invalid wikidata_property"

    testclass = presets["testclass_quick"]
    assert testclass["wikidata_property"] == "rdfs:label"
    assert testclass["wkd_class"] == "Q34770"
    assert testclass["wdc_value_is_wikidata"] is False

    testclass_large = presets["testclass_large_benchmark"]
    assert testclass_large["wikidata_property"] == "rdfs:label"
    assert testclass_large["wkd_class"] == "Q34770"
    assert testclass_large["wdc_value_is_wikidata"] is False
    assert testclass_large["force_align"] is True

    testclass_label = presets["testclass_label"]
    assert testclass_label["wikidata_property"] == "rdfs:label"
    assert testclass_label["wkd_class"] == "Q34770"
    assert testclass_label["wdc_value_is_wikidata"] is False

    testclass_identifier = presets["testclass_identifier"]
    assert testclass_identifier["wikidata_property"] == "wdt:P2704"
    assert testclass_identifier["wkd_class"] == "Q11424"
    assert testclass_identifier["wdc_value_is_wikidata"] is False

    testclass_wikidata_url = presets["testclass_wikidata_url"]
    assert testclass_wikidata_url["wikidata_property"] == "wdt:P31"
    assert testclass_wikidata_url["wkd_class"] == "Q515"
    assert testclass_wikidata_url["wdc_value_is_wikidata"] is True

    testclass_wikidata_sameas = presets["testclass_wikidata_sameas"]
    assert testclass_wikidata_sameas["wikidata_property"] == "wdt:P31"
    assert testclass_wikidata_sameas["wkd_class"] == "Q6256"
    assert testclass_wikidata_sameas["wdc_value_is_wikidata"] is True
