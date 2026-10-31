import json
import os
import sys
from pathlib import Path

import pytest

from core import runtime_paths as rp
from prerequisites import load_config, save_user_config
from prerequisites.config import DEFAULTS


def w(p: Path, data):
    p.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return p


def test_defaults_are_safe(tmp_path):
    cfg = load_config(tmp_path / "u", tmp_path / "s", tmp_path / "m")
    assert cfg.mode == "ask" and cfg.require_verification is True and cfg.prefer_user_scope is True
    assert cfg.allow_downloads and cfg.allow_installs and cfg.proxy == "" and not cfg.locked and not cfg.warnings


def test_precedence_and_locking(tmp_path):
    user = w(tmp_path / "u.json", {"proxy": "http://user-proxy:1", "mode": "auto", "snooze_days": 3})
    shipped = w(tmp_path / "s.json", {"proxy": "http://shipped-proxy:2", "allow_downloads": False})
    machine = w(tmp_path / "m.json", {"proxy": "http://machine-proxy:3"})
    cfg = load_config(user, shipped, machine)
    assert cfg.proxy == "http://machine-proxy:3"                  # machine > shipped > user
    assert cfg.allow_downloads is False and cfg.mode == "auto" and cfg.snooze_days == 3
    assert {"proxy", "allow_downloads"} <= cfg.locked and "mode" not in cfg.locked


def test_invalid_values_and_broken_files_never_raise(tmp_path):
    user = w(tmp_path / "u.json", {"mode": "banana", "snooze_days": "x", "blocked": [1], "allowed_hosts": ["ok.example"], "proxy": 5,
                                   "unknown": 1, "max_download_mb": 10**9})
    shipped = w(tmp_path / "s.json", "{not json")
    cfg = load_config(user, shipped, tmp_path / "missing")
    assert cfg.mode == "ask" and cfg.blocked == [] and cfg.allowed_hosts == ["ok.example"] and cfg.max_download_mb == 20480
    assert len(cfg.warnings) >= 4 and "unknown" not in cfg.values


def test_save_user_config_refuses_locked_keys(tmp_path):
    user, shipped = tmp_path / "u.json", w(tmp_path / "s.json", {"offline_dir": "D:/approved"})
    cfg = load_config(user, shipped, tmp_path / "m")
    warns = save_user_config({"offline_dir": "C:/hack", "proxy": "http://p:1", "mode": "nope"}, user, cfg)
    saved = json.loads(user.read_text(encoding="utf-8"))
    assert saved == {"proxy": "http://p:1"} and any("policy" in x for x in warns)


def test_runtime_paths_from_source_is_project_folder():
    assert rp.resource_root() == rp.data_root() == Path(rp.__file__).resolve().parent.parent
    assert not rp.is_frozen()


def test_frozen_exe_keeps_old_behaviour_when_writable_and_moves_when_not(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Program Files" / "App" / "app.exe"))
    monkeypatch.delenv("AISEA_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "local"))
    rp._WRITABLE_CACHE.clear()
    assert rp.resource_root() == bundle
    assert rp.data_root() == bundle                                   # writable install folder: exactly the previous behaviour
    rp._WRITABLE_CACHE.clear()
    monkeypatch.setattr(rp, "_writable", lambda folder: False)        # e.g. installed under Program Files, no write permission
    assert rp.data_root() == tmp_path / "local" / "AISoftwareEngineeringAgent"
    exe_folder = tmp_path / "Program Files" / "App"
    exe_folder.mkdir(parents=True)
    (exe_folder / "portable.flag").write_text("")
    assert rp.data_root() == exe_folder.resolve()
    monkeypatch.setenv("AISEA_HOME", str(tmp_path / "custom"))
    assert rp.data_root() == tmp_path / "custom"


def test_user_data_dir_windows_and_mac_branches(tmp_path, monkeypatch):
    import types
    monkeypatch.setattr(rp, "os", types.SimpleNamespace(name="nt", environ={"LOCALAPPDATA": str(tmp_path / "L")}))
    assert rp._user_data_dir() == tmp_path / "L" / "AISoftwareEngineeringAgent"


def test_writable_probe_is_real(tmp_path):
    rp._WRITABLE_CACHE.clear()
    assert rp._writable(tmp_path / "new" / "sub") is True and not list((tmp_path / "new" / "sub").glob(".write_test_*"))
    blocked = tmp_path / "file"
    blocked.write_text("x")
    assert rp._writable(blocked / "child") is False


def test_seed_config_copies_only_missing_files(tmp_path):
    src, dst = tmp_path / "res", tmp_path / "data"
    (src / "config").mkdir(parents=True)
    (src / "config" / "a.json").write_text("new")
    (src / "config" / "b.json").write_text("new")
    (dst / "config").mkdir(parents=True)
    (dst / "config" / "a.json").write_text("USER EDIT")
    assert rp.seed_config(src, dst) == ["b.json"]
    assert (dst / "config" / "a.json").read_text() == "USER EDIT"
    assert rp.seed_config(src, src) == []


def test_machine_policy_location():
    assert rp.machine_policy_path().name == "policy.json"
