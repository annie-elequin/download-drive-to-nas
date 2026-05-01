import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


MAX_LOG_CHARS = 32_000

# gdown prints "Processing file", id, name (stdout); per-file download uses stderr (merged) with "To:" and tqdm %.
RE_PROCESSING_FILE = re.compile(r"^Processing file\s+(\S+)\s+(.+)$")
RE_TO_LINE = re.compile(r"^To:\s*(.+?)\s*$")
RE_SKIPPING = re.compile(r"Skipping already downloaded file\s+(.+)")
RE_PERCENT = re.compile(r"(\d{1,3})%")


@dataclass
class Job:
    id: str
    status: JobStatus = JobStatus.PENDING
    job_kind: str = "full"  # "full" | "selective"
    selective: dict[str, Any] | None = None
    drive_url: str = ""
    destination_path: str = ""
    archive_base: str = ""
    phase: str = ""
    message: str = ""
    log: str = ""
    zip_path: str | None = None
    files: list[dict[str, Any]] = field(default_factory=list)
    skipped_downloads: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    proc: subprocess.Popen | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None


_jobs: dict[str, Job] = {}
_pipeline_lock = threading.Lock()
_job_fields_lock = threading.RLock()


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data")).expanduser().resolve(strict=False)


def sanitize_segment(name: str, max_len: int = 128) -> str:
    s = name.strip()
    if not s or len(s) > max_len:
        raise ValueError("Invalid name length")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", s):
        raise ValueError("Name may only contain letters, numbers, ._-")
    if s in (".", "..") or s.startswith("."):
        raise ValueError("Invalid name")
    return s


def safe_destination_path(user_path: str) -> Path:
    """Resolve user path; must lie under DATA_DIR (after expanduser + resolve)."""
    raw = (user_path or "").strip()
    if not raw or len(raw) > 4096 or "\x00" in raw:
        raise ValueError("Invalid destination path")
    dest = Path(raw).expanduser()
    try:
        resolved = dest.resolve(strict=False)
    except OSError as exc:
        raise ValueError("Invalid destination path") from exc
    base = _data_dir()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError("Destination must be inside DATA_DIR") from exc
    return resolved


def default_output_suggestion() -> str:
    """Prefilled destination in the form (must still resolve under DATA_DIR)."""
    custom = (os.environ.get("DEFAULT_OUTPUT_PATH") or "").strip()
    if custom:
        try:
            return str(safe_destination_path(custom))
        except ValueError:
            pass
    return str(_data_dir() / "exports")


def create_job(drive_url: str, destination_path: str, archive_base: str) -> Job:
    job_id = uuid.uuid4().hex
    # Validate early so bad paths fail before the worker starts
    safe_destination_path(destination_path)
    job = Job(
        id=job_id,
        drive_url=drive_url.strip(),
        destination_path=destination_path.strip(),
        archive_base=sanitize_segment(archive_base) if archive_base.strip() else "",
        phase="Queued — starting soon…",
    )
    _jobs[job_id] = job
    return job


def create_selective_job(
    drive_url: str,
    destination_path: str,
    archive_base: str,
    spec: dict[str, Any],
) -> Job:
    """Download only chosen beautyshot files + listed STL tree files, then zip."""
    job_id = uuid.uuid4().hex
    safe_destination_path(destination_path)
    ab = sanitize_segment(archive_base) if archive_base.strip() else ""
    job = Job(
        id=job_id,
        job_kind="selective",
        selective=dict(spec),
        drive_url=drive_url.strip(),
        destination_path=destination_path.strip(),
        archive_base=ab,
        phase="Queued (selective download)…",
    )
    _jobs[job_id] = job
    return job


def _safe_download_basename(name: str) -> str:
    b = os.path.basename((name or "").replace("\\", "/"))
    if not b or b in (".", "..") or ".." in b or "/" in b or "\\" in b:
        raise ValueError(f"Unsafe filename: {name!r}")
    return b


def _safe_rel_path(path: str) -> Path:
    parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
    for p in parts:
        if p == ".." or p.startswith(".."):
            raise ValueError("Invalid path in STL file list")
    return Path(*parts) if parts else Path("file")


