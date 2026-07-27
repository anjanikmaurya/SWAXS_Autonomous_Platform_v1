"""
src/preprocess/sftp_sync.py — poll a remote SFTP folder and copy new/updated files
into a local folder (e.g. a Google-Drive-synced directory), preserving sub-dirs.

Headless/backend version of the group's SFTP→GDrive tool, driven by the
calibration app. paramiko is imported lazily so the module loads without it.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

CONFIG_FILE = Path.home() / ".swaxs_sftp_sync.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def save_config(data: dict) -> None:
    # never persist the password to disk
    CONFIG_FILE.write_text(json.dumps({k: v for k, v in data.items() if k != "password"}, indent=2))


def relative_local_path(remote_path: str, remote_base: str, local_base) -> Path:
    """Map a remote file path to its local destination, preserving the tree under
    ``remote_base``. Pure/testable."""
    rb = remote_base.rstrip("/")
    rel = remote_path[len(rb):] if remote_path.startswith(rb) else Path(remote_path).name
    return Path(local_base) / str(rel).lstrip("/")


def _connect(cfg: dict):
    import paramiko                                        # noqa: PLC0415
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(hostname=cfg["host"], port=int(cfg.get("port", 22) or 22),
              username=cfg["username"], timeout=30, look_for_keys=False)
    key = str(cfg.get("key_path", "") or "").strip()
    if key and Path(key).exists():
        kw["key_filename"] = key
    else:
        kw["password"] = cfg.get("password", "")
    ssh.connect(**kw)
    return ssh.open_sftp(), ssh


def test_connection(cfg: dict):
    """(ok, message) — connect and list the remote folder."""
    if not cfg.get("host") or not cfg.get("username"):
        return False, "Host and Username are required."
    try:
        sftp, ssh = _connect(cfg)
        try:
            n = len(sftp.listdir(cfg.get("remote_dir", ".")))
        finally:
            sftp.close(); ssh.close()
        return True, f"Connected — {n} item(s) in the remote folder."
    except Exception as exc:
        return False, str(exc)


class SftpSync(threading.Thread):
    """Polls the remote folder every ``interval`` s and downloads new/updated files."""

    #: cfg["mode"] values that mean "copy everything once, then stop"
    ONCE_MODES = ("once", "one-time", "onetime", "bulk")

    def __init__(self, cfg: dict, log_cb=None, status_cb=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self._log = log_cb or (lambda level, msg: None)
        self._status = status_cb or (lambda text, color: None)
        self._stop = threading.Event()
        self.seen: dict[str, float] = {}   # remote_path → mtime
        #: True → single full pass (post-beamtime); False → keep polling (live)
        self.once = str(cfg.get("mode", "watch")).strip().lower() in self.ONCE_MODES

    def stop(self):
        self._stop.set()

    def log(self, msg, level="INFO"):
        self._log(level, f"[{datetime.now():%H:%M:%S}] {msg}")

    def _list_remote(self, sftp, remote_dir):
        out = []
        try:
            for a in sftp.listdir_attr(remote_dir):
                rp = f"{remote_dir.rstrip('/')}/{a.filename}"
                if a.st_mode and (a.st_mode & 0o40000):    # directory
                    out.extend(self._list_remote(sftp, rp))
                else:
                    out.append((rp, a.st_mtime or 0.0))
        except Exception as exc:
            self.log(f"cannot list {remote_dir}: {exc}", "WARN")
        return out

    def _download(self, sftp, remote_path) -> bool:
        local_path = relative_local_path(remote_path, self.cfg["remote_dir"], self.cfg["local_dir"])
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:                                               # skip if same size already
            if local_path.exists() and local_path.stat().st_size == sftp.stat(remote_path).st_size:
                return False
        except Exception:
            pass
        tmp = local_path.with_suffix(local_path.suffix + ".part")
        sftp.get(remote_path, str(tmp))
        tmp.replace(local_path)                            # atomic; overwrites on Windows too
        return True

    def _cycle(self, sftp, verbose: bool):
        """One full pass over the remote tree. Returns (total, copied, skipped, failed)."""
        files = self._list_remote(sftp, self.cfg["remote_dir"])
        total = len(files)
        if verbose:
            self.log(f"found {total} remote file(s) — copying")
        copied = skipped = failed = 0
        for i, (rp, mtime) in enumerate(files, 1):
            if self._stop.is_set():
                break
            if self.seen.get(rp) is not None and mtime <= self.seen[rp]:
                skipped += 1
                continue
            fn = Path(rp).name
            try:
                if self._download(sftp, rp):
                    copied += 1
                    self.log(f"[{i}/{total}] saved {fn}", "OK")
                else:
                    skipped += 1
                self.seen[rp] = mtime
            except Exception as exc:
                failed += 1
                self.log(f"[{i}/{total}] failed {fn}: {exc}", "ERR")
        return total, copied, skipped, failed

    def _run_once(self):
        """Post-beamtime bulk copy: one full pass, then stop."""
        self.log("one-time copy — scanning the whole remote folder")
        self._status("Connecting…", "warn")
        sftp = ssh = None
        try:
            sftp, ssh = _connect(self.cfg)
            self._status("Copying…", "warn")
            total, copied, skipped, failed = self._cycle(sftp, verbose=True)
            if self._stop.is_set():
                self.log(f"cancelled — {copied} file(s) copied", "WARN")
                self._status("Cancelled", "muted")
                return
            self.log(f"done — {copied} copied, {skipped} already present, {failed} failed "
                     f"(of {total})", "ERR" if failed else "OK")
            self._status(f"Completed — {copied} copied" + (f", {failed} failed" if failed else ""),
                         "err" if failed else "ok")
        except Exception as exc:
            self.log(f"connection error: {exc}", "ERR")
            self._status("Connection failed", "err")
        finally:
            self._close(sftp, ssh)

    def _run_watch(self):
        """Live beamtime mode: poll forever until stopped."""
        interval = int(self.cfg.get("interval", 60) or 60)
        self.log(f"watch mode — polling every {interval}s")
        self._status("Connecting…", "warn")
        while not self._stop.is_set():
            sftp = ssh = None
            try:
                sftp, ssh = _connect(self.cfg)
                self._status("Connected — watching for new files", "ok")
                _, copied, _, failed = self._cycle(sftp, verbose=False)
                self.log(f"cycle done — {copied} new file(s)" if copied else "no new files this cycle",
                         "OK" if copied else "INFO")
            except Exception as exc:
                self.log(f"connection error: {exc}", "ERR")
                self._status("Connection failed — retrying…", "err")
            finally:
                self._close(sftp, ssh)
            for _ in range(interval * 2):                  # early-exit wait
                if self._stop.is_set():
                    break
                time.sleep(0.5)
        self.log("monitor stopped")
        self._status("Stopped", "muted")

    @staticmethod
    def _close(sftp, ssh):
        try:
            if sftp: sftp.close()
            if ssh: ssh.close()
        except Exception:
            pass

    def run(self):
        if self.once:
            self._run_once()
        else:
            self._run_watch()
