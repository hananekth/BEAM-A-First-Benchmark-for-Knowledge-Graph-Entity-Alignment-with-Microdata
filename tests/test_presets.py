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
    assert "code_movie" in presets
    assert "label_language" in presets
    assert "property_college_or_university_telephone" in presets
    assert "wikidata_link_city" in presets

    for key, preset in presets.items():
        assert preset["class_name"], f"{key} class_name should not be empty"
        assert preset.get("parts_spec") == "all", f"{key} should default to parts_spec=all"
        assert bool(preset.get("force_align", False)) is False, f"{key} should not force align cache bypass"
        assert bool(preset.get("use_local_only", False)) is False, f"{key} should not force local-only mode"
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
    assert testclass_large["force_align"] is False

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

    label_language = presets["label_language"]
    assert label_language["wikidata_property"] == "rdfs:label"
    assert label_language["wkd_class"] == "Q33742"

    college_phone = presets["property_college_or_university_telephone"]
    assert college_phone["class_name"] == "CollegeOrUniversity"
    assert college_phone["parts_spec"] == "all"
    assert college_phone["wdc_predicate_pattern"] == "telephone"
    assert college_phone["wikidata_property"] == "P1329"
    assert college_phone["wkd_class"] == "Q38723"
    assert college_phone["wdc_value_is_wikidata"] is False

    city_link = presets["wikidata_link_city"]
    assert city_link["class_name"] == "City"
    assert city_link["parts_spec"] == "all"
    assert city_link["wdc_predicate_pattern"] == "sameAs"
    assert city_link["wikidata_property"] == ""
    assert city_link["wkd_class"] == "Q486972"
    assert city_link["wdc_value_is_wikidata"] is True
    assert city_link["force_align"] is False
    assert city_link["use_local_only"] is False