def _run_selective_pipeline(job: Job) -> None:
    from gdown import download

    with _pipeline_lock:
        with _job_fields_lock:
            if job.cancel_requested:
                job.status = JobStatus.CANCELLED
                job.message = "Cancelled"
                job.phase = "Cancelled."
                job.finished_at = datetime.now(timezone.utc).isoformat()
                return
            job.status = JobStatus.RUNNING

    spec = job.selective or {}
    tmp_root: str | None = None
    try:
        dest_dir = safe_destination_path(job.destination_path)
        dest_dir.mkdir(parents=True, exist_ok=True)
        archive_name = job.archive_base or f"archive_{job.id[:8]}"
        archive_name = sanitize_segment(archive_name, max_len=200)
        if archive_name.endswith(".zip"):
            archive_name = archive_name[:-4]

        tmp_root = tempfile.mkdtemp(prefix="sel_")
        tmp_path = Path(tmp_root)
        _append_log(job, f"Selective pack from: {job.drive_url}\n")
        _append_log(job, f"Temp directory: {tmp_path}\n")

        beauty = list(spec.get("beautyshots") or [])
        stl_files = list(spec.get("stl_files") or [])

        _set_phase(job, f"Downloading {len(beauty)} beautyshot(s) and {len(stl_files)} STL file(s)…")

        ok_count = 0

        for b in beauty:
            with _job_fields_lock:
                if job.cancel_requested:
                    _finalize_cancelled(job, tmp_root, "\nCancelled during selective download.\n")
                    return
            bn = _safe_download_basename(b["name"])
            out = tmp_path / bn
            bid = b.get("id") or ""
            try:
                _append_log(job, f"→ {bn}\n")
                download(id=b["id"], output=str(out), quiet=True, use_cookies=True)
                ok_count += 1
            except Exception as exc:
                line = f"Beautyshot {bn!r} (id={bid}): {exc}"
                with _job_fields_lock:
                    job.skipped_downloads.append(line)
                _append_log(job, f"SKIP: {line}\n")
                if out.exists():
                    try:
                        out.unlink()
                    except OSError:
                        pass

        for sf in stl_files:
            with _job_fields_lock:
                if job.cancel_requested:
                    _finalize_cancelled(job, tmp_root, "\nCancelled during selective download.\n")
                    return
            rel = _safe_rel_path(sf.get("path") or "")
            out = tmp_path / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            sid = sf.get("id") or ""
            try:
                _append_log(job, f"→ {rel.as_posix()}\n")
                download(id=sf["id"], output=str(out), quiet=True, use_cookies=True)
                ok_count += 1
            except Exception as exc:
                line = f"STL {rel.as_posix()!r} (id={sid}): {exc}"
                with _job_fields_lock:
                    job.skipped_downloads.append(line)
                _append_log(job, f"SKIP: {line}\n")
                if out.exists():
                    try:
                        out.unlink()
                    except OSError:
                        pass

        with _job_fields_lock:
            if job.cancel_requested:
                _finalize_cancelled(job, tmp_root, "\nCancelled before ZIP.\n")
                return

        if ok_count == 0:
            with _job_fields_lock:
                lines = list(job.skipped_downloads)
            detail = "Every file download failed (nothing to zip). See log for each error.\n" + "\n".join(
                lines
            )
            with _job_fields_lock:
                job.status = JobStatus.FAILED
                job.message = "All selected files failed to download."
            _set_phase(job, "Failed.")
            _append_log(job, f"\n{detail}\n")
            return

        with _job_fields_lock:
            n_skipped = len(job.skipped_downloads)
        if n_skipped > 0:
            with _job_fields_lock:
                lines = list(job.skipped_downloads)
            detail = (
                f"{n_skipped} file(s) could not be downloaded; incomplete model — ZIP not created.\n"
                + "\n".join(lines)
            )
            with _job_fields_lock:
                job.status = JobStatus.FAILED
                job.message = (
                    f"{n_skipped} file(s) failed to download; incomplete archive not written."
                )
            _set_phase(job, "Failed.")
            _append_log(job, f"\n{detail}\n")
            return

        zip_base = dest_dir / archive_name
        _append_log(job, f"\nCreating ZIP: {zip_base}.zip\n")
        _set_phase(job, "Creating ZIP archive…")
        shutil.make_archive(str(zip_base), "zip", root_dir=str(tmp_path))
        zip_file = dest_dir / f"{archive_name}.zip"
        if not zip_file.is_file():
            raise RuntimeError("Zip file was not created")
        with _job_fields_lock:
            job.zip_path = str(zip_file)
            job.status = JobStatus.SUCCESS
            job.message = "Done"
        _set_phase(job, "Complete.")
        _append_log(job, f"Wrote: {zip_file}\n")
    except Exception as exc:
        with _job_fields_lock:
            cancelled_here = job.cancel_requested
        if cancelled_here:
            _finalize_cancelled(job, tmp_root, "\nCancelled.\n")
            return
        with _job_fields_lock:
            job.status = JobStatus.FAILED
            job.message = str(exc)
        _set_phase(job, "Failed.")
        _append_log(job, f"\nError: {exc}\n")
    finally:
        with _job_fields_lock:
            st = job.status
            if job.finished_at is None:
                job.finished_at = datetime.now(timezone.utc).isoformat()
        if tmp_root and os.path.isdir(tmp_root) and st != JobStatus.CANCELLED:
            try:
                shutil.rmtree(tmp_root, ignore_errors=True)
            except OSError:
                pass


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def _append_log(job: Job, text: str) -> None:
    with _job_fields_lock:
        job.log += text
        if len(job.log) > MAX_LOG_CHARS:
            job.log = job.log[-MAX_LOG_CHARS:]


