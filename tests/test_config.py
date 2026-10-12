import json

from simq.config import Config


def test_defaults_when_missing():
    cfg = Config.load()
    assert cfg.search_dirs == [] and cfg.programs == []


def test_round_trip():
    Config(search_dirs=["/data/apps"], programs=["/opt/x/namd3"]).save()
    cfg = Config.load()
    assert cfg.search_dirs == ["/data/apps"]
    assert cfg.programs == ["/opt/x/namd3"]


def test_tolerates_bad_file():
    Config.path().write_text("{not json")
    assert Config.load().programs == []
    Config.path().write_text(json.dumps({"programs": ["ok", 3, None], "search_dirs": "x"}))
    cfg = Config.load()
    assert cfg.programs == ["ok"] and cfg.search_dirs == []
