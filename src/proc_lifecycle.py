"""
src/proc_lifecycle.py — start/stop a platform app for real.

The hub used to manage sub-apps with `Popen.terminate()` and a bare port check,
which left three ways to end up stuck:

1. **Stop did not stop anything it had not spawned.** `_stop_app` returned
   "Not running" whenever its own `Popen` handle was gone — even when a live
   process from an earlier hub run was still listening on the port and still
   writing files.
2. **Stop did not wait for the port.** It returned success the moment
   `wait()` came back, so an immediate restart could race the socket teardown.
3. **Start refused instead of recovering.** A held port produced
   "Port 5003 is already in use — free that port and retry", with no way to
   free it from the UI. Close-then-start-fresh was impossible without a
   terminal.

This module does the process work, keeps it out of the Flask layer, and makes it
testable. Everything is best-effort and never raises.

SAFETY
------
`reclaim_port` will only kill a process it can positively identify as one of this
platform's apps — its command line must reference the expected entry file or the
project root. Anything else (a database, another user's server, an editor) is
reported by name and left alone. Killing by port number alone would be a
foot-gun on a shared beamline workstation.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

try:                                    # declared in requirements.txt
    import psutil
except Exception:                       # pragma: no cover - degraded mode
    psutil = None

IS_WIN = sys.platform.startswith("win")


# ── ports ────────────────────────────────────────────────────────────────────
def _connect_probe(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, int(port))) == 0


def can_bind(port: int, host: str = "127.0.0.1") -> bool:
    """Can WE bind this port? The only question that actually matters.

    This is deliberately a real bind attempt rather than an inference from the
    process table, because the two disagree in practice. On macOS, AirPlay
    Receiver (`ControlCenter`) listens on port 5000 — but a Flask app can still
    bind 127.0.0.1:5000 alongside it. A process-table check sees "port 5000 is
    taken by ControlCenter" and refuses to start something that would have worked
    perfectly well. Asking the kernel removes the guesswork.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # SO_REUSEADDR because werkzeug sets it too, and this must predict
            # exactly what the server will manage. Without it, a connection left in
            # TIME_WAIT from the app we just stopped reads as "port unavailable"
            # and a restart is refused for no reason. SO_REUSEADDR relaxes only
            # TIME_WAIT — an active LISTEN socket still blocks the bind, which is
            # the collision we actually want to detect.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, int(port)))
            s.listen(1)
        return True
    except OSError:
        return False


def port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """Is something we would collide with bound to this port?

    True only when we CANNOT bind it ourselves. Anything else — a listener on a
    different interface, an unrelated service the OS lets us share the port with —
    is not our problem and must not stop an app from starting.
    """
    return not can_bind(port, host)


def wait_port_free(port: int, timeout: float = 8.0, step: float = 0.15) -> bool:
    """Block until nothing answers on the port. This is the check that matters:
    a process can be gone while its listening socket is still being torn down,
    and a restart that races it dies with EADDRINUSE."""
    end = time.time() + timeout
    while time.time() < end:
        if can_bind(port):
            return True
        time.sleep(step)
    return can_bind(port)


_LOCAL_ADDRS = ("127.0.0.1", "0.0.0.0", "::", "::1", "*", "")


def listeners(port: int) -> list:
    """psutil.Process objects LISTENING on the port on loopback or the wildcard.

    Used only to explain and to reclaim — never to decide whether we can start
    (see :func:`can_bind`). Restricted to loopback/wildcard because a service
    bound to a specific external interface does not collide with us, and
    restricted to LISTEN because an outbound connection whose ephemeral local port
    happens to equal 5003 is not holding anything.
    """
    if psutil is None:
        return []
    out, seen = [], set()
    try:
        for c in psutil.net_connections(kind="inet"):
            if not c.laddr or c.laddr.port != int(port):
                continue
            if c.status != psutil.CONN_LISTEN:
                continue
            if getattr(c.laddr, "ip", "") not in _LOCAL_ADDRS:
                continue
            if not c.pid or c.pid in seen:
                continue
            seen.add(c.pid)
            try:
                out.append(psutil.Process(c.pid))
            except Exception:
                pass
    except Exception:
        # macOS raises AccessDenied for net_connections without elevation; fall
        # back to scanning our own processes, which is all we are allowed to kill
        # anyway.
        try:
            me = psutil.Process().username()
            for p in psutil.process_iter(["pid", "username"]):
                try:
                    if p.info.get("username") != me:
                        continue
                    for c in p.net_connections(kind="inet"):
                        if (c.laddr and c.laddr.port == int(port)
                                and c.status == psutil.CONN_LISTEN
                                and getattr(c.laddr, "ip", "") in _LOCAL_ADDRS
                                and p.pid not in seen):
                            seen.add(p.pid); out.append(p)
                except Exception:
                    continue
        except Exception:
            pass
    return out