def _set_phase(job: Job, phase: str) -> None:
    with _job_fields_lock:
        job.phase = phase


def job_public_dict(job: Job) -> dict:
    """Consistent snapshot for API / polling (thread-safe)."""
    with _job_fields_lock:
        st = job.status.value
        cancellable = st in ("pending", "running")
        return {
            "id": job.id,
            "status": st,
            "message": job.message,
            "phase": job.phase,
            "log": job.log,
            "zip_path": job.zip_path,
            "destination_path": job.destination_path,
            "files": [dict(f) for f in job.files],
            "cancellable": cancellable,
            "job_kind": job.job_kind,
            "skipped_downloads": list(job.skipped_downloads),
            "created_at": job.created_at,
            "finished_at": job.finished_at,
        }


def _match_pending_file_index(files: list[dict[str, Any]], to_path: str) -> int | None:
    norm = to_path.replace("\\", "/")
    for i, f in enumerate(files):
        if f.get("status") != "pending":
            continue
        name = (f.get("name") or "").replace("\\", "/")
        if not name:
            continue
        if norm.endswith(name) or norm.rstrip("/").endswith(name.split("/")[-1]):
            return i
    for i, f in enumerate(files):
        if f.get("status") == "pending":
            return i
    return None


def _parse_gdown_progress_line(job: Job, line: str, ctx: dict[str, Any]) -> None:
    """Update job.files from a single gdown log line (best-effort; tqdm uses \\r so % may batch)."""
    raw = line.rstrip("\n\r")
    stripped = raw.strip()
    if not stripped:
        return

    with _job_fields_lock:
        m = RE_PROCESSING_FILE.match(stripped)
        if m:
            name = (m.group(2) or "").strip()
            job.files.append(
                {
                    "id": m.group(1),
                    "name": name,
                    "status": "pending",
                    "percent": 0,
                }
            )
            return

        m = RE_TO_LINE.match(stripped)
        if m:
            to_path = m.group(1).strip()
            ai = ctx.get("active_idx")
            if ai is not None and isinstance(ai, int) and 0 <= ai < len(job.files):
                if job.files[ai].get("status") == "downloading":
                    job.files[ai]["status"] = "complete"
                    job.files[ai]["percent"] = 100
            idx = _match_pending_file_index(job.files, to_path)
            if idx is not None:
                job.files[idx]["status"] = "downloading"
                job.files[idx]["percent"] = 0
                ctx["active_idx"] = idx
            return

        m = RE_SKIPPING.search(stripped)
        if m:
            path = m.group(1).strip()
            idx = _match_pending_file_index(job.files, path)
            if idx is not None:
                job.files[idx]["status"] = "complete"
                job.files[idx]["percent"] = 100
            return

        ai = ctx.get("active_idx")
        if ai is not None and isinstance(ai, int) and 0 <= ai < len(job.files) and "%" in stripped:
            nums = [int(x) for x in RE_PERCENT.findall(stripped)]
            if nums:
                job.files[ai]["percent"] = min(100, max(nums))


def request_cancel(job_id: str) -> tuple[bool, str]:
    """Request cancellation: kill gdown if running. Returns (ok, message)."""
    job = get_job(job_id)
    if not job:
        return False, "Job not found"
    with _job_fields_lock:
        if job.status in (
            JobStatus.SUCCESS,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ):
            return False, "Job already finished"
        job.cancel_requested = True
        proc = job.proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    return True, "Cancellation requested"


def _finalize_cancelled(job: Job, tmp_root: str | None, log_note: str) -> None:
    with _job_fields_lock:
        for f in job.files:
            if f.get("status") in ("pending", "downloading"):
                f["status"] = "cancelled"
                f["percent"] = f.get("percent", 0)
        job.status = JobStatus.CANCELLED
        job.message = "Cancelled"
        job.phase = "Cancelled."
        job.proc = None
    _append_log(job, log_note)
    if tmp_root and os.path.isdir(tmp_root):
        try:
            shutil.rmtree(tmp_root, ignore_errors=True)
        except OSError:
            pass
    with _job_fields_lock:
        job.finished_at = datetime.now(timezone.utc).isoformat()


