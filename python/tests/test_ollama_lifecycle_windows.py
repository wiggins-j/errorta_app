"""Windows lifecycle: _platform_start must resolve ollama.exe from
%LOCALAPPDATA%\\Programs\\Ollama when it isn't on PATH (spec WS6)."""

import errorta_ollama.lifecycle as lc


class _FakePopen:
    last_args = None

    def __init__(self, args, **kwargs):
        _FakePopen.last_args = args


def _win(monkeypatch):
    monkeypatch.setattr(lc.sys, "platform", "win32")
    monkeypatch.setattr(lc.subprocess, "Popen", _FakePopen)
    _FakePopen.last_args = None


def test_uses_ollama_on_path_when_available(monkeypatch):
    _win(monkeypatch)
    monkeypatch.setattr(lc.shutil, "which", lambda name, **k: r"C:\somewhere\ollama.exe"
                        if name in ("ollama.exe", "ollama") else None)
    assert lc._platform_start() is True
    # cmd /c start "" <exe> serve
    assert _FakePopen.last_args[:4] == ["cmd", "/c", "start", ""]
    assert _FakePopen.last_args[4] == r"C:\somewhere\ollama.exe"
    assert _FakePopen.last_args[5] == "serve"


def test_falls_back_to_localappdata_when_not_on_path(monkeypatch, tmp_path):
    _win(monkeypatch)
    monkeypatch.setattr(lc.shutil, "which", lambda name, **k: None)
    local = tmp_path / "AppData" / "Local"
    exe = local / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert lc._platform_start() is True
    assert _FakePopen.last_args[4] == str(exe)