def describe(proc) -> dict:
    """A line an operator can act on: who is holding this port?"""
    try:
        return {"pid": proc.pid, "name": proc.name(),
                "cmdline": " ".join(proc.cmdline() or [])[:400],
                "created": proc.create_time()}
    except Exception:
        return {"pid": getattr(proc, "pid", None), "name": "?", "cmdline": "",
                "created": None}


# ── identification ───────────────────────────────────────────────────────────
def is_our_app(proc, entry: str | Path, root: str | Path) -> bool:
    """True only if this process is plainly one of OUR app processes.

    Matched on the command line containing the app's entry file (e.g.
    ``background/app.py``) or, failing that, the project root plus a `python`
    executable. Deliberately strict: a false positive here kills someone else's
    program.
    """
    try:
        cmd = " ".join(proc.cmdline() or [])
    except Exception:
        return False
    if not cmd:
        return False
    ent = Path(entry)
    # match the tail of the entry path so absolute and relative forms both work
    # ("background/app.py" appears in both "python background/app.py" and
    #  "python /Users/…/SWAXS/background/app.py")
    tail = "/".join(ent.parts[-2:]) if len(ent.parts) >= 2 else ent.name
    norm = cmd.replace("\\", "/")
    if tail and tail in norm:
        return True
    # Narrow fallback: the project root AND the app's own directory AND python.
    # All three are required — matching the root alone would claim any unrelated
    # script an operator happens to be running from the same checkout.
    rootstr = str(Path(root)).replace("\\", "/")
    return bool(rootstr and rootstr in norm
                and f"/{ent.parent.name}/" in norm
                and "python" in norm.lower())


# ── killing ──────────────────────────────────────────────────────────────────
def _gone(p) -> bool:
    """Is this process effectively dead?

    A ZOMBIE counts as gone. `psutil.wait_procs` does not: it reports a zombie as
    alive until the process's real parent reaps it, and an orphan we kill is not
    our child — so waiting on it burned the entire grace period on every single
    reclaim, which is why a hub restart used to take eight seconds. A zombie has
    already released its sockets and files, which is all we care about.
    """
    if psutil is None:
        return True
    try:
        if not p.is_running():
            return True
        return p.status() == psutil.STATUS_ZOMBIE
    except Exception:
        return True          # NoSuchProcess / AccessDenied → not our problem


