"""
src/folder_browse.py — the folder listing behind every app's Browse… button.

``GET /api/browse?path=…`` returns ``{"current", "parent", "dirs"}`` — the
sub-folders of ``path`` (hidden ones skipped). The shared picker in
``_icon_helpers.html`` (``swaxsBrowse``) reads exactly this shape.

A relative path is resolved against the project root, so a field holding
``1D/SAXS/Subtracted`` opens at that folder inside the project rather than
inside whatever directory the app happened to start in. A path that does not
exist yet falls back to its nearest existing parent. Read only: it lists
folder names and never creates, moves or deletes anything.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional


def list_dirs(raw: str = "", project_root: Optional[str] = None) -> dict:
    raw = (raw or "").strip()
    base = Path(project_root) if project_root else Path.home()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = base / p
    else:
        p = base
    while not p.exists() and p != p.parent:
        p = p.parent
    if not p.is_dir():
        p = Path.home()
    try:
        dirs = sorted((d.name for d in p.iterdir()
                       if d.is_dir() and not d.name.startswith(".")), key=str.lower)
    except (PermissionError, OSError):
        dirs = []
    return {"current": str(p), "parent": str(p.parent) if p != p.parent else None,
            "dirs": dirs}


def register_browse(app, project_root: Callable[[], Optional[str]]) -> None:
    """Add ``GET /api/browse`` to a Flask app, unless it already has one."""
    from flask import jsonify, request  # noqa: PLC0415
    if any(r.rule == "/api/browse" for r in app.url_map.iter_rules()):
        return

    def api_browse():
        return jsonify(list_dirs(request.args.get("path", ""), project_root()))

    app.add_url_rule("/api/browse", "api_browse", api_browse, methods=["GET"])
