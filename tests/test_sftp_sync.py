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


# ── copy mode (watch vs one-time) ────────────────────────────────────────────
def test_mode_defaults_to_watch():
    assert ss.SftpSync({}).once is False
    assert ss.SftpSync({"mode": "watch"}).once is False


def test_mode_once_variants_recognised():
    for m in ("once", "one-time", "onetime", "bulk", "ONCE", " Once "):
        assert ss.SftpSync({"mode": m}).once is True, m


def test_mode_dispatches_to_correct_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(ss.SftpSync, "_run_once", lambda self: calls.append("once"))
    monkeypatch.setattr(ss.SftpSync, "_run_watch", lambda self: calls.append("watch"))
    ss.SftpSync({"mode": "once"}).run()
    ss.SftpSync({"mode": "watch"}).run()
    assert calls == ["once", "watch"]


# ── throughput settings ──────────────────────────────────────────────────────
def test_workers_default_and_clamped():
    assert ss.SftpSync({}).workers == ss.DEFAULT_WORKERS
    assert ss.SftpSync({"workers": 8}).workers == 8
    assert ss.SftpSync({"workers": 99}).workers == 16      # ceiling
    assert ss.SftpSync({"workers": -5}).workers == 1        # floor
    # 0 / blank are treated as "unset" → default
    assert ss.SftpSync({"workers": 0}).workers == ss.DEFAULT_WORKERS
    assert ss.SftpSync({"workers": ""}).workers == ss.DEFAULT_WORKERS


def test_needs_copy_uses_listing_size_not_remote_stat(tmp_path):
    """Skip decision must be local-only — no per-file remote round-trip."""
    s = ss.SftpSync({"remote_dir": "/r", "local_dir": str(tmp_path)})
    assert s._needs_copy("/r/a.dat", 4) is True            # missing locally
    (tmp_path / "a.dat").write_bytes(b"1234")
    assert s._needs_copy("/r/a.dat", 4) is False           # present, same size
    assert s._needs_copy("/r/a.dat", 9) is True            # present, size differs


# ── progress reporting ───────────────────────────────────────────────────────
def test_progress_idle_before_start():
    p = ss.SftpSync({}).progress()
    assert p["phase"] == "idle" and p["percent"] == 0.0
    assert p["files_total"] == 0 and p["current"] == []


def test_progress_percent_is_byte_based():
    s = ss.SftpSync({})
    s._prog_reset("copying", files_total=4, bytes_total=1000)
    s._prog["bytes_done"] = 250
    p = s.progress()
    assert p["percent"] == 25.0 and p["phase"] == "copying"
    assert p["data_total"] == "1.0 KB" and p["data_done"] == "250 B"
    assert p["files_done"] == 0


def test_progress_reports_gb_for_large_transfers():
    s = ss.SftpSync({})
    s._prog_reset("copying", files_total=100, bytes_total=1_500_000_000)
    s._prog["bytes_done"] = 750_000_000
    p = s.progress()
    assert p["data_total"] == "1.50 GB" and p["data_done"] == "750.0 MB"
    assert p["gb_total"] == 1.5 and p["gb_done"] == 0.75
    assert p["percent"] == 50.0


def test_human_bytes_scales():
    assert ss.human_bytes(0) == "0 B"
    assert ss.human_bytes(4096) == "4.1 KB"
    assert ss.human_bytes(318_700_000) == "318.7 MB"
    assert ss.human_bytes(2_400_000_000) == "2.40 GB"
    assert ss.human_bytes(3.5e12) == "3.50 TB"


def test_progress_falls_back_to_file_count_when_sizes_unknown():
    s = ss.SftpSync({})
    s._prog_reset("copying", files_total=4, bytes_total=0)
    s._prog["files_done"] = 3
    assert s.progress()["percent"] == 75.0


def test_progress_callback_is_delta_based():
    """Two concurrent files must not double-count bytes."""
    s = ss.SftpSync({})
    s._prog_reset("copying", files_total=2, bytes_total=200)
    cb_a, cb_b = s._make_cb("a.dat"), s._make_cb("b.dat")
    cb_a(50, 100); cb_a(100, 100)          # a: cumulative reports
    cb_b(30, 100); cb_b(100, 100)          # b: cumulative reports
    p = s.progress()
    assert p["bytes_done"] == 200 and p["percent"] == 100.0
    assert {c["name"] for c in p["current"]} == {"a.dat", "b.dat"}


def test_progress_eta_none_when_complete():
    s = ss.SftpSync({})
    s._prog_reset("copying", files_total=1, bytes_total=100)
    s._prog["bytes_done"] = 100
    assert s.progress()["eta_s"] is None


def test_human_rate():
    assert ss.human_rate(10e6, 1.0) == "10.0 MB/s"
    assert ss.human_rate(2e9, 1.0) == "2.00 GB/s"
    assert ss.human_rate(50e3, 1.0) == "50 KB/s"
    assert ss.human_rate(100, 0) == "—"


def test_save_config_persists_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "CONFIG_FILE", tmp_path / "c.json")
    ss.save_config({"host": "h", "mode": "once", "password": "p"})
    assert ss.load_config()["mode"] == "once"