def _wait_gone(procs, timeout: float, step: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if all(_gone(p) for p in procs):
            return True
        time.sleep(step)
    return all(_gone(p) for p in procs)


def kill_tree(proc, *, grace: float = 5.0) -> str:
    """Terminate a process and everything it spawned, escalating if needed.

    Returns "already-gone" | "terminated" | "killed" | "failed: ...".

    Children matter even though the apps are single-process today: a future app
    that shells out to a fitting binary would otherwise leave that binary running
    and still holding file handles in the project folder.
    """
    if proc is None:
        return "already-gone"
    if psutil is not None and isinstance(proc, psutil.Process):
        try:
            if _gone(proc):
                return "already-gone"
            kids = proc.children(recursive=True)
            targets = kids + [proc]
            for p in targets:
                try:
                    p.terminate()
                except Exception:
                    pass
            if _wait_gone(targets, grace):
                return "terminated"
            for p in targets:
                if not _gone(p):
                    try:
                        p.kill()
                    except Exception:
                        pass
            _wait_gone(targets, 3.0)
            return "killed"
        except Exception as exc:
            return f"failed: {exc}"

    # subprocess.Popen path (no psutil, or the handle we spawned)
    try:
        if proc.poll() is not None:
            return "already-gone"
    except Exception:
        return "already-gone"
    try:
        _signal_group(proc, signal.SIGTERM)
        proc.wait(timeout=grace)
        return "terminated"
    except subprocess.TimeoutExpired:
        try:
            _signal_group(proc, signal.SIGKILL if not IS_WIN else signal.SIGTERM)
            proc.kill()
            proc.wait(timeout=3.0)
        except Exception:
            pass
        return "killed"
    except Exception as exc:
        return f"failed: {exc}"


def _signal_group(proc, sig) -> None:
    """Signal the child's whole process group when we created one for it."""
    try:
        if IS_WIN:
            proc.terminate()
            return
        os.killpg(os.getpgid(proc.pid), sig)
    except Exception:
        try:
            proc.send_signal(sig)
        except Exception:
            pass


def popen_kwargs() -> dict:
    """Extra Popen args that put the child in its own process group, so the whole
    tree can be signalled at once and so Ctrl-C in the hub's terminal does not
    race the hub's own orderly shutdown."""
    if IS_WIN:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def reclaim_port(port: int, entry: str | Path, root: str | Path,
                 *, grace: float = 5.0) -> tuple[bool, str, list[dict]]:
    """Free a port held by one of OUR orphaned apps.

    Returns (freed, message, killed). ``freed`` is False when the port is held by
    something we refuse to touch — the message then names it, which is far more
    useful than "port in use".
    """
    if not port_in_use(port):
        return True, "port was already free", []

    holders = listeners(port)
    if not holders:
        # We cannot see the owner (no psutil, or the OS hid it). Do NOT guess.
        return False, (f"port {port} is in use but the owning process could not be "
                       f"identified — close it manually, then start the app"), []

    killed, refused = [], []
    for h in holders:
        info = describe(h)
        if is_our_app(h, entry, root):
            info["result"] = kill_tree(h, grace=grace)
            killed.append(info)
        else:
            refused.append(info)

    if refused and not killed:
        who = "; ".join(f"PID {r['pid']} {r['name']}" for r in refused)
        return False, (f"port {port} is held by a process that is not part of this "
                       f"platform ({who}) — leaving it alone"), []

    freed = wait_port_free(port, timeout=grace + 3.0)
    pids = ", ".join(str(k["pid"]) for k in killed)
    if freed:
        msg = f"reclaimed port {port} from orphaned app process(es) {pids}"
    else:
        msg = (f"killed orphaned process(es) {pids} but port {port} is still "
               f"bound — wait a few seconds and retry")
    if refused:
        msg += f" (left {len(refused)} unrelated process(es) alone)"
    return freed, msg, killed


# ── log rotation ─────────────────────────────────────────────────────────────
def rotate_log(path: Path, keep: int = 3) -> None:
    """Roll <app>.log to <app>.log.1 … before a fresh start.

    The hub used to open the log with mode "w", so restarting a crashed app
    destroyed the traceback that explained the crash — the one artefact needed at
    08:00 was deleted by the first recovery attempt (platform audit O17). Each
    start still gets a clean file; the previous run is one suffix away.
    """
    try:
        path = Path(path)
        if not path.exists() or path.stat().st_size == 0:
            return
        for i in range(keep - 1, 0, -1):
            older, newer = path.with_suffix(path.suffix + f".{i}"), None
            newer = path.with_suffix(path.suffix + f".{i + 1}")
            if older.exists():
                older.replace(newer)
        path.replace(path.with_suffix(path.suffix + ".1"))
    except Exception:
        pass


# ── orphan reaping across hub restarts ───────────────────────────────────────
def read_children(path: Path) -> dict:
    try:
        import json
        return json.loads(Path(path).read_text() or "{}") or {}
    except Exception:
        return {}


def write_children(path: Path, mapping: dict) -> None:
    """Record app_id -> {pid, port, entry, started} so a *new* hub can find the
    children of a hub that was SIGKILLed. Without this, orphans keep running and
    writing to the project folder while the UI reports "Stopped" and every port
    is unavailable."""
    try:
        import json
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(mapping, indent=1))
        tmp.replace(p)
    except Exception:
        pass


def reap_orphans(children: dict, apps: dict, root: str | Path) -> list[str]:
    """Kill leftovers from a previous hub run so this one starts clean.

    ``children`` is the file written by :func:`write_children`; ``apps`` maps
    app_id -> {"port", "entry"}. Only processes that still identify as the SAME
    app are killed — a recycled PID belonging to something else is skipped.
    """
    notes: list[str] = []
    if psutil is None:
        return notes
    for app_id, rec in (children or {}).items():
        meta = apps.get(app_id) or {}
        pid = rec.get("pid")
        entry = rec.get("entry") or meta.get("entry") or ""
        if not pid or not entry:
            continue
        try:
            p = psutil.Process(int(pid))
        except Exception:
            continue
        if _gone(p):
            continue                    # already dead (or a zombie awaiting reap)
        if not is_our_app(p, entry, root):
            continue                    # PID recycled — not ours any more
        res = kill_tree(p)
        notes.append(f"{app_id} (PID {pid}): {res}")
    return notes
