from bs4 import BeautifulSoup

from beam import wdc_classes


def test_is_class_anchor_filters_expected_links():
    soup = BeautifulSoup(
        """
        <a href="https://schema.org/TestClass">TestClass</a>
        <a href="https://schema.org/Test/Class">Test/Class</a>
        <a href="https://example.org/TestClass">TestClass</a>
        <a href="https://schema.org/TestClass">https://schema.org/TestClass</a>
        """,
        "html.parser",
    )
    anchors = soup.find_all("a")

    assert wdc_classes._is_class_anchor(anchors[0]) is True
    assert wdc_classes._is_class_anchor(anchors[1]) is False
    assert wdc_classes._is_class_anchor(anchors[2]) is False
    assert wdc_classes._is_class_anchor(anchors[3]) is False


def test_fetch_wdc_classes_parses_testclass_rows(monkeypatch):
    html = """
    <html>
      <body>
        <a href="https://schema.org/TestClass">TestClass</a>
        (2) total size 1.5 GB
        <a href="https://data.dws.informatik.uni-mannheim.de/structureddata/schema.org/TestClass.gz">TestClass</a>

        <a href="https://schema.org/NoDownloadClass">NoDownloadClass</a>
        text with no parts and no size

        <a href="https://schema.org/TestClass">TestClass</a>
        (9) total size 9.9 GB
        <a href="https://data.dws.informatik.uni-mannheim.de/structureddata/schema.org/TestClass_v2.gz">TestClass</a>

        <a href="https://schema.org/TestClassTwo">TestClassTwo</a>
        (1) total size 512 MB
        <a href="https://data.dws.informatik.uni-mannheim.de/structureddata/schema.org/TestClassTwo.gz">TestClassTwo</a>
      </body>
    </html>
    """

    class _Response:
        text = html

        @staticmethod
        def raise_for_status():
            return None

    monkeypatch.setattr(wdc_classes.requests, "get", lambda *args, **kwargs: _Response())

    rows = wdc_classes.fetch_wdc_classes()
    by_name = {row["class_name"]: row for row in rows}

    assert "TestClass" in by_name
    assert by_name["TestClass"]["num_parts"] == 2
    assert by_name["TestClass"]["size_human"] == "1.5 GB"

    assert "TestClassTwo" in by_name
    assert by_name["TestClassTwo"]["num_parts"] == 1
    assert by_name["TestClassTwo"]["size_human"] == "512 MB"

    assert "NoDownloadClass" not in by_name
    assert len([row for row in rows if row["class_name"] == "TestClass"]) == 1


def test_load_and_save_wdc_classes_catalog_roundtrip(tmp_path, monkeypatch):
    catalog_path = tmp_path / "wdc_catalog.json"
    monkeypatch.setenv("WDC_CLASSES_CATALOG_PATH", str(catalog_path))

    wdc_classes.save_wdc_classes_catalog(
        [
            {"class_name": "City", "num_parts": "12", "size_human": "1.0 GB"},
            {"class_name": "City", "num_parts": 99, "size_human": "ignored duplicate"},
            {"class_name": "Language", "num_parts": None, "size_human": ""},
            {"class_name": "", "num_parts": 1, "size_human": "bad row"},
        ]
    )
    loaded = wdc_classes.load_wdc_classes_catalog()
    assert loaded == [
        {"class_name": "City", "num_parts": 12, "size_human": "1.0 GB"},
        {"class_name": "Language", "num_parts": None, "size_human": None},
    ]


def test_load_wdc_classes_catalog_accepts_object_payload(tmp_path, monkeypatch):
    catalog_path = tmp_path / "wdc_catalog.json"
    monkeypatch.setenv("WDC_CLASSES_CATALOG_PATH", str(catalog_path))
    catalog_path.write_text(
        """
        {
          "classes": [
            {"class_name": "Movie", "num_parts": 3, "size_human": "2.0 MB"}
          ]
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )
    loaded = wdc_classes.load_wdc_classes_catalog()
    assert loaded == [{"class_name": "Movie", "num_parts": 3, "size_human": "2.0 MB"}]
