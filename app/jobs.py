import io
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


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


MAX_LOG_CHARS = 32_000


@dataclass
class Job:
    id: str
    status: JobStatus = JobStatus.PENDING
    drive_url: str = ""
    destination_path: str = ""
    archive_base: str = ""
    message: str = ""
    log: str = ""
    zip_path: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None


_jobs: dict[str, Job] = {}
_pipeline_lock = threading.Lock()


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
    )
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def _append_log(job: Job, text: str) -> None:
    job.log += text
    if len(job.log) > MAX_LOG_CHARS:
        job.log = job.log[-MAX_LOG_CHARS:]


def _run_pipeline(job: Job) -> None:
    with _pipeline_lock:
        job.status = JobStatus.RUNNING
        buf = io.StringIO()

        def log_line(line: str) -> None:
            buf.write(line)
            if not line.endswith("\n"):
                buf.write("\n")

        tmp_root = None
        try:
            dest_dir = safe_destination_path(job.destination_path)
            dest_dir.mkdir(parents=True, exist_ok=True)

            archive_name = job.archive_base or f"archive_{job.id[:8]}"
            archive_name = sanitize_segment(archive_name, max_len=200)
            if archive_name.endswith(".zip"):
                archive_name = archive_name[:-4]

            tmp_root = tempfile.mkdtemp(prefix="gdown_")
            tmp_path = Path(tmp_root)

            log_line(f"Downloading folder to temp: {tmp_path}\n")
            job.log = buf.getvalue()

            cmd = [
                sys.executable,
                "-m",
                "gdown",
                "--folder",
                job.drive_url,
                "-O",
                str(tmp_path),
                "--remaining-ok",
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=86_400,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            if proc.stdout:
                log_line(proc.stdout)
            if proc.stderr:
                log_line(proc.stderr)
            job.log = buf.getvalue()

            if proc.returncode != 0:
                raise RuntimeError(f"gdown exited with code {proc.returncode}")

            entries = list(tmp_path.iterdir())
            if not entries:
                raise RuntimeError("Download produced no files")

            zip_base = dest_dir / archive_name
            log_line(f"Creating zip: {zip_base}.zip\n")
            job.log = buf.getvalue()

            shutil.make_archive(str(zip_base), "zip", root_dir=str(tmp_path))

            zip_file = dest_dir / f"{archive_name}.zip"
            if not zip_file.is_file():
                raise RuntimeError("Zip file was not created")

            job.zip_path = str(zip_file)
            job.status = JobStatus.SUCCESS
            job.message = "Done"
            log_line(f"Wrote: {zip_file}\n")
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.message = str(exc)
            log_line(f"Error: {exc}\n")
        finally:
            job.log = buf.getvalue()
            if tmp_root and os.path.isdir(tmp_root):
                try:
                    shutil.rmtree(tmp_root, ignore_errors=True)
                except OSError:
                    pass
            job.finished_at = datetime.now(timezone.utc).isoformat()


def start_job_worker(job_id: str) -> None:
    job = _jobs.get(job_id)
    if not job:
        return

    def run() -> None:
        _run_pipeline(job)

    t = threading.Thread(target=run, daemon=True)
    t.start()
