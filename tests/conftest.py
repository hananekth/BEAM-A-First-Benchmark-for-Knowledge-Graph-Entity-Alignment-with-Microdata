from pathlib import Path

import pytest

from beam import db as beam_db


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in ("Download", "data", "jobs", "logs"):
        (runtime / name).mkdir(parents=True, exist_ok=True)

    monkeypatch.chdir(runtime)
    monkeypatch.setattr(beam_db, "DB_PATH", Path(runtime / "jobs.db"))
    beam_db.init_db()
    return runtime


@pytest.fixture
def test_wdc_classes():
    return [
        {"class_name": "TestClass", "num_parts": 2, "size_human": "2.0 MB"},
        {"class_name": "TestClassTwo", "num_parts": 1, "size_human": "512 KB"},
    ]
