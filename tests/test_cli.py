import json

from simq.__main__ import main


def test_info(capsys):
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert "CPU" in out and "Memory" in out and "Disks" in out


def test_detect_json(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["detect", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data["installations"], list)
    assert data["folders_searched"] > 0


def test_gui_without_display(capsys, monkeypatch):
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(var, raising=False)
    assert main([]) == 1
    assert "no graphical display" in capsys.readouterr().err


def test_add_prints_readable_resources(capsys, tmp_path):
    folder = tmp_path / "job"
    folder.mkdir()
    (folder / "run.sh").write_text("true\n")
    assert main(["add", str(folder), "--cores", "1"]) == 0
    out = capsys.readouterr().out
    assert "(command, 1 CPU, no GPU)" in out
