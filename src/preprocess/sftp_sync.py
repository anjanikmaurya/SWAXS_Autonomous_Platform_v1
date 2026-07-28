"""
src/preprocess/sftp_sync.py — poll a remote SFTP folder and copy new/updated files
into a local folder (e.g. a Google-Drive-synced directory), preserving sub-dirs.

Two modes (cfg["mode"]):
  • "watch" — keep polling every ``interval`` s and copy anything new (beamtime);
  • "once"  — walk the whole remote tree a single time, then stop (post-beamtime).

Throughput notes (why this is not slower than FileZilla):
  • a large SFTP window + packet size — paramiko's defaults are tiny and are the
    usual reason "python sftp" feels ~10× slower than a real client;
  • ``workers`` files transferred in parallel over separate channels on ONE ssh
    connection (FileZilla does the same thing with multiple connections);
  • file sizes come from the directory listing, so there is no extra stat()
    round-trip per file; and
  • in watch mode the connection is reused across polls instead of reconnecting.

paramiko is imported lazily so the module loads without it.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

CONFIG_FILE = Path.home() / ".swaxs_sftp_sync.json"

#: parallel file transfers (channels on one ssh connection)
DEFAULT_WORKERS = 4
#: SFTP flow-control window — the single biggest throughput lever
_WINDOW_SIZE = 2 ** 31 - 1
_PACKET_SIZE = 32768


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


def human_bytes(nbytes) -> str:
    """Auto-scaled size string — '1.42 GB', '318.7 MB', '4.0 KB'."""
    n = float(nbytes or 0)
    if n >= 1e12:
        return f"{n / 1e12:.2f} TB"
    if n >= 1e9:
        return f"{n / 1e9:.2f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.1f} MB"
    if n >= 1e3:
        return f"{n / 1e3:.1f} KB"
    return f"{int(n)} B"


def human_rate(nbytes: float, seconds: float) -> str:
    """'12.4 MB/s' / '1.05 GB/s' — for the transfer log."""
    if seconds <= 0:
        return "—"
    r = nbytes / seconds
    if r >= 1e9:
        return f"{r / 1e9:.2f} GB/s"
    if r >= 1e6:
        return f"{r / 1e6:.1f} MB/s"
    if r >= 1e3:
        return f"{r / 1e3:.0f} KB/s"
    return f"{r:.0f} B/s"


def _tune(transport):
    """Apply the big-window / no-compression settings before channels open."""
    try:
        transport.default_window_size = _WINDOW_SIZE
        transport.packet_size = _PACKET_SIZE
        transport.use_compression(False)      # data is already binary; CPU cost only
        transport.set_keepalive(30)
    except Exception:
        pass
    return transport


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
    _tune(ssh.get_transport())
    return paramiko.SFTPClient.from_transport(ssh.get_transport()), ssh


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
    """Copies a remote SFTP tree locally, in watch or one-time mode."""

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
        self.workers = max(1, min(16, int(cfg.get("workers", DEFAULT_WORKERS) or DEFAULT_WORKERS)))
        self._tl = threading.local()       # per-worker sftp channel
        # ── live progress (read by the UI via progress()) ──
        self._plock = threading.Lock()
        self._prog = {"phase": "idle", "files_done": 0, "files_total": 0,
                      "bytes_done": 0, "bytes_total": 0, "failed": 0,
                      "started": 0.0, "current": {}}

    def stop(self):
        self._stop.set()

    def log(self, msg, level="INFO"):
        self._log(level, f"[{datetime.now():%H:%M:%S}] {msg}")

    # ── progress reporting ───────────────────────────────────────────────────
    def _prog_reset(self, phase, files_total=0, bytes_total=0):
        with self._plock:
            self._prog.update(phase=phase, files_done=0, files_total=files_total,
                              bytes_done=0, bytes_total=bytes_total, failed=0,
                              started=time.time(), current={})

    def _prog_phase(self, phase):
        with self._plock:
            self._prog["phase"] = phase

    def progress(self) -> dict:
        """Snapshot for the UI: percent complete, rate, ETA, in-flight files."""
        with self._plock:
            p = dict(self._prog)
            cur = [{"name": n, "pct": (round(d / t * 100) if t else 0)}
                   for n, (d, t) in p["current"].items()]
        elapsed = max(1e-6, time.time() - p["started"]) if p["started"] else 0.0
        bt, bd = p["bytes_total"], p["bytes_done"]
        if bt > 0:
            pct = min(100.0, bd / bt * 100.0)
        elif p["files_total"]:
            pct = p["files_done"] / p["files_total"] * 100.0
        else:
            pct = 0.0
        rate = bd / elapsed if elapsed > 0 else 0.0
        eta = (bt - bd) / rate if (rate > 0 and bt > bd) else None
        return {"phase": p["phase"], "percent": round(pct, 1),
                "files_done": p["files_done"], "files_total": p["files_total"],
                "bytes_done": bd, "bytes_total": bt, "failed": p["failed"],
                "gb_done": round(bd / 1e9, 2), "gb_total": round(bt / 1e9, 2),
                "data_done": human_bytes(bd), "data_total": human_bytes(bt),
                "rate": human_rate(bd, elapsed), "eta_s": round(eta) if eta else None,
                "elapsed_s": round(elapsed), "current": cur[:8]}

    def _make_cb(self, name):
        """paramiko get() callback → aggregate byte counter (delta-based, so it is
        correct with several workers running at once)."""
        last = [0]
        def cb(done, total):
            with self._plock:
                self._prog["bytes_done"] += done - last[0]
                self._prog["current"][name] = (done, total)
            last[0] = done
        return cb

    # ── remote walk ──────────────────────────────────────────────────────────
    def _list_remote(self, sftp, remote_dir):
        """Recursive listing → [(path, mtime, size)]. Size comes from the listing
        so no extra stat() per file is needed later."""
        out = []
        try:
            for a in sftp.listdir_attr(remote_dir):
                rp = f"{remote_dir.rstrip('/')}/{a.filename}"
                if a.st_mode and (a.st_mode & 0o40000):    # directory
                    out.extend(self._list_remote(sftp, rp))
                else:
                    out.append((rp, a.st_mtime or 0.0, a.st_size or 0))
        except Exception as exc:
            self.log(f"cannot list {remote_dir}: {exc}", "WARN")
        return out

    # ── transfer ─────────────────────────────────────────────────────────────
    def _channel(self, ssh):
        """One SFTP channel per worker thread, all on the same ssh connection."""
        ch = getattr(self._tl, "sftp", None)
        if ch is None:
            import paramiko                                # noqa: PLC0415
            ch = paramiko.SFTPClient.from_transport(ssh.get_transport())
            self._tl.sftp = ch
        return ch

    def _needs_copy(self, remote_path, size) -> bool:
        """Local file missing or a different size → copy. No remote round-trip."""
        lp = relative_local_path(remote_path, self.cfg["remote_dir"], self.cfg["local_dir"])
        try:
            return not (lp.exists() and lp.stat().st_size == size)
        except Exception:
            return True

    def _fetch(self, ssh, remote_path, size) -> int:
        """Download one file; returns bytes written."""
        local_path = relative_local_path(remote_path, self.cfg["remote_dir"], self.cfg["local_dir"])
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_suffix(local_path.suffix + ".part")
        sftp = self._channel(ssh)
        name = local_path.name
        try:
            # prefetch is on by default; callback drives the progress bar
            sftp.get(remote_path, str(tmp), callback=self._make_cb(name))
            tmp.replace(local_path)                         # atomic
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            with self._plock:
                self._prog["current"].pop(name, None)
        return size

    def _cycle(self, ssh, sftp, verbose: bool):
        """One full pass. Returns (total, copied, skipped, failed)."""
        self._prog_phase("scanning")
        files = self._list_remote(sftp, self.cfg["remote_dir"])
        total = len(files)

        todo, skipped = [], 0
        for rp, mtime, size in files:
            if self.seen.get(rp) is not None and mtime <= self.seen[rp]:
                skipped += 1
            elif self._needs_copy(rp, size):
                todo.append((rp, mtime, size))
            else:
                self.seen[rp] = mtime                       # already on disk, same size
                skipped += 1

        bytes_total = sum(s for _, _, s in todo)
        if verbose:
            self.log(f"{total} remote file(s) — {len(todo)} to copy "
                     f"({human_bytes(bytes_total)}), {skipped} already present · "
                     f"{self.workers} parallel transfers")
        if not todo:
            self._prog_reset("idle")
            return total, 0, skipped, 0

        self._prog_reset("copying", files_total=len(todo), bytes_total=bytes_total)
        copied = failed = 0
        nbytes = 0
        t0 = time.time()
        done = 0
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = {ex.submit(self._fetch, ssh, rp, size): (rp, mtime, size)
                    for rp, mtime, size in todo}
            for fut in as_completed(futs):
                rp, mtime, size = futs[fut]
                done += 1
                if self._stop.is_set():
                    fut.cancel(); continue
                fn = Path(rp).name
                try:
                    nbytes += fut.result()
                    copied += 1
                    self.seen[rp] = mtime
                    self.log(f"[{done}/{len(todo)}] saved {fn}", "OK")
                except Exception as exc:
                    failed += 1
                    self.log(f"[{done}/{len(todo)}] failed {fn}: {exc}", "ERR")
                with self._plock:
                    self._prog["files_done"] = done
                    self._prog["failed"] = failed

        self._prog_phase("idle")
        dt = time.time() - t0
        if copied:
            self.log(f"{copied} file(s), {human_bytes(nbytes)} in {dt:.1f}s "
                     f"({human_rate(nbytes, dt)})", "OK")
        return total, copied, skipped, failed

    # ── modes ────────────────────────────────────────────────────────────────
    def _run_once(self):
        """Post-beamtime bulk copy: one full pass, then stop."""
        self.log("one-time copy — scanning the whole remote folder")
        self._status("Connecting…", "warn")
        sftp = ssh = None
        try:
            sftp, ssh = _connect(self.cfg)
            self._status("Copying…", "warn")
            total, copied, skipped, failed = self._cycle(ssh, sftp, verbose=True)
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
        """Live beamtime mode: poll until stopped, reusing the connection."""
        interval = int(self.cfg.get("interval", 60) or 60)
        self.log(f"watch mode — polling every {interval}s · {self.workers} parallel transfers")
        self._status("Connecting…", "warn")
        sftp = ssh = None
        while not self._stop.is_set():
            try:
                if sftp is None:                            # (re)connect only when needed
                    sftp, ssh = _connect(self.cfg)
                    self._status("Connected — watching for new files", "ok")
                _, copied, _, failed = self._cycle(ssh, sftp, verbose=False)
                if not copied:
                    self.log("no new files this cycle")
            except Exception as exc:
                self.log(f"connection error: {exc}", "ERR")
                self._status("Connection failed — retrying…", "err")
                self._close(sftp, ssh)
                sftp = ssh = None                           # force a fresh connect
                self._tl = threading.local()                # drop stale worker channels
            for _ in range(interval * 2):                   # early-exit wait
                if self._stop.is_set():
                    break
                time.sleep(0.5)
        self._close(sftp, ssh)
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
