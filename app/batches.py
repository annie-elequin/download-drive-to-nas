"""Batch paste of Drive links → discover → review → enqueue selective downloads."""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app import drive_select, jobs

MAX_URLS_DEFAULT = 120
_lock = threading.Lock()


@dataclass
class BatchItem:
    index: int
    source_url: str
    status: str = "pending"  # pending|ok|error
    error: str | None = None
    parent_folder: str | None = None  # linked Drive folder title (for ZIP / archive name)
    beautyshots: list[dict[str, Any]] = field(default_factory=list)  # {id, name, selected}
    stl_folder: str | None = None
    stl_files: list[dict[str, Any]] = field(default_factory=list)  # {id, path}
    include_stl: bool = True
    note: str | None = None
    other_root_files: list[str] = field(default_factory=list)
    other_root_folders: list[str] = field(default_factory=list)


@dataclass
class Batch:
    id: str
    status: str = "discovering"  # discovering|ready|failed
    destination_path: str = ""
    phase: str = ""
    log: str = ""
    items: list[BatchItem] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None


_batches: dict[str, Batch] = {}


def max_urls() -> int:
    import os

    try:
        return int(os.environ.get("BATCH_MAX_URLS", str(MAX_URLS_DEFAULT)))
    except ValueError:
        return MAX_URLS_DEFAULT


def parse_urls_blob(blob: str) -> list[str]:
    lines = []
    for line in (blob or "").splitlines():
        u = line.strip()
        if not u or u.startswith("#"):
            continue
        lines.append(u)
    return lines


def create_batch(urls: list[str], destination_path: str) -> Batch:
    jobs.safe_destination_path(destination_path)
    mx = max_urls()
    if len(urls) > mx:
        raise ValueError(f"Too many URLs (max {mx})")

    batch_id = uuid.uuid4().hex
    items = [BatchItem(index=i, source_url=u) for i, u in enumerate(urls)]
    batch = Batch(
        id=batch_id,
        destination_path=destination_path.strip(),
        items=items,
        phase=f"Queued discovery for {len(items)} link(s)…",
    )
    with _lock:
        _batches[batch_id] = batch
    threading.Thread(target=_discover_worker, args=(batch_id,), daemon=True).start()
    return batch


def get_batch(batch_id: str) -> Batch | None:
    with _lock:
        return _batches.get(batch_id)


def batch_public_dict(batch: Batch) -> dict[str, Any]:
    with _lock:
        return {
            "id": batch.id,
            "status": batch.status,
            "phase": batch.phase,
            "log": batch.log,
            "destination_path": batch.destination_path,
            "items": [
                {
                    "index": it.index,
                    "source_url": it.source_url,
                    "status": it.status,
                    "error": it.error,
                    "parent_folder": it.parent_folder,
                    "beautyshots": list(it.beautyshots),
                    "stl_folder": it.stl_folder,
                    "stl_file_count": len(it.stl_files),
                    "include_stl": it.include_stl,
                    "note": it.note,
                    "other_root_files": list(it.other_root_files),
                    "other_root_folders": list(it.other_root_folders),
                }
                for it in batch.items
            ],
            "created_at": batch.created_at,
            "finished_at": batch.finished_at,
        }


def _append_batch_log(batch: Batch, text: str) -> None:
    with _lock:
        batch.log += text
        if len(batch.log) > 32000:
            batch.log = batch.log[-32000:]


def _discover_worker(batch_id: str) -> None:
    batch = get_batch(batch_id)
    if not batch:
        return
    try:
        n = len(batch.items)
        for i, it in enumerate(batch.items):
            with _lock:
                batch.phase = f"Discovering {i + 1}/{n}…"
            _append_batch_log(batch, f"\n[{i + 1}/{n}] {it.source_url}\n")
            r = drive_select.discover_public_folder(it.source_url)
            with _lock:
                it.parent_folder = r.parent_folder
                if not r.ok:
                    it.status = "error"
                    it.error = r.error or "Unknown error"
                else:
                    it.status = "ok"
                    it.stl_folder = r.stl_folder
                    it.stl_files = list(r.stl_files or [])
                    it.note = r.note
                    it.other_root_files = list(r.other_root_files or [])
                    it.other_root_folders = list(r.other_root_folders or [])
                    it.beautyshots = [
                        {"id": b["id"], "name": b["name"], "selected": True}
                        for b in (r.beautyshots or [])
                    ]
                    if not it.beautyshots:
                        it.note = (it.note + " " if it.note else "") + (
                            "No file with 'beautyshot' in the name at folder root or under Render Images/."
                        )
        with _lock:
            batch.status = "ready"
            batch.phase = "Review selections below, then approve."
            batch.finished_at = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        with _lock:
            batch.status = "failed"
            batch.phase = "Discovery failed."
            _append_batch_log(batch, f"\nFatal: {exc}\n")
            batch.finished_at = datetime.now(timezone.utc).isoformat()


def approve_batch(
    batch_id: str,
    selections: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    selections: [{ "index": 0, "include_stl": true, "beautyshot_ids": ["id1", ...] }, ...]
    Returns {"job_ids": [...], "started": [{ "job_id", "source_url" }, ...]}.
    """
    batch = get_batch(batch_id)
    if not batch:
        raise ValueError("Batch not found")
    with _lock:
        if batch.status != "ready":
            raise ValueError("Batch is not ready for approval")

    by_index = {s.get("index"): s for s in selections}
    job_ids: list[str] = []
    started: list[dict[str, str]] = []

    for it in batch.items:
        sel = by_index.get(it.index) or {}
        include_stl = bool(sel.get("include_stl", True))
        if it.status != "ok":
            continue
        ids = sel.get("beautyshot_ids")
        if isinstance(ids, list):
            beauty_list = [b for b in it.beautyshots if b["id"] in ids]
        else:
            beauty_list = list(it.beautyshots)
        if not include_stl and not beauty_list:
            continue
        stl_part = list(it.stl_files) if include_stl else []
        raw = (it.parent_folder or it.stl_folder or "pack").replace("/", "_")
        slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw)[:80].strip("._-") or "pack"
        label = slug[:120]
        spec = {
            "source_url": it.source_url,
            "label": label,
            "beautyshots": [{"id": b["id"], "name": b["name"]} for b in beauty_list],
            "stl_files": stl_part,
        }
        dest = batch.destination_path
        archive = jobs.sanitize_segment(slug)
        job = jobs.create_selective_job(it.source_url, dest, archive, spec)
        jobs.start_job_worker(job.id)
        job_ids.append(job.id)
        started.append({"job_id": job.id, "source_url": it.source_url})

    with _lock:
        batch.phase = f"Started {len(job_ids)} download job(s). Open each job page for progress."
    return {"job_ids": job_ids, "started": started}
