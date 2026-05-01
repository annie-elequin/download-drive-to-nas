"""Discover files in a public Drive folder for STL + beautyshot selection (gdown dry-run)."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from typing import Any


def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/")


def _is_render_images_folder(segment: str) -> bool:
    return segment.strip().lower() == "render images"


def _is_stl_root_folder(segment: str) -> bool:
    """Top-level folder named STL, or * _STL suffix (e.g. Ashitaka_STL)."""
    s = segment.strip().lower()
    return s == "stl" or s.endswith("_stl")


def _choose_stl_folder(stl_by_prefix: dict[str, list[Any]]) -> tuple[str, str | None]:
    keys = list(stl_by_prefix.keys())
    exact_stl = [k for k in keys if k.strip().lower() == "stl"]
    if exact_stl:
        chosen = sorted(exact_stl)[0]
        if len(keys) > 1:
            return (
                chosen,
                f"Multiple STL-style folders ({', '.join(sorted(keys))}); using {chosen}.",
            )
        return chosen, None
    keys_sorted = sorted(keys)
    chosen = keys_sorted[0]
    if len(keys) > 1:
        return (
            chosen,
            f"Multiple STL-style folders ({', '.join(keys_sorted)}); using {chosen}.",
        )
    return chosen, None


@dataclass
class DiscoveryResult:
    ok: bool
    error: str | None = None
    parent_folder: str | None = None  # Drive folder title (gdown root), for ZIP naming
    beautyshots: list[dict[str, Any]] | None = None  # {id, name}
    stl_folder: str | None = None  # first path segment, e.g. STL or Ashitaka_STL
    stl_files: list[dict[str, Any]] | None = None  # {id, path}
    other_root_files: list[str] | None = None  # names only, for preview
    other_root_folders: list[str] | None = None
    note: str | None = None


def _parent_folder_from_gdown_paths(entries: list[Any], output_dir: str) -> str | None:
    """First path segment under output_dir from gdown's local_path (the linked folder's title)."""
    if not entries:
        return None
    lp = getattr(entries[0], "local_path", None) or ""
    if not lp:
        return None
    try:
        out_abs = os.path.abspath(output_dir)
        lp_abs = os.path.abspath(lp)
        rel = os.path.relpath(lp_abs, out_abs)
    except (ValueError, OSError):
        return None
    parts = _norm_path(rel).split("/")
    if not parts or parts[0] in (".", ".."):
        return None
    return parts[0] or None


def _analyze_entries(entries: list[Any]) -> DiscoveryResult:
    """Split gdown skip_download entries into beautyshots vs STL tree (folder STL or *_STL)."""
    root_beautyshots: list[dict[str, Any]] = []
    render_beautyshots: list[dict[str, Any]] = []
    stl_by_prefix: dict[str, list[dict[str, Any]]] = {}
    other_files: list[str] = []
    root_folders: set[str] = set()

    for ent in entries:
        path = _norm_path(getattr(ent, "path", "") or "")
        fid = getattr(ent, "id", None)
        if not path or not fid:
            continue
        parts = path.split("/")
        if len(parts) == 1:
            name = parts[0]
            if "beautyshot" in name.lower():
                root_beautyshots.append({"id": fid, "name": name})
            else:
                other_files.append(name)
            continue
        # nested: first segment may be a folder name
        root = parts[0]
        if _is_stl_root_folder(root):
            stl_by_prefix.setdefault(root, []).append({"id": fid, "path": path})
        else:
            root_folders.add(root)
            if (
                _is_render_images_folder(root)
                and len(parts) >= 2
                and "beautyshot" in parts[-1].lower()
            ):
                # Flat name for ZIP temp dir (unique if nested under Render Images)
                out_name = parts[-1] if len(parts) == 2 else "__".join(parts[1:])
                render_beautyshots.append({"id": fid, "name": out_name})
            else:
                other_files.append(path)

    render_trim_note: str | None = None
    if root_beautyshots:
        beautyshots = root_beautyshots
    elif render_beautyshots:
        rb_sorted = sorted(
            render_beautyshots,
            key=lambda b: (str(b.get("name", "")).lower(), str(b.get("id", ""))),
        )
        beautyshots = [rb_sorted[0]]
        if len(render_beautyshots) > 1:
            render_trim_note = (
                f"Several beautyshots under Render Images/; using only the first ({rb_sorted[0]['name']!r})."
            )
    else:
        beautyshots = []

    if not stl_by_prefix:
        return DiscoveryResult(
            ok=False,
            error=(
                "No top-level STL folder was found: need a folder named STL or whose name ends with "
                "_STL, with at least one file inside."
            ),
            beautyshots=beautyshots or None,
            other_root_files=sorted(set(other_files))[:50] or None,
            other_root_folders=sorted(root_folders)[:50] or None,
        )

    chosen, stl_note = _choose_stl_folder(stl_by_prefix)
    note = stl_note
    if not root_beautyshots and render_beautyshots:
        extra = "Beautyshot from Render Images/ (none at folder root)."
        note = f"{note} {extra}".strip() if note else extra
        if render_trim_note:
            note = f"{note} {render_trim_note}".strip()

    return DiscoveryResult(
        ok=True,
        beautyshots=beautyshots or None,
        stl_folder=chosen,
        stl_files=stl_by_prefix[chosen],
        other_root_files=sorted(set(other_files))[:80] or None,
        other_root_folders=sorted(root_folders)[:80] or None,
        note=note,
    )


def discover_public_folder(url: str) -> DiscoveryResult:
    """
    List contents of a public folder without downloading (gdown skip_download).
    Identifies beautyshot files (root-level names containing 'beautyshot', or if none,
    the first matching file under Render Images/ by filename), and files under a top-level
    folder named STL or whose name ends with _STL.
    """
    import gdown

    url = (url or "").strip()
    if not url:
        return DiscoveryResult(ok=False, error="Empty URL")

    tmp: str | None = None
    try:
        tmp = tempfile.mkdtemp(prefix="gdisc_")
        # output must end with separator for gdown's root_dir join behavior
        out = tmp + os.sep
        res = gdown.download_folder(url=url, output=out, skip_download=True, quiet=True, use_cookies=True)
    except Exception as exc:
        return DiscoveryResult(ok=False, error=str(exc))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    if res is None:
        return DiscoveryResult(
            ok=False,
            error="gdown could not read this folder (permissions, link type, or network).",
        )
    if not res:
        return DiscoveryResult(ok=False, error="Folder appears empty to gdown.")

    entries = list(res)
    parent_folder = _parent_folder_from_gdown_paths(entries, out)
    analyzed = _analyze_entries(entries)
    return replace(analyzed, parent_folder=parent_folder)