def _run_pipeline(job: Job) -> None:
    with _pipeline_lock:
        with _job_fields_lock:
            if job.cancel_requested:
                job.status = JobStatus.CANCELLED
                job.message = "Cancelled"
                job.phase = "Cancelled."
                job.finished_at = datetime.now(timezone.utc).isoformat()
                return
            job.status = JobStatus.RUNNING

        _set_phase(job, "Preparing destination folder…")
        tmp_root: str | None = None
        try:
            dest_dir = safe_destination_path(job.destination_path)
            dest_dir.mkdir(parents=True, exist_ok=True)

            archive_name = job.archive_base or f"archive_{job.id[:8]}"
            archive_name = sanitize_segment(archive_name, max_len=200)
            if archive_name.endswith(".zip"):
                archive_name = archive_name[:-4]

            tmp_root = tempfile.mkdtemp(prefix="gdown_")
            tmp_path = Path(tmp_root)

            _append_log(job, f"Temp download directory: {tmp_path}\n")
            _append_log(job, "Starting gdown (Google Drive folder download)…\n")

            _set_phase(
                job,
                "Downloading from Google Drive — file list fills in as gdown discovers items; "
                "percent updates when gdown prints progress (tqdm may batch until a newline).",
            )

            cmd = [
                sys.executable,
                "-u",
                "-m",
                "gdown",
                "--folder",
                job.drive_url,
                "-O",
                str(tmp_path),
                "--remaining-ok",
            ]
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            if proc.stdout is None:
                raise RuntimeError("gdown subprocess has no stdout pipe")

            ctx: dict[str, Any] = {"active_idx": None}
            with _job_fields_lock:
                job.proc = proc

            cancelled = False
            try:
                for line in iter(proc.stdout.readline, ""):
                    _append_log(job, line)
                    _parse_gdown_progress_line(job, line, ctx)
                    with _job_fields_lock:
                        if job.cancel_requested:
                            cancelled = True
                            break
                try:
                    proc.wait(timeout=86_400)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    raise RuntimeError("Download exceeded 24 hour limit") from None
            finally:
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        pass
                try:
                    proc.stdout.close()
                except OSError:
                    pass
                with _job_fields_lock:
                    job.proc = None

            if cancelled:
                _finalize_cancelled(
                    job,
                    tmp_root,
                    "\nCancelled — stopped gdown and removed temp download folder.\n",
                )
                return

            if proc.returncode != 0:
                raise RuntimeError(f"gdown exited with code {proc.returncode}")

            ai = ctx.get("active_idx")
            with _job_fields_lock:
                if isinstance(ai, int) and 0 <= ai < len(job.files):
                    if job.files[ai].get("status") == "downloading":
                        job.files[ai]["status"] = "complete"
                        job.files[ai]["percent"] = 100

            _set_phase(job, "Verifying downloaded files…")
            entries = list(tmp_path.iterdir())
            if not entries:
                raise RuntimeError("Download produced no files")

            with _job_fields_lock:
                if job.cancel_requested:
                    _finalize_cancelled(
                        job,
                        tmp_root,
                        "\nCancelled before ZIP — removed temp download folder.\n",
                    )
                    return

            zip_base = dest_dir / archive_name
            _append_log(job, f"\nCreating ZIP: {zip_base}.zip\n")
            _set_phase(job, "Creating ZIP archive…")

            with _job_fields_lock:
                if job.cancel_requested:
                    _finalize_cancelled(
                        job,
                        tmp_root,
                        "\nCancelled before ZIP — removed temp download folder.\n",
                    )
                    return

            shutil.make_archive(str(zip_base), "zip", root_dir=str(tmp_path))

            zip_file = dest_dir / f"{archive_name}.zip"
            if not zip_file.is_file():
                raise RuntimeError("Zip file was not created")

            with _job_fields_lock:
                job.zip_path = str(zip_file)
                job.status = JobStatus.SUCCESS
                job.message = "Done"
            _set_phase(job, "Complete.")
            _append_log(job, f"Wrote: {zip_file}\n")
        except Exception as exc:
            with _job_fields_lock:
                cancelled_here = job.cancel_requested
            if cancelled_here:
                _finalize_cancelled(
                    job,
                    tmp_root,
                    "\nCancelled — removed temp download folder.\n",
                )
                return
            with _job_fields_lock:
                job.status = JobStatus.FAILED
                job.message = str(exc)
            _set_phase(job, "Failed.")
            _append_log(job, f"\nError: {exc}\n")
        finally:
            with _job_fields_lock:
                job.proc = None
                st = job.status
                if job.finished_at is None:
                    job.finished_at = datetime.now(timezone.utc).isoformat()
            if tmp_root and os.path.isdir(tmp_root) and st != JobStatus.CANCELLED:
                try:
                    shutil.rmtree(tmp_root, ignore_errors=True)
                except OSError:
                    pass


def start_job_worker(job_id: str) -> None:
    job = _jobs.get(job_id)
    if not job:
        return

    def run() -> None:
        if job.job_kind == "selective":
            _run_selective_pipeline(job)
        else:
            _run_pipeline(job)

    t = threading.Thread(target=run, daemon=True)
    t.start()
