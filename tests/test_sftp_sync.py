"""Tests for src.preprocess.sftp_sync (pure/config parts; paramiko not required)."""
from __future__ import annotations

import json
from pathlib import Path

from src.preprocess import sftp_sync as ss


def test_relative_local_path_preserves_tree():
    p = ss.relative_local_path("/data/bl1-5/run1/scan_001.dat", "/data/bl1-5", "/local/out")
    assert p == Path("/local/out/run1/scan_001.dat")


def test_relative_local_path_trailing_slash_base():
    p = ss.relative_local_path("/data/bl1-5/a/b.raw", "/data/bl1-5/", "/local")
    assert p == Path("/local/a/b.raw")


def test_relative_local_path_outside_base_uses_name():
    p = ss.relative_local_path("/somewhere/else/x.tif", "/data/bl1-5", "/local")
    assert p == Path("/local/x.tif")


def test_save_config_excludes_password(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".swaxs_sftp_sync.json"
    monkeypatch.setattr(ss, "CONFIG_FILE", cfg_file)
    ss.save_config({"host": "h", "username": "u", "password": "secret", "remote_dir": "/r"})
    saved = json.loads(cfg_file.read_text())
    assert "password" not in saved
    assert saved["host"] == "h" and saved["remote_dir"] == "/r"


def test_load_config_roundtrip(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".swaxs_sftp_sync.json"
    monkeypatch.setattr(ss, "CONFIG_FILE", cfg_file)
    assert ss.load_config() == {}                      # missing file → {}
    ss.save_config({"host": "h", "password": "p"})
    assert ss.load_config() == {"host": "h"}           # password not persisted


def test_test_connection_requires_host_user():
    ok, msg = ss.test_connection({"username": "u"})
    assert ok is False and "Host" in msg
