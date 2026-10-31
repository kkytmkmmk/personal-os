"""Personal OS: local-first notes, tasks, and iPhone capture inbox."""

from __future__ import annotations

import codecs
import base64
import json
import os
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import hashlib
import secrets
import uuid
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from io import BytesIO
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from personal_os.ingest import detect_image_mime, multipart_file, multipart_form_file
from personal_os.llm_ollama import OllamaClient
from personal_os.moneyforward import moneyforward_projection
from personal_os.moneyforward_sync import import_snapshot as import_moneyforward_snapshot, MAX_BYTES as MONEYFORWARD_MAX_BYTES
from personal_os.ocr import extract_text as extract_ocr_text, is_sufficient as ocr_is_sufficient

ROOT = Path(__file__).resolve().parent
APP_ENV = os.environ.get("PERSONAL_OS_ENV", "production").strip().lower()
if APP_ENV not in {"production", "verification"}:
    raise RuntimeError("PERSONAL_OS_ENV must be 'production' or 'verification'")
PRODUCTION_PORT = 8787
VERIFICATION_PORT = 8877
APP_PORT = VERIFICATION_PORT if APP_ENV == "verification" else PRODUCTION_PORT
DEFAULT_DB_PATH = (
    ROOT / "data" / "verification" / "personal_os_verification.db"
    if APP_ENV == "verification"
    else ROOT / "data" / "personal_os.db"
)
DB_PATH = Path(os.environ.get("PERSONAL_OS_DB_PATH", str(DEFAULT_DB_PATH))).resolve()
BACKUP_DIR = Path(os.environ.get("PERSONAL_OS_BACKUP_DIR", str(DB_PATH.parent / "backups"))).resolve()
ATTACHMENT_DIR = Path(os.environ.get("PERSONAL_OS_ATTACHMENT_DIR", str(DB_PATH.parent / "attachments"))).resolve()
MONEYFORWARD_DB_PATH = os.environ.get("PERSONAL_OS_MONEYFORWARD_DB_PATH", "").strip()
# Browser acceptance tests can request a bounded, verification-only response
# delay to capture the real submitting UI. Production never reads this value.
try:
    E2E_CHAT_DELAY_SECONDS = min(2.0, max(0.0, float(os.environ.get("PERSONAL_OS_E2E_CHAT_DELAY_MS", "0")) / 1000.0)) if APP_ENV == "verification" else 0.0
except ValueError:
    E2E_CHAT_DELAY_SECONDS = 0.0
ANALYSIS_THREAD_LOCK = threading.Lock()
ANALYSIS_PREFILTER_LOCK = threading.Lock()
# Kept as a single-scope compatibility signal for isolated test/runtime
# callers; the set below correctly tracks multiple configured providers.
ANALYSIS_PREFILTER_SCOPE: tuple[str, str, str] | None = None
ANALYSIS_PREFILTER_SCOPES: set[tuple[str, str, str]] = set()
RUNTIME_INSTANCE_ID = uuid.uuid4().hex
RUNTIME_LEASE_SECONDS = 75
RUNTIME_LEASED = False
SERVER: ThreadingHTTPServer | None = None
AUTH_SESSIONS: dict[str, dict[str, object]] = {}
AUTH_SESSIONS_LOCK = threading.Lock()
AUTH_SESSION_SECONDS = 60 * 60 * 12
OLLAMA_START_LOCK = threading.Lock()
OLLAMA_LAST_START = 0.0

DEFAULT_MEMORY_CATEGORIES = [
    ("finance", "資産", "💰", "資産・投資・収入・支出"),
    ("travel", "旅行", "✈️", "訪問地・ホテル・交通・旅行の好み"),
    ("housing", "住居", "🏠", "現在の住居・希望条件・候補"),
    ("relationship", "人間関係", "👥", "明示Evidenceのある人物Factは自動確定し、推測はFact化しない"),
    ("work", "仕事", "💼", "仕事・学習・キャリア"),
    ("health", "健康", "🩺", "体調・運動・睡眠"),
    ("life", "生活（旧分類）", "🌱", "既存データとの互換カテゴリ"),
    ("lifestyle", "生活", "🌿", "日常・習慣・家事"),
    ("learning", "学習", "📚", "学習テーマ・スキル"),
    ("hobby", "趣味", "🎨", "趣味・娯楽・スポーツ"),
    ("food", "食事", "🍽️", "好きな食事・店・食の記録"),
    ("shopping", "買い物", "🛒", "購入検討・保有品"),
    ("technology", "技術", "💻", "技術・開発の記録"),
    ("reference", "参考情報", "📎", "一般知識・調査メモ（個人の現在情報には使わない）"),
    ("other", "その他", "🗂️", "分類未確定の情報"),
]

TRANSACTION_KINDS = {
    "buy", "sell", "deposit", "withdrawal", "investment", "dividend",
    "interest", "fee", "transfer", "repayment",
}
TRANSACTION_STATES = {"auto_confirmed", "pending", "excluded", "confirmed"}
TRANSACTION_CONFIDENCE_THRESHOLD = 0.90
AUTO_CONFIRM_CONFIDENCE_THRESHOLD = 0.75
MEMORY_QUALITY_VERSION = "memory-quality-v2"
ENTITY_TYPES = {
    "person", "organization", "place", "product", "service", "fictional_character",
    "media_character", "ai_character", "AI_character", "brand", "project", "work", "asset", "unknown",
}
RETRIEVAL_ELIGIBILITY = {"eligible", "pending", "excluded", "conflict"}
PERSONAL_RELEVANCE = {"personal", "linked_context", "archive_only", "unknown"}
CHUNK_VERSION = "conversation-turn-v3"
BACKEND_VERSION = os.environ.get("PERSONAL_OS_BACKEND_VERSION", "2026.07.26-reliability-1")
LLM_TRACE_EVENTS: list[dict[str, object]] = []
LLM_TRACE_LOCK = threading.Lock()


def record_llm_trace(stage: str, *, provider: str = "", model: str = "", request_id: str = "",
                     error_type: str | None = None, duration_ms: float | None = None) -> None:
    """Keep a small, non-content trace for operational diagnostics.

    Raw prompts, responses, facts and personal text are intentionally never stored.
    """
    event = {"stage": stage, "provider": provider, "model": model,
             "request_id": request_id, "at": now()}
    if error_type:
        event["error_type"] = error_type
    if duration_ms is not None:
        event["duration_ms"] = round(float(duration_ms), 1)
    with LLM_TRACE_LOCK:
        LLM_TRACE_EVENTS.append(event)
        del LLM_TRACE_EVENTS[:-100]


def normalize_category_slug(value: object) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
    return slug[:40] or "other"


def seed_memory_categories(connection: sqlite3.Connection) -> None:
    for position, (slug, label, icon, description) in enumerate(DEFAULT_MEMORY_CATEGORIES, start=1):
        connection.execute(
            """INSERT OR IGNORE INTO memory_categories(slug,label,icon,description,sort_order,active,created_at,updated_at)
               VALUES(?,?,?,?,?,1,?,?)""",
            (slug, label, icon, description, position, now(), now()),
        )
        connection.execute(
            """UPDATE memory_categories SET label=?,icon=?,description=?,updated_at=?
               WHERE slug=? AND (label='' OR label=slug)""",
            (label, icon, description, now(), slug),
        )


def ensure_memory_category(connection: sqlite3.Connection, slug: object, label: str | None = None) -> str:
    normalized = normalize_category_slug(slug)
    connection.execute(
        """INSERT OR IGNORE INTO memory_categories(slug,label,icon,description,sort_order,active,created_at,updated_at)
           VALUES(?,?,?,?,?,1,?,?)""",
        (normalized, (label or normalized)[:80], "🏷️", "ユーザーまたは抽出結果から追加", 999, now(), now()),
    )
    return normalized


def reclassify_generic_reference_facts(connection: sqlite3.Connection) -> int:
    """Keep imported technical explanations as reference, not personal memory."""
    markers = ("GKE", "Kubernetes", "RFC ", "CSV", "Vue", "axios", "fetch", "S3", "API", "Python", "JavaScript", "SQL")
    rows = connection.execute(
        "SELECT id,summary,value_json FROM facts WHERE category IN ('other','technology') AND fact_type='note'"
    ).fetchall()
    changed = 0
    for row in rows:
        try:
            value = json.loads(row["value_json"] or "{}")
        except json.JSONDecodeError:
            value = {}
        if value.get("asset") or value.get("amount") is not None or not any(marker.lower() in row["summary"].lower() for marker in markers):
            continue
        connection.execute(
            "UPDATE facts SET category='reference', fact_key=? WHERE id=?",
            (canonical_fact_key("reference", "note", None, value.get("details"), row["summary"]), row["id"]),
        )
        changed += 1
    return changed


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def export_timestamp(value: object) -> str:
    """Keep the original ChatGPT conversation date when it is available."""
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return now()


@contextmanager
def db():
    """Yield a short-lived SQLite connection and always release its handle."""
    DB_PATH.parent.mkdir(exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def runtime_status() -> dict[str, object]:
    with db() as connection:
        row = connection.execute("SELECT * FROM runtime_leases WHERE lease_name='personal-os'").fetchone()
    return dict(row) if row else {"running": False}


def process_is_running(pid: int) -> bool:
    """Return whether a recorded local PID still exists without shelling out.

    An abrupt process kill cannot run the normal lease cleanup.  Windows does
    not reliably implement ``os.kill(pid, 0)`` (it can raise ``SystemError``
    for an otherwise ordinary stale PID), so use its process-query API there.
    Permission failures are treated as running so we never steal a live
    process owned by another user.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: conservatively live
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_runtime_lease(port: int) -> tuple[bool, dict[str, object] | None]:
    """Allow one healthy Personal OS process for this database at a time."""
    global RUNTIME_LEASED
    timestamp = now()
    with db() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS runtime_leases (
                   lease_name TEXT PRIMARY KEY, instance_id TEXT NOT NULL, pid INTEGER NOT NULL,
                   port INTEGER NOT NULL, started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL
               )"""
        )
        previous = connection.execute("SELECT * FROM runtime_leases WHERE lease_name='personal-os'").fetchone()
        if previous and previous["instance_id"] != RUNTIME_INSTANCE_ID:
            try:
                stale = datetime.now(timezone.utc).astimezone() - datetime.fromisoformat(previous["heartbeat_at"])
            except ValueError:
                stale = timedelta.max
            owner_is_alive = process_is_running(int(previous["pid"]))
            if owner_is_alive and stale < timedelta(seconds=RUNTIME_LEASE_SECONDS):
                return False, dict(previous)
        connection.execute(
            """INSERT INTO runtime_leases(lease_name,instance_id,pid,port,started_at,heartbeat_at)
               VALUES('personal-os',?,?,?,?,?)
               ON CONFLICT(lease_name) DO UPDATE SET instance_id=excluded.instance_id,pid=excluded.pid,
                 port=excluded.port,started_at=excluded.started_at,heartbeat_at=excluded.heartbeat_at""",
            (RUNTIME_INSTANCE_ID, os.getpid(), port, timestamp, timestamp),
        )
    RUNTIME_LEASED = True
    return True, None


def recover_interrupted_analysis() -> int:
    """A lease owner can safely recover work abandoned by a prior process."""
    if not RUNTIME_LEASED:
        return 0
    with db() as connection:
        connection.execute("DELETE FROM analysis_locks WHERE lock_name='gemini-import-analysis'")
        cursor = connection.execute(
            """UPDATE analysis_jobs SET status='pending', error='interrupted process recovered',
               started_at=NULL, finished_at=NULL, updated_at=? WHERE status='running'""",
            (now(),),
        )
    return cursor.rowcount


def refresh_runtime_lease() -> None:
    if not RUNTIME_LEASED:
        return
    with db() as connection:
        connection.execute(
            "UPDATE runtime_leases SET heartbeat_at=? WHERE lease_name='personal-os' AND instance_id=?",
            (now(), RUNTIME_INSTANCE_ID),
        )


def release_runtime_lease() -> None:
    global RUNTIME_LEASED
    if not RUNTIME_LEASED:
        return
    with db() as connection:
        connection.execute(
            "DELETE FROM runtime_leases WHERE lease_name='personal-os' AND instance_id=?", (RUNTIME_INSTANCE_ID,)
        )
    RUNTIME_LEASED = False


def runtime_heartbeat_loop() -> None:
    while RUNTIME_LEASED:
        try:
            refresh_runtime_lease()
        except sqlite3.Error as error:
            print(f"[runtime] {error}")
        time.sleep(15)


def request_server_shutdown() -> None:
    """Stop serve_forever outside the request thread and release the DB lease."""
    def shutdown() -> None:
        time.sleep(0.15)
        release_runtime_lease()
        if SERVER:
            SERVER.shutdown()
    threading.Thread(target=shutdown, name="personal-os-shutdown", daemon=True).start()


def record_schema_migrations(connection: sqlite3.Connection) -> None:
    """Keep an auditable baseline for idempotent SQLite migrations.

    Earlier releases used safe `CREATE` / `ALTER` checks directly.  These
    markers preserve that compatibility while making subsequent migrations
    explicit and reviewable.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version TEXT PRIMARY KEY,
               applied_at TEXT NOT NULL
           )"""
    )
    for version in ("001_legacy_baseline", "002_facts_canonical", "003_analysis_jobs_and_decisions", "004_runtime_lease", "005_memory_categories", "006_reference_category_cleanup", "007_screenshot_attachments", "008_evidence_recommendation_planning", "009_analysis_queue_performance", "010_requirements_cycle", "011_memory_correctness"):
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
            (version, now()),
        )


def setting(key: str, default: str = "") -> str:
    with db() as connection:
        row = connection.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def save_setting(key: str, value: str) -> None:
    with db() as connection:
        connection.execute(
            """INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (key, value, now()),
        )


def analysis_paused() -> bool:
    """Whether background local/remote import analysis should remain idle."""
    return setting("analysis_paused", "false").lower() == "true"


def setting_int(key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(setting(key, str(default)))))
    except ValueError:
        return default


def analysis_batch_size() -> int:
    """Number of sequential jobs handled per queue/lock acquisition."""
    try:
        default = int(os.environ.get("PERSONAL_OS_ANALYSIS_BATCH_SIZE", "100"))
    except ValueError:
        default = 100
    return setting_int("analysis_batch_size", default, 1, 200)


BACKUP_FORMAT = "personal-os-backup-v1"
BACKUP_SUFFIX = ".posbackup"
BACKUP_INTERVAL_HOURS = 24
DECISION_STATES = {"candidate", "considered", "decided", "executed", "result"}
BACKUP_INTERVAL_SECONDS = BACKUP_INTERVAL_HOURS * 60 * 60


def _path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _backup_display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _create_backup_bundle(prefix: str = "personal_os") -> Path:
    """Create an atomic generation containing SQLite, attachments and a manifest."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    token = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_prefix = re.sub(r"[^a-zA-Z0-9_.-]+", "-", prefix).strip("-") or "personal_os"
    target = BACKUP_DIR / f"{safe_prefix}-{token}{BACKUP_SUFFIX}"
    temporary_bundle = BACKUP_DIR / f".{target.name}.{uuid.uuid4().hex}.tmp"
    temporary_db = BACKUP_DIR / f".{safe_prefix}-{uuid.uuid4().hex}.sqlite.tmp"
    files: list[dict[str, object]] = []
    try:
        source = sqlite3.connect(DB_PATH)
        destination = sqlite3.connect(temporary_db)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        database_item = {
            "path": "database.sqlite3",
            "size": temporary_db.stat().st_size,
            "sha256": _path_sha256(temporary_db),
            "kind": "database",
        }
        files.append(database_item)
        attachment_paths = (
            sorted(path for path in ATTACHMENT_DIR.rglob("*") if path.is_file())
            if ATTACHMENT_DIR.exists()
            else []
        )
        for attachment in attachment_paths:
            relative = attachment.relative_to(ATTACHMENT_DIR).as_posix()
            files.append({
                "path": f"attachments/{relative}",
                "size": attachment.stat().st_size,
                "sha256": _path_sha256(attachment),
                "kind": "attachment",
            })
        manifest_connection = sqlite3.connect(temporary_db)
        try:
            has_migrations = manifest_connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            migrations = [
                row[0] for row in manifest_connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ] if has_migrations else []
        finally:
            manifest_connection.close()
        manifest = {
            "format": BACKUP_FORMAT,
            "created_at": now(),
            "database_name": DB_PATH.name,
            "schema_migrations": migrations,
            "files": files,
        }
        with zipfile.ZipFile(temporary_bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(temporary_db, "database.sqlite3")
            for attachment, item in zip(attachment_paths, files[1:]):
                archive.write(attachment, str(item["path"]))
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        os.replace(temporary_bundle, target)
        return target
    finally:
        temporary_db.unlink(missing_ok=True)
        temporary_bundle.unlink(missing_ok=True)


def backup_before_migration(version: str) -> Path | None:
    """Create a complete backup only when a migration is pending."""
    if not DB_PATH.exists():
        return None
    connection = None
    try:
        connection = sqlite3.connect(DB_PATH)
        has_migration_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if has_migration_table and connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
        ).fetchone():
            return None
    except sqlite3.Error:
        # An existing pre-migration database may predate the migration table.
        pass
    finally:
        if connection is not None:
            connection.close()
    safe_version = re.sub(r"[^a-zA-Z0-9_.-]+", "-", version).strip("-") or "migration"
    return _create_backup_bundle(f"personal_os-pre-{safe_version}")


def backup_database(force: bool = False) -> Path | None:
    """Create a complete local backup and retain every generation."""
    previous = setting("last_backup_at")
    if not force and previous:
        try:
            elapsed = datetime.now(timezone.utc).astimezone() - datetime.fromisoformat(previous)
            if elapsed < timedelta(hours=BACKUP_INTERVAL_HOURS):
                return None
        except ValueError:
            pass
    target = _create_backup_bundle("personal_os")
    save_setting("last_backup_at", now())
    save_setting("last_backup_path", _backup_display_path(target))
    return target


def backup_wait_seconds(reference: datetime | None = None) -> int:
    """Return the delay until the next due daily backup.

    A fresh install waits one full day; startup itself never forces a backup.
    If the previous generation is already older than a day, the worker runs
    immediately because the backup is due.
    """
    previous = setting("last_backup_at")
    if not previous:
        return BACKUP_INTERVAL_SECONDS
    try:
        current = reference or datetime.now(timezone.utc).astimezone()
        elapsed = current - datetime.fromisoformat(previous)
        return max(0, int(BACKUP_INTERVAL_SECONDS - elapsed.total_seconds()))
    except (TypeError, ValueError):
        return BACKUP_INTERVAL_SECONDS


def backup_status() -> dict[str, object]:
    backups = sorted(
        [
            path for path in BACKUP_DIR.glob("personal_os-*")
            if path.is_file() and path.suffix.lower() in {".db", BACKUP_SUFFIX}
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if BACKUP_DIR.exists() else []
    return {
        "last_backup_at": setting("last_backup_at"),
        "last_backup_path": setting("last_backup_path"),
        "backup_count": len(backups),
        "interval_hours": BACKUP_INTERVAL_HOURS,
        "retention_count": None,
    }


def _resolve_backup_path(relative_path: str) -> Path:
    supplied = Path(relative_path)
    candidate = supplied.resolve() if supplied.is_absolute() else (ROOT / supplied).resolve()
    backup_root = BACKUP_DIR.resolve()
    if backup_root != candidate.parent and backup_root not in candidate.parents:
        raise ValueError("Backup path must be under the configured backup directory")
    if candidate.suffix.lower() not in {".db", BACKUP_SUFFIX}:
        raise ValueError("Backup path must point to a .posbackup or legacy .db generation")
    if not candidate.exists():
        raise FileNotFoundError(str(candidate))
    return candidate


def _safe_archive_name(name: str) -> bool:
    path = Path(name.replace("\\", "/"))
    return not path.is_absolute() and ".." not in path.parts


def verify_backup(relative_path: str) -> dict[str, object]:
    """Verify database integrity plus every file checksum in a backup generation."""
    candidate = _resolve_backup_path(relative_path)
    if candidate.suffix.lower() == ".db":
        connection = sqlite3.connect(candidate)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            tables = connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        finally:
            connection.close()
        return {
            "path": _backup_display_path(candidate),
            "format": "legacy-sqlite",
            "integrity": result,
            "tables": tables,
            "attachments": 0,
            "valid": result == "ok",
        }
    with zipfile.ZipFile(candidate) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or not all(_safe_archive_name(name) for name in names):
            raise ValueError("Backup contains unsafe or duplicate archive paths")
        if "manifest.json" not in names or "database.sqlite3" not in names:
            raise ValueError("Backup manifest or database is missing")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        if manifest.get("format") != BACKUP_FORMAT or not isinstance(manifest.get("files"), list):
            raise ValueError("Unsupported backup format")
        expected_names = {"manifest.json"}
        for item in manifest["files"]:
            archive_name = str(item.get("path", ""))
            if not _safe_archive_name(archive_name) or archive_name not in names:
                raise ValueError(f"Backup file is missing: {archive_name}")
            expected_names.add(archive_name)
            digest = hashlib.sha256()
            size = 0
            with archive.open(archive_name) as stream:
                while block := stream.read(1024 * 1024):
                    size += len(block)
                    digest.update(block)
            if size != int(item.get("size", -1)) or digest.hexdigest() != item.get("sha256"):
                raise ValueError(f"Backup checksum mismatch: {archive_name}")
        unexpected = set(names) - expected_names
        if unexpected:
            raise ValueError(f"Backup contains untracked files: {sorted(unexpected)[:3]}")
        with tempfile.TemporaryDirectory(prefix="personal-os-verify-") as directory:
            database_copy = Path(directory) / "database.sqlite3"
            with archive.open("database.sqlite3") as source, database_copy.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            connection = sqlite3.connect(database_copy)
            try:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
                tables = connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            finally:
                connection.close()
    return {
        "path": _backup_display_path(candidate),
        "format": BACKUP_FORMAT,
        "integrity": result,
        "tables": tables,
        "attachments": sum(1 for item in manifest["files"] if item.get("kind") == "attachment"),
        "valid": result == "ok",
        "created_at": manifest.get("created_at"),
        "schema_migrations": manifest.get("schema_migrations", []),
    }
    with sqlite3.connect(candidate) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    return {"path": str(candidate.relative_to(ROOT)), "integrity": result, "tables": tables, "valid": result == "ok"}


def restore_database(relative_path: str) -> dict[str, object]:
    """Restore a verified generation after creating a fresh pre-restore backup."""
    verification = verify_backup(relative_path)
    if not verification["valid"]:
        raise ValueError("Backup integrity check failed")
    source = _resolve_backup_path(relative_path)
    backup_database(force=True)
    temporary_root = DB_PATH.parent / f".restore-{uuid.uuid4().hex}"
    temporary_root.mkdir(parents=True, exist_ok=False)
    temporary_db = temporary_root / "database.sqlite3"
    temporary_attachments = temporary_root / "attachments"
    previous_attachments = DB_PATH.parent / f".attachments-pre-restore-{uuid.uuid4().hex}"
    attachment_swapped = False
    try:
        if source.suffix.lower() == ".db":
            with sqlite3.connect(source) as source_connection, sqlite3.connect(temporary_db) as destination:
                source_connection.backup(destination)
        else:
            with zipfile.ZipFile(source) as archive:
                with archive.open("database.sqlite3") as input_stream, temporary_db.open("wb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream)
                for name in archive.namelist():
                    if not name.startswith("attachments/") or name.endswith("/"):
                        continue
                    relative = Path(name).relative_to("attachments")
                    destination = temporary_attachments / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as input_stream, destination.open("wb") as output_stream:
                        shutil.copyfileobj(input_stream, output_stream)
            if ATTACHMENT_DIR.exists():
                os.replace(ATTACHMENT_DIR, previous_attachments)
            temporary_attachments.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_attachments, ATTACHMENT_DIR)
            attachment_swapped = True
        os.replace(temporary_db, DB_PATH)
        if previous_attachments.exists():
            shutil.rmtree(previous_attachments)
    except Exception:
        if attachment_swapped and previous_attachments.exists():
            if ATTACHMENT_DIR.exists():
                shutil.rmtree(ATTACHMENT_DIR)
            os.replace(previous_attachments, ATTACHMENT_DIR)
        raise
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)
    return verification | {"restored": True}


def _json_ids_without(raw: str, removed: set[int]) -> str:
    try:
        values = json.loads(raw or "[]")
    except json.JSONDecodeError:
        values = []
    if not isinstance(values, list):
        values = []
    return json.dumps([value for value in values if value not in removed], ensure_ascii=False)


def privacy_delete_preview(target_type: str, target_id: int, delete_raw: bool = False) -> dict[str, object]:
    """Describe a deletion scope before the destructive confirmed request."""
    if target_type not in {"fact", "attachment", "entry", "entity"}:
        raise ValueError("target_type must be fact, attachment, entry, or entity")
    with db() as connection:
        if target_type == "fact":
            fact_ids = [target_id]
        elif target_type == "attachment":
            fact_ids = [row["id"] for row in connection.execute("SELECT id FROM facts WHERE source_attachment_id=?", (target_id,))]
        elif target_type == "entry":
            fact_ids = [row["id"] for row in connection.execute("SELECT id FROM facts WHERE document_id IN (SELECT id FROM documents WHERE legacy_entry_id=?)", (target_id,))]
        else:
            fact_ids = [row["id"] for row in connection.execute("SELECT id FROM facts WHERE subject_entity_id=?", (target_id,))]
        attachment_entry_ids = [
            int(row["entry_id"]) for row in connection.execute(
                "SELECT entry_id FROM attachments WHERE id=?", (target_id,)
            )
        ] if target_type == "attachment" else []
        entry_ids = [target_id] if target_type == "entry" else (
            attachment_entry_ids if target_type == "attachment" and delete_raw else []
        )
        attachment_filter = []
        attachment_parameters: list[object] = []
        if target_type == "attachment":
            attachment_filter.append("id=?")
            attachment_parameters.append(target_id)
        if entry_ids:
            marks = ",".join("?" for _ in entry_ids)
            attachment_filter.append(f"entry_id IN ({marks})")
            attachment_parameters.extend(entry_ids)
        attachments = [
            dict(row) for row in connection.execute(
                f"SELECT id,entry_id,storage_path,original_name,content_hash FROM attachments WHERE {' OR '.join(attachment_filter)}",
                attachment_parameters,
            )
        ] if attachment_filter else []
        document_ids = []
        chunk_ids = []
        if entry_ids:
            marks = ",".join("?" for _ in entry_ids)
            document_ids = [int(row["id"]) for row in connection.execute(
                f"SELECT id FROM documents WHERE legacy_entry_id IN ({marks})", entry_ids
            )]
            if document_ids:
                document_marks = ",".join("?" for _ in document_ids)
                chunk_ids = [int(row["id"]) for row in connection.execute(
                    f"SELECT id FROM chunks WHERE document_id IN ({document_marks})", document_ids
                )]
    return {
        "target_type": target_type,
        "target_id": target_id,
        "facts": fact_ids,
        "fact_count": len(fact_ids),
        "entries": entry_ids,
        "documents": document_ids,
        "chunks": chunk_ids,
        "attachments": attachments,
        "raw_deleted": bool(entry_ids or target_type == "attachment"),
        "warning": "原文を含む削除です" if entry_ids or target_type == "attachment" else "派生情報だけを削除し、原文は保持します",
    }


def _safe_attachment_file(storage_path: str) -> Path | None:
    attachment_root = ATTACHMENT_DIR.resolve()
    candidates = [(ROOT / storage_path).resolve(), (ATTACHMENT_DIR / Path(storage_path).name).resolve()]
    for candidate in candidates:
        if candidate == attachment_root or attachment_root in candidate.parents:
            return candidate
    return None


def delete_private_data(target_type: str, target_id: int, delete_raw: bool = False) -> dict[str, object]:
    """Delete the previewed scope, including derived indexes and orphaned files."""
    preview = privacy_delete_preview(target_type, target_id, delete_raw)
    fact_ids = [int(value) for value in preview["facts"]]
    entry_ids = [int(value) for value in preview["entries"]]
    document_ids = [int(value) for value in preview["documents"]]
    chunk_ids = [int(value) for value in preview["chunks"]]
    attachment_rows = list(preview["attachments"])
    with db() as connection:
        connection.execute("BEGIN")
        if fact_ids:
            marks = ",".join("?" for _ in fact_ids)
            for table in ("finance_transaction_candidates", "finance_transactions", "fact_evidence", "fact_reviews", "fact_currentness"):
                connection.execute(f"DELETE FROM {table} WHERE fact_id IN ({marks})", fact_ids)
            connection.execute(f"DELETE FROM entity_mentions WHERE fact_id IN ({marks})", fact_ids)
            connection.execute(f"DELETE FROM memory_corrections WHERE fact_id IN ({marks})", fact_ids)
            connection.execute(f"DELETE FROM memory_changes WHERE fact_id IN ({marks}) OR previous_fact_id IN ({marks})", fact_ids * 2)
            connection.execute(f"DELETE FROM facts WHERE id IN ({marks})", fact_ids)
            removed = set(fact_ids)
            for row in connection.execute("SELECT id,related_fact_ids_json FROM decisions").fetchall():
                updated = _json_ids_without(row["related_fact_ids_json"], removed)
                if updated != row["related_fact_ids_json"]:
                    connection.execute("UPDATE decisions SET related_fact_ids_json=?,updated_at=? WHERE id=?", (updated, now(), row["id"]))
            for row in connection.execute("SELECT id,source_fact_ids_json FROM recommendations").fetchall():
                updated = _json_ids_without(row["source_fact_ids_json"], removed)
                if updated != row["source_fact_ids_json"]:
                    connection.execute("UPDATE recommendations SET source_fact_ids_json=?,updated_at=? WHERE id=?", (updated, now(), row["id"]))
        if target_type == "entity":
            removed_entities = {target_id}
            for row in connection.execute("SELECT id,related_entity_ids_json FROM decisions").fetchall():
                updated = _json_ids_without(row["related_entity_ids_json"], removed_entities)
                if updated != row["related_entity_ids_json"]:
                    connection.execute("UPDATE decisions SET related_entity_ids_json=?,updated_at=? WHERE id=?", (updated, now(), row["id"]))
            connection.execute("DELETE FROM entity_mentions WHERE resolved_entity_id=?", (target_id,))
            connection.execute("DELETE FROM memory_corrections WHERE entity_id=?", (target_id,))
            connection.execute("DELETE FROM entities WHERE id=?", (target_id,))
        attachment_ids = [int(row["id"]) for row in attachment_rows]
        if attachment_ids:
            marks = ",".join("?" for _ in attachment_ids)
            connection.execute(f"DELETE FROM analysis_jobs WHERE source_attachment_id IN ({marks})", attachment_ids)
            connection.execute(f"DELETE FROM attachment_derivatives WHERE attachment_id IN ({marks})", attachment_ids)
            connection.execute(f"DELETE FROM attachments WHERE id IN ({marks})", attachment_ids)
        if chunk_ids:
            marks = ",".join("?" for _ in chunk_ids)
            connection.execute(f"DELETE FROM embeddings WHERE chunk_id IN ({marks})", chunk_ids)
            connection.execute(f"DELETE FROM embedding_jobs WHERE chunk_id IN ({marks})", chunk_ids)
            connection.execute(f"DELETE FROM entity_mentions WHERE chunk_id IN ({marks})", chunk_ids)
            try:
                connection.execute(f"DELETE FROM chunk_fts WHERE chunk_id IN ({marks})", chunk_ids)
            except sqlite3.OperationalError:
                pass
            connection.execute(f"DELETE FROM chunks WHERE id IN ({marks})", chunk_ids)
        if document_ids:
            marks = ",".join("?" for _ in document_ids)
            connection.execute(f"DELETE FROM analysis_jobs WHERE document_id IN ({marks})", document_ids)
            connection.execute(f"DELETE FROM documents WHERE id IN ({marks})", document_ids)
        if entry_ids:
            marks = ",".join("?" for _ in entry_ids)
            for table in ("analysis_status", "memory_proposals", "task_plans"):
                connection.execute(f"DELETE FROM {table} WHERE entry_id IN ({marks})", entry_ids)
            connection.execute(f"DELETE FROM entries WHERE id IN ({marks})", entry_ids)
        connection.commit()
        remaining_paths = {
            str(row["storage_path"]) for row in connection.execute("SELECT storage_path FROM attachments")
        }
    files_deleted = 0
    for attachment in attachment_rows:
        if attachment["storage_path"] in remaining_paths:
            continue
        path = _safe_attachment_file(str(attachment["storage_path"]))
        if path and path.exists() and path.is_file():
            path.unlink()
            files_deleted += 1
    return {
        "target_type": target_type,
        "target_id": target_id,
        "facts_deleted": len(fact_ids),
        "entries_deleted": len(entry_ids),
        "attachments_deleted": len(attachment_rows),
        "files_deleted": files_deleted,
        "raw_deleted": bool(preview["raw_deleted"]),
    }


def correct_fact(fact_id: int, payload: dict[str, object]) -> dict[str, object]:
    """Apply a user correction while retaining an auditable before/after record."""
    allowed = {"summary", "value", "amount", "currency", "asset", "details", "fact_type",
               "occurred_on", "valid_from", "valid_to", "entity_name", "entity_type"}
    if not any(key in payload for key in allowed):
        raise ValueError("No correctable Fact fields supplied")
    with db() as connection:
        row = connection.execute(
            """SELECT f.*,e.canonical_name AS entity_name,e.entity_type
               FROM facts f LEFT JOIN entities e ON e.id=f.subject_entity_id WHERE f.id=?""",
            (fact_id,),
        ).fetchone()
        if not row:
            raise ValueError("Fact not found")
        if canonical_domain(str(row["category"] or "other")) in {"finance", "relationship", "health"}:
            supplied_summary = str(payload.get("summary", "")).strip()
            if supplied_summary == "機微情報の確認が必要です":
                raise ValueError("Masked review content cannot be used to correct a sensitive Fact")
        value = _fact_value(row)
        replacement_value = payload.get("value")
        if replacement_value is not None:
            if not isinstance(replacement_value, dict):
                raise ValueError("value must be an object")
            value = dict(replacement_value)
        for key in ("amount", "currency", "asset", "details"):
            if key in payload:
                value[key] = payload[key]
        if value.get("details") is not None and not isinstance(value.get("details"), dict):
            raise ValueError("details must be an object")
        summary = str(payload.get("summary", row["summary"])).strip()[:500]
        if not summary:
            raise ValueError("summary cannot be empty")
        fact_type = str(payload.get("fact_type", row["fact_type"])).strip()[:80]
        if not fact_type:
            raise ValueError("fact_type cannot be empty")
        entity_id = row["subject_entity_id"]
        entity_name = str(payload.get("entity_name", row["entity_name"] or value.get("asset") or "")).strip()[:160]
        entity_type = str(payload.get("entity_type", row["entity_type"] or row["resolved_entity_type"] or "unknown")).strip()
        if entity_type not in ENTITY_TYPES and entity_type != "subject":
            raise ValueError("Invalid entity_type")
        if entity_name:
            connection.execute(
                "INSERT OR IGNORE INTO entities(entity_type,canonical_name,created_at,updated_at) VALUES(?,?,?,?)",
                (entity_type, entity_name, now(), now()),
            )
            entity_id = connection.execute(
                "SELECT id FROM entities WHERE entity_type=? AND canonical_name=?",
                (entity_type, entity_name),
            ).fetchone()["id"]
            value["asset"] = entity_name
        fields = {
            "summary": summary,
            "value_json": json.dumps(value, ensure_ascii=False, sort_keys=True),
            "fact_type": fact_type,
            "occurred_on": str(payload.get("occurred_on", row["occurred_on"]) or "")[:32] or None,
            "valid_from": str(payload.get("valid_from", row["valid_from"]) or "")[:32] or None,
            "valid_to": str(payload.get("valid_to", row["valid_to"]) or "")[:32] or None,
            "subject_entity_id": entity_id,
        }
        fields["fact_key"] = canonical_fact_key(
            row["category"], fact_type, value.get("asset"), value.get("details"), summary
        )
        before = {
            "summary": row["summary"], "value_json": row["value_json"], "fact_type": row["fact_type"],
            "occurred_on": row["occurred_on"], "valid_from": row["valid_from"], "valid_to": row["valid_to"],
            "fact_key": row["fact_key"], "subject_entity_id": row["subject_entity_id"],
        }
        connection.execute(
            """UPDATE facts SET summary=?,value_json=?,fact_type=?,occurred_on=?,valid_from=?,valid_to=?,
                      subject_entity_id=?,fact_key=?,validation_status='confirmed',validation_reason=?,
                      validated_at=? WHERE id=?""",
            (fields["summary"], fields["value_json"], fields["fact_type"], fields["occurred_on"],
             fields["valid_from"], fields["valid_to"], fields["subject_entity_id"], fields["fact_key"],
             "ユーザー訂正済み", now(), fact_id),
        )
        connection.execute(
            """INSERT INTO fact_reviews(fact_id,state,reason,review_note,reviewed_at,created_at)
               VALUES(?,'confirmed','ユーザー訂正済み','ユーザー訂正',?,?)
               ON CONFLICT(fact_id) DO UPDATE SET state='confirmed',reason='ユーザー訂正済み',
                 review_note='ユーザー訂正',reviewed_at=excluded.reviewed_at""",
            (fact_id, now(), now()),
        )
        _record_memory_correction(
            connection, fact_id=fact_id, entity_id=entity_id, correction_type="user_fact_correction",
            before=before, after=fields, reason=str(payload.get("reason", "ユーザーによる訂正"))[:1000],
            source="user",
        )
        apply_fact_timeline(connection, fact_id)
        quality = apply_memory_quality_to_fact(connection, fact_id, source="user")
        if row["category"] == "finance":
            sync_finance_transaction(connection, fact_id, confirmed=True)
    return {"fact_id": fact_id, "quality": quality, "corrected": fields}


def initialize() -> None:
    global ANALYSIS_PREFILTER_SCOPE
    queue_analysis_on_start = os.environ.get(
        "PERSONAL_OS_QUEUE_ANALYSIS_ON_START", "true"
    ).strip().lower() != "false"
    run_startup_maintenance = os.environ.get(
        "PERSONAL_OS_RUN_STARTUP_MAINTENANCE", "true"
    ).strip().lower() != "false"
    if DB_PATH.is_file():
        # Schema changes are deliberately independent from startup maintenance.
        # A large production database may skip expensive re-audits, but it must
        # never start against an older schema.
        with db() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                     version TEXT PRIMARY KEY,
                     applied_at TEXT NOT NULL
                )"""
            )
            existing_tables = {
                str(row["name"])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        if {"facts", "fact_reviews", "memory_proposals", "decisions"} <= existing_tables:
            migrate_decision_replay()
            migrate_action_center_review_inbox()
            migrate_action_center_review_inbox_stabilization()
        with db() as connection:
            existing_tables = {
                str(row["name"])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            latest_migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?",
                ("015_action_center_review_inbox_stabilization",),
            ).fetchone()
        required_tables = {
            "entries", "documents", "chunks", "facts", "fact_evidence",
            "analysis_jobs", "runtime_leases", "app_settings", "decisions",
            "fact_review_queue_state", "memory_proposal_queue_state",
            "action_center_snoozes", "schema_migrations",
        }
        if not run_startup_maintenance and required_tables <= existing_tables and latest_migration:
            for provider in extraction_providers():
                scope = (provider, provider_model(provider, "extract"), PROMPT_VERSION)
                ANALYSIS_PREFILTER_SCOPES.add(scope)
                ANALYSIS_PREFILTER_SCOPE = scope
            return
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kind TEXT NOT NULL DEFAULT 'note',
              title TEXT NOT NULL,
              body TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT 'manual',
              tags TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'inbox',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status);
            CREATE INDEX IF NOT EXISTS idx_entries_created_at ON entries(created_at DESC);
            CREATE TABLE IF NOT EXISTS attachments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              entry_id INTEGER NOT NULL,
              storage_path TEXT NOT NULL,
              original_name TEXT NOT NULL,
              mime_type TEXT NOT NULL,
              byte_size INTEGER NOT NULL,
              content_hash TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(entry_id) REFERENCES entries(id)
            );
            CREATE INDEX IF NOT EXISTS idx_attachments_entry ON attachments(entry_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS attachment_derivatives (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              attachment_id INTEGER NOT NULL,
              derivative_kind TEXT NOT NULL,
              engine TEXT NOT NULL,
              version TEXT NOT NULL,
              content TEXT NOT NULL DEFAULT '',
              confidence REAL,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              UNIQUE(attachment_id, derivative_kind, engine, version),
              FOREIGN KEY(attachment_id) REFERENCES attachments(id)
            );
            CREATE INDEX IF NOT EXISTS idx_attachment_derivatives_attachment
              ON attachment_derivatives(attachment_id, derivative_kind);
            CREATE TABLE IF NOT EXISTS question_answers (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              domain TEXT NOT NULL,
              question_id TEXT NOT NULL,
              question TEXT NOT NULL,
              answer TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_question_answers_domain ON question_answers(domain);
            CREATE TABLE IF NOT EXISTS memory_categories (
              slug TEXT PRIMARY KEY,
              label TEXT NOT NULL,
              icon TEXT NOT NULL DEFAULT '🏷️',
              description TEXT NOT NULL DEFAULT '',
              sort_order INTEGER NOT NULL DEFAULT 999,
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_categories_active ON memory_categories(active, sort_order);
            CREATE TABLE IF NOT EXISTS checkins (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              mood TEXT NOT NULL,
              energy TEXT NOT NULL,
              focus TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_checkins_created_at ON checkins(created_at DESC);
            CREATE TABLE IF NOT EXISTS task_plans (
              entry_id INTEGER PRIMARY KEY,
              area TEXT NOT NULL,
              urgency TEXT NOT NULL,
              next_action TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(entry_id) REFERENCES entries(id)
            );
            CREATE TABLE IF NOT EXISTS structured_memories (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              entry_id INTEGER NOT NULL,
              category TEXT NOT NULL,
              type TEXT NOT NULL,
              asset TEXT,
              amount REAL,
              currency TEXT,
              occurred_on TEXT,
              summary TEXT NOT NULL,
              details_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(entry_id) REFERENCES entries(id)
            );
            CREATE INDEX IF NOT EXISTS idx_structured_memories_category ON structured_memories(category);
            CREATE INDEX IF NOT EXISTS idx_structured_memories_entry ON structured_memories(entry_id);
            CREATE TABLE IF NOT EXISTS analysis_status (
              entry_id INTEGER PRIMARY KEY,
              analyzer TEXT NOT NULL,
              analyzed_at TEXT NOT NULL,
              FOREIGN KEY(entry_id) REFERENCES entries(id)
            );
            CREATE TABLE IF NOT EXISTS analysis_locks (
              lock_name TEXT PRIMARY KEY,
              acquired_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_leases (
              lease_name TEXT PRIMARY KEY,
              instance_id TEXT NOT NULL,
              pid INTEGER NOT NULL,
              port INTEGER NOT NULL,
              started_at TEXT NOT NULL,
              heartbeat_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              legacy_entry_id INTEGER UNIQUE,
              title TEXT NOT NULL,
              source TEXT NOT NULL,
              source_created_at TEXT NOT NULL,
              ingested_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(legacy_entry_id) REFERENCES entries(id)
            );
            CREATE INDEX IF NOT EXISTS idx_documents_source_created ON documents(source_created_at DESC);
            CREATE TABLE IF NOT EXISTS chunks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              document_id INTEGER NOT NULL,
              ordinal INTEGER NOT NULL,
              text TEXT NOT NULL,
              text_hash TEXT NOT NULL,
              segment_type TEXT NOT NULL DEFAULT 'paragraph',
              segment_version TEXT NOT NULL DEFAULT 'paragraph-v1',
              speaker_role TEXT NOT NULL DEFAULT 'unknown',
              source_type TEXT NOT NULL DEFAULT 'unknown',
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              UNIQUE(document_id, ordinal),
              FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id, ordinal);
            CREATE TABLE IF NOT EXISTS entities (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              entity_type TEXT NOT NULL,
              canonical_name TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(entity_type, canonical_name)
            );
            CREATE TABLE IF NOT EXISTS facts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              legacy_structured_id INTEGER UNIQUE,
              document_id INTEGER NOT NULL,
              chunk_id INTEGER,
              subject_entity_id INTEGER,
              subject_scope TEXT NOT NULL DEFAULT 'unknown',
              resolved_entity_type TEXT NOT NULL DEFAULT 'unknown',
              personal_relevance TEXT NOT NULL DEFAULT 'unknown',
              extraction_confidence REAL,
              truth_confidence REAL,
              evidence_support_count INTEGER NOT NULL DEFAULT 0,
              evidence_contradiction_count INTEGER NOT NULL DEFAULT 0,
              trust_details_json TEXT NOT NULL DEFAULT '{}',
              trust_updated_at TEXT,
              validation_status TEXT NOT NULL DEFAULT 'pending',
              validation_reason TEXT NOT NULL DEFAULT '',
              validated_at TEXT,
              retrieval_eligibility TEXT NOT NULL DEFAULT 'pending',
              category TEXT NOT NULL,
              fact_type TEXT NOT NULL,
              occurred_on TEXT,
              valid_from TEXT,
              valid_to TEXT,
              status TEXT NOT NULL DEFAULT 'unknown',
              fact_key TEXT,
              source_chunk_id INTEGER,
              source_attachment_id INTEGER,
              supersedes_fact_id INTEGER,
              value_json TEXT NOT NULL,
              summary TEXT NOT NULL,
              confidence REAL,
              extractor TEXT NOT NULL,
              extractor_model TEXT,
              prompt_version TEXT,
              extracted_at TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY(document_id) REFERENCES documents(id),
              FOREIGN KEY(chunk_id) REFERENCES chunks(id),
              FOREIGN KEY(source_chunk_id) REFERENCES chunks(id),
              FOREIGN KEY(source_attachment_id) REFERENCES attachments(id),
              FOREIGN KEY(supersedes_fact_id) REFERENCES facts(id),
              FOREIGN KEY(subject_entity_id) REFERENCES entities(id)
            );
            CREATE INDEX IF NOT EXISTS idx_facts_category_date ON facts(category, occurred_on DESC);
            CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(subject_entity_id, fact_type);
            CREATE TABLE IF NOT EXISTS entity_mentions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              fact_id INTEGER,
              document_id INTEGER NOT NULL,
              chunk_id INTEGER,
              mention_text TEXT NOT NULL,
              resolved_entity_id INTEGER,
              entity_type TEXT NOT NULL DEFAULT 'unknown',
              resolution_status TEXT NOT NULL DEFAULT 'candidate',
              confidence REAL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(fact_id) REFERENCES facts(id),
              FOREIGN KEY(document_id) REFERENCES documents(id),
              FOREIGN KEY(chunk_id) REFERENCES chunks(id),
              FOREIGN KEY(resolved_entity_id) REFERENCES entities(id),
              CHECK(resolution_status IN ('candidate','resolved','rejected','ambiguous'))
            );
            CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity ON entity_mentions(resolved_entity_id, resolution_status);
            CREATE TABLE IF NOT EXISTS memory_corrections (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              fact_id INTEGER,
              entity_id INTEGER,
              correction_type TEXT NOT NULL,
              before_json TEXT NOT NULL DEFAULT '{}',
              after_json TEXT NOT NULL DEFAULT '{}',
              reason TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT 'automatic',
              quality_version TEXT NOT NULL DEFAULT 'memory-quality-v1',
              created_at TEXT NOT NULL,
              FOREIGN KEY(fact_id) REFERENCES facts(id),
              FOREIGN KEY(entity_id) REFERENCES entities(id)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_corrections_fact ON memory_corrections(fact_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_corrections_entity ON memory_corrections(entity_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS personal_inferences (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              statement TEXT NOT NULL,
              inference_type TEXT NOT NULL DEFAULT 'pattern',
              domain TEXT NOT NULL DEFAULT 'other',
              confidence REAL NOT NULL DEFAULT 0,
              source_fact_ids_json TEXT NOT NULL DEFAULT '[]',
              source_decision_ids_json TEXT NOT NULL DEFAULT '[]',
              source_chunk_ids_json TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL,
              last_evaluated_at TEXT NOT NULL,
              expires_at TEXT,
              status TEXT NOT NULL DEFAULT 'active',
              CHECK(status IN ('active','superseded','expired','rejected'))
            );
            CREATE INDEX IF NOT EXISTS idx_personal_inferences_active ON personal_inferences(status,domain,last_evaluated_at DESC);
            CREATE TABLE IF NOT EXISTS repair_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              status TEXT NOT NULL DEFAULT 'running',
              reason TEXT NOT NULL DEFAULT '',
              scanned_count INTEGER NOT NULL DEFAULT 0,
              changed_count INTEGER NOT NULL DEFAULT 0,
              reclassified_count INTEGER NOT NULL DEFAULT 0,
              excluded_count INTEGER NOT NULL DEFAULT 0,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              error TEXT NOT NULL DEFAULT '',
              CHECK(status IN ('running','completed','failed'))
            );
            CREATE INDEX IF NOT EXISTS idx_repair_jobs_status ON repair_jobs(status,started_at DESC);
            CREATE TABLE IF NOT EXISTS execution_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              decision_id INTEGER,
              plan_id INTEGER,
              event_type TEXT NOT NULL,
              summary TEXT NOT NULL DEFAULT '',
              source_entry_id INTEGER,
              source_chunk_id INTEGER,
              occurred_at TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY(decision_id) REFERENCES decisions(id),
              FOREIGN KEY(plan_id) REFERENCES plans(id),
              FOREIGN KEY(source_entry_id) REFERENCES entries(id),
              FOREIGN KEY(source_chunk_id) REFERENCES chunks(id)
            );
            CREATE INDEX IF NOT EXISTS idx_execution_events_decision ON execution_events(decision_id,occurred_at DESC,created_at DESC);
            CREATE TABLE IF NOT EXISTS fact_evidence (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              fact_id INTEGER NOT NULL,
              evidence_kind TEXT NOT NULL DEFAULT 'conversation',
              source_chunk_id INTEGER,
              source_attachment_id INTEGER,
              source_group TEXT NOT NULL DEFAULT '',
              source_identity TEXT NOT NULL DEFAULT '',
              quote TEXT NOT NULL DEFAULT '',
              support TEXT NOT NULL DEFAULT 'supports',
              reliability REAL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(fact_id) REFERENCES facts(id),
              FOREIGN KEY(source_chunk_id) REFERENCES chunks(id),
              FOREIGN KEY(source_attachment_id) REFERENCES attachments(id),
              CHECK(support IN ('supports','contradicts','context'))
            );
            CREATE INDEX IF NOT EXISTS idx_fact_evidence_fact ON fact_evidence(fact_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_fact_evidence_source ON fact_evidence(source_group, evidence_kind);
            CREATE TABLE IF NOT EXISTS fact_currentness (
              fact_id INTEGER PRIMARY KEY,
              state TEXT NOT NULL DEFAULT 'unknown',
              current_key TEXT,
              valid_from TEXT,
              valid_until TEXT,
              replaced_by_fact_id INTEGER,
              updated_at TEXT NOT NULL,
              CHECK(state IN ('current', 'historical', 'superseded', 'unknown')),
              FOREIGN KEY(fact_id) REFERENCES facts(id),
              FOREIGN KEY(replaced_by_fact_id) REFERENCES facts(id)
            );
            CREATE INDEX IF NOT EXISTS idx_fact_currentness_key ON fact_currentness(current_key, state);
            CREATE TABLE IF NOT EXISTS memory_changes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              fact_id INTEGER,
              previous_fact_id INTEGER,
              change_type TEXT NOT NULL,
              summary TEXT NOT NULL,
              detail_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(fact_id) REFERENCES facts(id),
              FOREIGN KEY(previous_fact_id) REFERENCES facts(id)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_changes_created ON memory_changes(created_at DESC);
            CREATE TABLE IF NOT EXISTS memory_proposals (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              entry_id INTEGER NOT NULL,
              facts_json TEXT NOT NULL,
              policy TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              created_at TEXT NOT NULL,
              resolved_at TEXT,
              CHECK(policy IN ('confirm', 'never_auto')),
              CHECK(status IN ('pending', 'applied', 'discarded')),
              FOREIGN KEY(entry_id) REFERENCES entries(id)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_proposals_status ON memory_proposals(status, created_at DESC);
            CREATE TABLE IF NOT EXISTS decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              context TEXT NOT NULL DEFAULT '',
              options_json TEXT NOT NULL DEFAULT '[]',
              decision TEXT NOT NULL,
              rationale TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'decided',
              decided_on TEXT,
              source_entry_id INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              CHECK(status IN ('considering', 'decided', 'revisited')),
              FOREIGN KEY(source_entry_id) REFERENCES entries(id)
            );
            CREATE INDEX IF NOT EXISTS idx_decisions_decided_on ON decisions(decided_on DESC, created_at DESC);
            CREATE TABLE IF NOT EXISTS analysis_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              document_id INTEGER NOT NULL,
              provider TEXT NOT NULL,
              model TEXT NOT NULL,
              prompt_version TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '',
              started_at TEXT,
              finished_at TEXT,
              job_kind TEXT NOT NULL DEFAULT 'document',
              source_attachment_id INTEGER,
              source_chunk_id INTEGER,
              priority INTEGER NOT NULL DEFAULT 100,
              priority_reason TEXT NOT NULL DEFAULT 'backfill',
              requested_at TEXT,
              usage_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              CHECK(status IN ('pending', 'running', 'completed', 'failed')),
              UNIQUE(document_id, provider, model, prompt_version, content_hash),
              FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_jobs_status ON analysis_jobs(status, created_at);
            CREATE TABLE IF NOT EXISTS import_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_kind TEXT NOT NULL,
              file_name TEXT NOT NULL DEFAULT '',
              file_hash TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'running',
              last_shard TEXT NOT NULL DEFAULT '',
              created_count INTEGER NOT NULL DEFAULT 0,
              skipped_count INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '',
              started_at TEXT NOT NULL,
              finished_at TEXT,
              updated_at TEXT NOT NULL,
              UNIQUE(source_kind,file_hash),
              CHECK(status IN ('running','completed','failed'))
            );
            CREATE INDEX IF NOT EXISTS idx_import_jobs_status ON import_jobs(status,updated_at);
            CREATE TABLE IF NOT EXISTS recommendations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              domain TEXT NOT NULL,
              title TEXT NOT NULL,
              rationale TEXT NOT NULL DEFAULT '',
              options_json TEXT NOT NULL DEFAULT '[]',
              criteria_json TEXT NOT NULL DEFAULT '{}',
              source_fact_ids_json TEXT NOT NULL DEFAULT '[]',
              source_decision_ids_json TEXT NOT NULL DEFAULT '[]',
              source_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
              context_json TEXT NOT NULL DEFAULT '{}',
              tradeoffs_json TEXT NOT NULL DEFAULT '[]',
              missing_context_json TEXT NOT NULL DEFAULT '[]',
              status TEXT NOT NULL DEFAULT 'draft',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              CHECK(status IN ('draft','accepted','dismissed','converted'))
            );
            CREATE INDEX IF NOT EXISTS idx_recommendations_domain ON recommendations(domain, status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS plans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              domain TEXT NOT NULL,
              title TEXT NOT NULL,
              steps_json TEXT NOT NULL DEFAULT '[]',
              budget REAL,
              target_date TEXT,
              source_recommendation_id INTEGER,
              decision_id INTEGER,
              status TEXT NOT NULL DEFAULT 'draft',
              result TEXT NOT NULL DEFAULT '',
              checkpoints_json TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              CHECK(status IN ('draft','active','completed','cancelled')),
              FOREIGN KEY(source_recommendation_id) REFERENCES recommendations(id),
              FOREIGN KEY(decision_id) REFERENCES decisions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_plans_domain ON plans(domain, status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS app_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ux_feedback (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              screen TEXT NOT NULL,
              feedback_type TEXT NOT NULL DEFAULT 'improvement',
              body TEXT NOT NULL,
              expected_behavior TEXT NOT NULL DEFAULT '',
              severity TEXT NOT NULL DEFAULT 'medium',
              status TEXT NOT NULL DEFAULT 'open',
              created_at TEXT NOT NULL,
              resolved_at TEXT,
              CHECK(feedback_type IN ('improvement','bug','confusing','praise')),
              CHECK(severity IN ('low','medium','high')),
              CHECK(status IN ('open','resolved','dismissed'))
            );
            CREATE INDEX IF NOT EXISTS idx_ux_feedback_status ON ux_feedback(status,created_at DESC);
            CREATE TABLE IF NOT EXISTS privacy_audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              target_type TEXT NOT NULL,
              target_id INTEGER NOT NULL,
              delete_raw INTEGER NOT NULL DEFAULT 0,
              backup_path TEXT NOT NULL DEFAULT '',
              deleted_counts_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_privacy_audit_created ON privacy_audit_log(created_at DESC);
            CREATE TABLE IF NOT EXISTS finance_transactions (
              fact_id INTEGER PRIMARY KEY,
              asset_entity_id INTEGER,
              amount REAL NOT NULL,
              normalized_amount REAL,
              currency TEXT NOT NULL,
              unit TEXT NOT NULL DEFAULT '',
              raw_amount_text TEXT NOT NULL DEFAULT '',
              transaction_type TEXT NOT NULL,
              transaction_kind TEXT NOT NULL DEFAULT '',
              actor TEXT NOT NULL DEFAULT 'unknown',
              is_actual INTEGER,
              eligibility_state TEXT NOT NULL DEFAULT 'pending',
              eligibility_reason TEXT NOT NULL DEFAULT '',
              validator_version TEXT NOT NULL DEFAULT 'finance-validator-v1',
              validated_at TEXT,
              occurred_on TEXT,
              FOREIGN KEY(fact_id) REFERENCES facts(id),
              FOREIGN KEY(asset_entity_id) REFERENCES entities(id),
              CHECK(eligibility_state IN ('auto_confirmed','pending','excluded','confirmed'))
            );
            CREATE INDEX IF NOT EXISTS idx_finance_transactions_date ON finance_transactions(occurred_on DESC);
            CREATE TABLE IF NOT EXISTS finance_transaction_candidates (
              fact_id INTEGER PRIMARY KEY,
              asset_entity_id INTEGER,
              amount REAL,
              normalized_amount REAL,
              currency TEXT NOT NULL DEFAULT '',
              unit TEXT NOT NULL DEFAULT '',
              raw_amount_text TEXT NOT NULL DEFAULT '',
              transaction_kind TEXT NOT NULL DEFAULT '',
              actor TEXT NOT NULL DEFAULT 'unknown',
              is_actual INTEGER,
              eligibility_state TEXT NOT NULL DEFAULT 'pending',
              eligibility_reason TEXT NOT NULL DEFAULT '',
              validator_version TEXT NOT NULL DEFAULT 'finance-validator-v1',
              validated_at TEXT,
              occurred_on TEXT,
              FOREIGN KEY(fact_id) REFERENCES facts(id),
              FOREIGN KEY(asset_entity_id) REFERENCES entities(id),
              CHECK(eligibility_state IN ('pending','excluded','confirmed'))
            );
            CREATE INDEX IF NOT EXISTS idx_finance_candidates_state ON finance_transaction_candidates(eligibility_state, validated_at);
            CREATE TABLE IF NOT EXISTS fact_reviews (
              fact_id INTEGER PRIMARY KEY,
              state TEXT NOT NULL DEFAULT 'pending',
              reason TEXT NOT NULL DEFAULT '',
              review_note TEXT NOT NULL DEFAULT '',
              reviewed_at TEXT,
              created_at TEXT NOT NULL,
              CHECK(state IN ('pending', 'confirmed', 'rejected', 'deferred')),
              FOREIGN KEY(fact_id) REFERENCES facts(id)
            );
            CREATE INDEX IF NOT EXISTS idx_fact_reviews_state ON fact_reviews(state);
            CREATE TABLE IF NOT EXISTS fact_review_queue_state (
              fact_id INTEGER PRIMARY KEY,
              snoozed_until TEXT,
              last_presented_at TEXT,
              presentation_count INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(fact_id) REFERENCES facts(id)
            );
            CREATE INDEX IF NOT EXISTS idx_fact_review_queue_snoozed
              ON fact_review_queue_state(snoozed_until, updated_at);
            CREATE TABLE IF NOT EXISTS memory_proposal_queue_state (
              proposal_id INTEGER PRIMARY KEY,
              snoozed_until TEXT,
              is_deferred INTEGER NOT NULL DEFAULT 0,
              last_presented_at TEXT,
              presentation_count INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(proposal_id) REFERENCES memory_proposals(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_memory_proposal_queue_snoozed
              ON memory_proposal_queue_state(snoozed_until, updated_at);
            CREATE TABLE IF NOT EXISTS action_center_snoozes (
              action_key TEXT PRIMARY KEY,
              snoozed_until TEXT,
              keep_in_inbox INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_action_center_snoozes_until
              ON action_center_snoozes(snoozed_until, updated_at);
            CREATE TABLE IF NOT EXISTS embeddings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chunk_id INTEGER NOT NULL,
              model TEXT NOT NULL,
              dimensions INTEGER NOT NULL,
              vector_json TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(chunk_id, model, content_hash),
              FOREIGN KEY(chunk_id) REFERENCES chunks(id)
            );
            CREATE TABLE IF NOT EXISTS embedding_jobs (
              chunk_id INTEGER PRIMARY KEY,
              state TEXT NOT NULL DEFAULT 'pending',
              last_error TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              CHECK(state IN ('pending', 'running', 'completed', 'failed')),
              FOREIGN KEY(chunk_id) REFERENCES chunks(id)
            );
            """
        )
        entry_columns = {row["name"] for row in connection.execute("PRAGMA table_info(entries)")}
        if "external_id" not in entry_columns:
            connection.execute("ALTER TABLE entries ADD COLUMN external_id TEXT")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_external_id ON entries(external_id) WHERE external_id IS NOT NULL")
        fact_columns = {row["name"] for row in connection.execute("PRAGMA table_info(facts)")}
        if "legacy_structured_id" not in fact_columns:
            connection.execute("ALTER TABLE facts ADD COLUMN legacy_structured_id INTEGER")
        for column, definition in {
            "subject_scope": "TEXT NOT NULL DEFAULT 'unknown'",
            "resolved_entity_type": "TEXT NOT NULL DEFAULT 'unknown'",
            "personal_relevance": "TEXT NOT NULL DEFAULT 'unknown'",
            "extraction_confidence": "REAL",
            "truth_confidence": "REAL",
            "evidence_support_count": "INTEGER NOT NULL DEFAULT 0",
            "evidence_contradiction_count": "INTEGER NOT NULL DEFAULT 0",
            "trust_details_json": "TEXT NOT NULL DEFAULT '{}'",
            "trust_updated_at": "TEXT",
            "validation_status": "TEXT NOT NULL DEFAULT 'pending'",
            "validation_reason": "TEXT NOT NULL DEFAULT ''",
            "validated_at": "TEXT",
            "retrieval_eligibility": "TEXT NOT NULL DEFAULT 'pending'",
            "valid_from": "TEXT",
            "valid_to": "TEXT",
            "effective_at": "TEXT",
            "observed_at": "TEXT",
            "temporal_source": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'unknown'",
            "fact_key": "TEXT",
            "source_chunk_id": "INTEGER",
            "source_attachment_id": "INTEGER",
            "supersedes_fact_id": "INTEGER",
            "extractor_model": "TEXT",
            "prompt_version": "TEXT",
            "extracted_at": "TEXT",
        }.items():
            if column not in fact_columns:
                connection.execute(f"ALTER TABLE facts ADD COLUMN {column} {definition}")
        connection.execute("UPDATE facts SET source_chunk_id=chunk_id WHERE source_chunk_id IS NULL")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_facts_current ON facts(status, category, fact_type, valid_to)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_facts_fact_key ON facts(fact_key, status)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_facts_retrieval ON facts(retrieval_eligibility, status, category)")
        evidence_columns = {row["name"] for row in connection.execute("PRAGMA table_info(fact_evidence)")}
        if "source_identity" not in evidence_columns:
            connection.execute("ALTER TABLE fact_evidence ADD COLUMN source_identity TEXT NOT NULL DEFAULT ''")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_fact_evidence_identity ON fact_evidence(fact_id,source_identity,support)")
        chunk_columns = {row["name"] for row in connection.execute("PRAGMA table_info(chunks)")}
        for column, definition in {
            "segment_type": "TEXT NOT NULL DEFAULT 'paragraph'",
            "segment_version": "TEXT NOT NULL DEFAULT 'paragraph-v1'",
            "speaker_role": "TEXT NOT NULL DEFAULT 'unknown'",
            "source_type": "TEXT NOT NULL DEFAULT 'unknown'",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
        }.items():
            if column not in chunk_columns:
                connection.execute(f"ALTER TABLE chunks ADD COLUMN {column} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_chunks_active ON chunks(document_id,is_active,ordinal)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source_role ON chunks(speaker_role,source_type,is_active)")
        legacy_chunks = connection.execute(
            "SELECT c.id,c.text,d.source FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.speaker_role='unknown' OR c.source_type='unknown'"
        ).fetchall()
        for legacy_chunk in legacy_chunks:
            source_type = legacy_chunk["source"] or "unknown"
            connection.execute(
                "UPDATE chunks SET speaker_role=?,source_type=? WHERE id=?",
                (source_role_for_text(legacy_chunk["text"], source_type), source_type, legacy_chunk["id"]),
            )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS entity_mentions (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 fact_id INTEGER,
                 document_id INTEGER NOT NULL,
                 chunk_id INTEGER,
                 mention_text TEXT NOT NULL,
                 resolved_entity_id INTEGER,
                 entity_type TEXT NOT NULL DEFAULT 'unknown',
                 resolution_status TEXT NOT NULL DEFAULT 'candidate',
                 confidence REAL,
                 created_at TEXT NOT NULL,
                 FOREIGN KEY(fact_id) REFERENCES facts(id),
                 FOREIGN KEY(document_id) REFERENCES documents(id),
                 FOREIGN KEY(chunk_id) REFERENCES chunks(id),
                 FOREIGN KEY(resolved_entity_id) REFERENCES entities(id),
                 CHECK(resolution_status IN ('candidate','resolved','rejected','ambiguous'))
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity ON entity_mentions(resolved_entity_id, resolution_status)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS memory_corrections (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 fact_id INTEGER,
                 entity_id INTEGER,
                 correction_type TEXT NOT NULL,
                 before_json TEXT NOT NULL DEFAULT '{}',
                 after_json TEXT NOT NULL DEFAULT '{}',
                 reason TEXT NOT NULL DEFAULT '',
                 source TEXT NOT NULL DEFAULT 'automatic',
                 quality_version TEXT NOT NULL DEFAULT 'memory-quality-v1',
                 created_at TEXT NOT NULL,
                 FOREIGN KEY(fact_id) REFERENCES facts(id),
                 FOREIGN KEY(entity_id) REFERENCES entities(id)
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_memory_corrections_fact ON memory_corrections(fact_id, created_at DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_memory_corrections_entity ON memory_corrections(entity_id, created_at DESC)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_legacy_structured_id ON facts(legacy_structured_id) WHERE legacy_structured_id IS NOT NULL")
        transaction_columns = {row["name"] for row in connection.execute("PRAGMA table_info(finance_transactions)")}
        for column, definition in {
            "normalized_amount": "REAL",
            "unit": "TEXT NOT NULL DEFAULT ''",
            "raw_amount_text": "TEXT NOT NULL DEFAULT ''",
            "transaction_kind": "TEXT NOT NULL DEFAULT ''",
            "actor": "TEXT NOT NULL DEFAULT 'unknown'",
            "is_actual": "INTEGER",
            "eligibility_state": "TEXT NOT NULL DEFAULT 'pending'",
            "eligibility_reason": "TEXT NOT NULL DEFAULT ''",
            "validator_version": "TEXT NOT NULL DEFAULT 'finance-validator-v1'",
            "validated_at": "TEXT",
        }.items():
            if column not in transaction_columns:
                connection.execute(f"ALTER TABLE finance_transactions ADD COLUMN {column} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_finance_transactions_eligibility ON finance_transactions(eligibility_state, actor, is_actual)")
        decision_columns = {row["name"] for row in connection.execute("PRAGMA table_info(decisions)")}
        for column, definition in {
            "domain": "TEXT NOT NULL DEFAULT 'other'",
            "question": "TEXT NOT NULL DEFAULT ''",
            "selected_option": "TEXT NOT NULL DEFAULT ''",
            "related_fact_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "related_entity_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "result": "TEXT NOT NULL DEFAULT ''",
            "later_evaluation": "TEXT NOT NULL DEFAULT ''",
            "source_recommendation_id": "INTEGER",
            "outcome_recorded_at": "TEXT",
            "evaluation_recorded_at": "TEXT",
            # The legacy ``status`` CHECK is intentionally preserved for
            # compatibility.  This richer state machine is additive and is
            # the canonical state used by new Decision/Execution APIs.
            "decision_state": "TEXT NOT NULL DEFAULT 'decided'",
        }.items():
            if column not in decision_columns:
                connection.execute(f"ALTER TABLE decisions ADD COLUMN {column} {definition}")
        connection.execute(
            "UPDATE decisions SET decision_state=CASE "
            "WHEN result IS NOT NULL AND TRIM(result)!='' THEN 'result' "
            "WHEN status='considering' THEN 'considered' "
            "WHEN status='revisited' THEN 'candidate' "
            "ELSE 'decided' END "
            "WHERE decision_state IS NULL OR decision_state='' OR decision_state='decided'"
        )
        analysis_columns = {row["name"] for row in connection.execute("PRAGMA table_info(analysis_jobs)")}
        for column, definition in {
            "job_kind": "TEXT NOT NULL DEFAULT 'document'",
            "source_attachment_id": "INTEGER",
            "source_chunk_id": "INTEGER",
            "priority": "INTEGER NOT NULL DEFAULT 100",
            "priority_reason": "TEXT NOT NULL DEFAULT 'backfill'",
            "requested_at": "TEXT",
            "usage_count": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in analysis_columns:
                connection.execute(f"ALTER TABLE analysis_jobs ADD COLUMN {column} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_analysis_jobs_kind ON analysis_jobs(job_kind, status, created_at)")
        # Retrieval consults these columns for every semantic candidate.  Without
        # the indexes, a large ChatGPT import turns the correlated EXISTS checks
        # into repeated full-table scans.
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_source_chunk_relevance "
            "ON facts(source_chunk_id,personal_relevance)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_embeddings_model_created "
            "ON embeddings(model,created_at DESC)"
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_analysis_jobs_chunk_scope
               ON analysis_jobs(source_chunk_id,job_kind,provider,model,prompt_version)"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_analysis_jobs_runnable
               ON analysis_jobs(provider,model,prompt_version,status,priority,requested_at,created_at)"""
        )
        recommendation_columns = {row["name"] for row in connection.execute("PRAGMA table_info(recommendations)")}
        for column, definition in {
            "source_evidence_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "context_json": "TEXT NOT NULL DEFAULT '{}'",
            "tradeoffs_json": "TEXT NOT NULL DEFAULT '[]'",
            "missing_context_json": "TEXT NOT NULL DEFAULT '[]'",
        }.items():
            if column not in recommendation_columns:
                connection.execute(f"ALTER TABLE recommendations ADD COLUMN {column} {definition}")
        plan_columns = {row["name"] for row in connection.execute("PRAGMA table_info(plans)")}
        if "checkpoints_json" not in plan_columns:
            connection.execute("ALTER TABLE plans ADD COLUMN checkpoints_json TEXT NOT NULL DEFAULT '[]'")
        try:
            connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(chunk_id UNINDEXED, text)")
        except sqlite3.OperationalError:
            pass
        seed_memory_categories(connection)
        if run_startup_maintenance:
            reclassify_generic_reference_facts(connection)
        record_schema_migrations(connection)
    migrate_action_center_review_inbox()
    migrate_action_center_review_inbox_stabilization()
    if not run_startup_maintenance:
        # The schema checks above remain enabled. Large, already-migrated local
        # databases can defer the full Fact/Evidence re-audit to an explicit
        # maintenance run without disabling normal workers or later queuing.
        for provider in extraction_providers():
            scope = (provider, provider_model(provider, "extract"), PROMPT_VERSION)
            ANALYSIS_PREFILTER_SCOPES.add(scope)
            ANALYSIS_PREFILTER_SCOPE = scope
        return
    migrate_memory_layers()
    prepare_conversation_reanalysis(queue_jobs=queue_analysis_on_start)
    backfill_fact_keys()
    migrate_current_truth()
    migrate_visualization_benchmark()
    migrate_decision_replay()
    migrate_action_center_review_inbox()
    migrate_action_center_review_inbox_stabilization()
    backfill_fact_evidence()
    with db() as connection:
        backfill_fact_evidence_identities(connection)
    auto_confirm_low_risk_facts()
    audit_memory_quality()
    reevaluate_finance_transactions()
    if queue_analysis_on_start:
        queue_analysis_jobs()
    else:
        # Existing jobs remain runnable; only the expensive full startup scan
        # is skipped. New chunks still pass through the local prefilter when a
        # later queue operation sees them.
        for provider in extraction_providers():
            scope = (provider, provider_model(provider, "extract"), PROMPT_VERSION)
            ANALYSIS_PREFILTER_SCOPES.add(scope)
            ANALYSIS_PREFILTER_SCOPE = scope


def json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def chunk_text(text: str, size: int = 1800) -> list[str]:
    """Chunk on paragraph boundaries where possible, keeping a stable search unit."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for paragraph in re.split(r"\n{2,}", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and len(current) + len(paragraph) + 2 > size:
            chunks.append(current)
            current = ""
        while len(paragraph) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:size])
            paragraph = paragraph[size:]
        current = f"{current}\n\n{paragraph}".strip() if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def conversation_turn_chunks(text: str, size: int = 1800) -> list[str]:
    """Split a ChatGPT export without joining unrelated turns.

    The importer writes each message as ``role: text`` separated by a blank
    line.  We keep those message boundaries intact when grouping turns.  A
    single very long message may still be split, but it is never merged with
    the next message, so an extracted Fact cannot silently borrow a later
    topic as its source context.
    """
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", str(text or "").strip()) if part.strip()]
    if not paragraphs:
        return []
    # A unit is one user turn plus its immediately following assistant turn.
    # We deliberately do not pack several exchanges into one chunk: a topic
    # change at the next user turn must not become hidden Evidence context.
    units: list[str] = []
    index = 0
    while index < len(paragraphs):
        unit = paragraphs[index]
        if index + 1 < len(paragraphs):
            next_part = paragraphs[index + 1]
            if unit.lower().startswith("user:") and next_part.lower().startswith("assistant:"):
                unit = f"{unit}\n\n{next_part}"
                index += 1
        units.append(unit)
        index += 1

    chunks: list[str] = []
    for unit in units:
        if len(unit) <= size:
            chunks.append(unit)
            continue
        # Keep a long exchange self-contained. Prefer sentence/newline
        # boundaries and only fall back to a hard cut within this exchange.
        remainder = unit
        while len(remainder) > size:
            boundary = max(
                (m.start() + 1 for m in re.finditer(r"[。！？.!?]\s+|\n", remainder[:size])),
                default=0,
            )
            cut = boundary if boundary >= max(200, size // 2) else size
            chunks.append(remainder[:cut].strip())
            remainder = remainder[cut:].lstrip()
        if remainder:
            chunks.append(remainder)
    return chunks


def chunk_may_contain_personal_memory(text: str) -> bool:
    """Cheap local prefilter for ChatGPT chunks before an LLM Job is queued."""
    normalized = str(text or "").lower()
    markers = (
        "自分", "私", "本人", "俺", "僕", "自宅", "家賃", "資産", "積立", "持株会",
        "年収", "職種", "症状", "受診", "検査結果", "友人", "家族", "恋人", "同僚",
        "会った", "連絡した", "行った", "行きたい", "旅行", "住んで", "仕事", "判断",
        "決めた", "購入", "売却", "買った", "売った", "好き", "趣味", "疲労", "生活費",
        "予定", "休日", "将来", "my ", "i ", "i'm ", "personal",
    )
    return any(marker.lower() in normalized for marker in markers)


def index_chunk(connection: sqlite3.Connection, chunk_id: int, text: str) -> None:
    """Keep the keyword index and embedding queue derived from the immutable chunk."""
    try:
        connection.execute("INSERT OR REPLACE INTO chunk_fts(rowid,chunk_id,text) VALUES(?,?,?)", (chunk_id, chunk_id, text))
    except sqlite3.OperationalError:
        pass
    connection.execute(
        "INSERT OR IGNORE INTO embedding_jobs(chunk_id,state,updated_at) VALUES(?,?,?)",
        (chunk_id, "pending", now()),
    )


EMBEDDING_MODEL = "local-charhash-v1"
EMBEDDING_DIMENSIONS = 256


def local_embedding(text: str, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Small deterministic local embedding used when no embedding service is configured.

    Character n-grams work reasonably for Japanese without tokenization and keep
    the privacy boundary local. This is a retrieval aid, never a Fact extractor.
    """
    vector = [0.0] * dimensions
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    grams = [normalized[i:i + 3] for i in range(max(0, len(normalized) - 2))]
    if not grams and normalized:
        grams = [normalized]
    for gram in grams[:12000]:
        digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = sum(value * value for value in vector) ** 0.5
    return [round(value / norm, 8) if norm else 0.0 for value in vector]


def process_embedding_jobs(limit: int = 32) -> int:
    processed = 0
    for _ in range(max(1, min(limit, 128))):
        with db() as connection:
            # A deleted/corrupt source chunk must not leave a Job pending forever.
            connection.execute(
                """UPDATE embedding_jobs SET state='failed',last_error='source chunk missing',updated_at=?
                   WHERE state IN ('pending','running') AND NOT EXISTS (
                     SELECT 1 FROM chunks c WHERE c.id=embedding_jobs.chunk_id
                   )""",
                (now(),),
            )
            # Recover a worker interrupted after claiming a Job.
            connection.execute(
                """UPDATE embedding_jobs SET state='pending',last_error='',updated_at=?
                   WHERE state='running' AND updated_at < ?""",
                (now(), (datetime.now(timezone.utc).astimezone() - timedelta(minutes=5)).isoformat(timespec="seconds")),
            )
            job = connection.execute(
                """SELECT j.chunk_id,c.text FROM embedding_jobs j JOIN chunks c ON c.id=j.chunk_id
                   WHERE j.state IN ('pending','failed') ORDER BY j.updated_at,j.chunk_id LIMIT 1"""
            ).fetchone()
            if not job:
                break
            claimed = connection.execute(
                "UPDATE embedding_jobs SET state='running',last_error='',updated_at=? WHERE chunk_id=? AND state IN ('pending','failed')",
                (now(), job["chunk_id"]),
            )
            if not claimed.rowcount:
                continue
        try:
            vector = local_embedding(job["text"])
            with db() as connection:
                digest = hashlib.sha256(job["text"].encode("utf-8")).hexdigest()
                connection.execute(
                    """INSERT INTO embeddings(chunk_id,model,dimensions,vector_json,content_hash,created_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(chunk_id,model,content_hash) DO UPDATE SET vector_json=excluded.vector_json,created_at=excluded.created_at""",
                    (job["chunk_id"], EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, json.dumps(vector), digest, now()),
                )
                connection.execute("UPDATE embedding_jobs SET state='completed',last_error='',updated_at=? WHERE chunk_id=?", (now(), job["chunk_id"]))
            processed += 1
        except (sqlite3.Error, UnicodeError) as error:
            with db() as connection:
                connection.execute("UPDATE embedding_jobs SET state='failed',last_error=?,updated_at=? WHERE chunk_id=?", (str(error)[:1000], now(), job["chunk_id"]))
    return processed


def embedding_loop() -> None:
    while True:
        try:
            process_embedding_jobs(16)
        except sqlite3.Error as error:
            print(f"[embedding] {error}")
        time.sleep(2)


def semantic_candidate_allowed(message: str, title: str, body: str, score: float) -> bool:
    """Prevent hash-embedding collisions from answering a short named query."""
    normalized_query = re.sub(r"\s+", "", str(message or "")).lower()
    haystack = f"{title}\n{body}".lower()
    terms = [
        term for term in re.split(r"[\s、。・,/]+", str(message or "").lower())
        if len(term) >= 2
    ]
    lexical_match = any(term in haystack for term in terms)
    if lexical_match:
        return score >= 0.05
    sentence_markers = ("は", "を", "に", "で", "が", "へ", "たい", "どう", "何", "いつ", "予定", "教えて", "相談")
    looks_like_named_query = (
        len(normalized_query) <= 12
        and not any(marker in normalized_query for marker in sentence_markers)
    )
    if looks_like_named_query:
        return score >= 0.45
    return score >= 0.18


def semantic_search(message: str, limit: int = 8) -> list[dict[str, str]]:
    query_vector = local_embedding(message)
    with db() as connection:
        rows = connection.execute(
            """SELECT e.id,e.title,c.text AS body,e.kind,e.created_at,em.vector_json
               FROM embeddings em JOIN chunks c ON c.id=em.chunk_id
               JOIN documents d ON d.id=c.document_id JOIN entries e ON e.id=d.legacy_entry_id
               WHERE em.model=?
                 AND (
                   EXISTS (
                     SELECT 1 FROM facts good LEFT JOIN fact_reviews gr ON gr.fact_id=good.id
                     WHERE good.source_chunk_id=c.id
                       AND (
                         good.personal_relevance='linked_context'
                         OR (
                           good.personal_relevance='personal'
                           AND COALESCE(gr.state,'pending')!='rejected'
                         )
                       )
                   )
                   OR (
                     NOT EXISTS (SELECT 1 FROM facts any_fact WHERE any_fact.source_chunk_id=c.id)
                     AND NOT EXISTS (
                       SELECT 1 FROM analysis_jobs skipped
                       WHERE skipped.source_chunk_id=c.id AND skipped.status='completed'
                         AND skipped.error='excluded by personal relevance prefilter'
                     )
                   )
                 )
               ORDER BY em.created_at DESC LIMIT 2000""", (EMBEDDING_MODEL,)
        ).fetchall()
    scored = []
    for row in rows:
        try:
            vector = json.loads(row["vector_json"])
            score = sum(float(a) * float(b) for a, b in zip(query_vector, vector))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if semantic_candidate_allowed(message, row["title"], row["body"], score):
            item = {key: row[key] for key in row.keys() if key != "vector_json"}
            scored.append((score, item | {"kind": "semantic", "score": round(score, 4), "tags": "semantic"}))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[:limit]]


def ensure_document_for_entry(entry_id: int) -> int:
    """Backfill the new document/chunk layer from a legacy raw entry."""
    with db() as connection:
        entry = connection.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not entry:
            raise ValueError("Entry not found")
        timestamp = entry["created_at"] or now()
        connection.execute(
            """INSERT OR IGNORE INTO documents(legacy_entry_id,title,source,source_created_at,ingested_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (entry["id"], entry["title"], entry["source"], timestamp, timestamp, timestamp, entry["updated_at"] or timestamp),
        )
        document = connection.execute("SELECT id FROM documents WHERE legacy_entry_id=?", (entry_id,)).fetchone()
        document_id = document["id"]
        is_conversation = entry["kind"] == "conversation" or entry["source"] == "chatgpt-export"
        desired_version = CHUNK_VERSION if is_conversation else "paragraph-v1"
        active_version = connection.execute(
            "SELECT 1 FROM chunks WHERE document_id=? AND is_active=1 AND segment_version=? LIMIT 1",
            (document_id, desired_version),
        ).fetchone()
        if not active_version:
            # Never delete old source chunks: facts and evidence may still
            # point at them.  They become inactive and are kept for audit.
            if is_conversation:
                connection.execute(
                    "UPDATE chunks SET is_active=0 WHERE document_id=? AND is_active=1",
                    (document_id,),
                )
            splitter = conversation_turn_chunks if is_conversation else chunk_text
            segments = splitter(entry["body"])
            existing_max = connection.execute(
                "SELECT COALESCE(MAX(ordinal),0) FROM chunks WHERE document_id=?",
                (document_id,),
            ).fetchone()[0]
            existing_min = connection.execute(
                "SELECT COALESCE(MIN(ordinal),0) FROM chunks WHERE document_id=?",
                (document_id,),
            ).fetchone()[0]
            # Negative ordinals keep the new active revision ahead of legacy
            # rows while preserving the UNIQUE(document_id, ordinal) contract.
            start_ordinal = (existing_min - len(segments) - 1) if is_conversation else max(existing_max, 0) + 1
            for index, text in enumerate(segments):
                ordinal = start_ordinal + index if is_conversation else index + start_ordinal
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                cursor = connection.execute(
                    "INSERT INTO chunks(document_id,ordinal,text,text_hash,segment_type,segment_version,speaker_role,source_type,is_active,created_at) VALUES(?,?,?,?,?,?,?, ?,1,?)",
                    (document_id, ordinal, text, digest, "conversation_turn" if is_conversation else "paragraph", desired_version,
                     source_role_for_text(text, entry["source"]), entry["source"] or "unknown", now()),
                )
                index_chunk(connection, cursor.lastrowid, text)
    return document_id


def migrate_memory_layers() -> None:
    """Idempotently migrate current raw entries into documents and chunks."""
    with db() as connection:
        entry_ids = [row["id"] for row in connection.execute("SELECT id FROM entries")]
    for entry_id in entry_ids:
        ensure_document_for_entry(entry_id)
    with db() as connection:
        legacy_facts = connection.execute(
            """SELECT s.* FROM structured_memories s
               LEFT JOIN facts f ON f.legacy_structured_id=s.id WHERE f.id IS NULL"""
        ).fetchall()
        for item in legacy_facts:
            document = connection.execute("SELECT id FROM documents WHERE legacy_entry_id=?", (item["entry_id"],)).fetchone()
            if not document:
                continue
            chunk = connection.execute("SELECT id FROM chunks WHERE document_id=? AND is_active=1 ORDER BY ordinal LIMIT 1", (document["id"],)).fetchone()
            asset = item["asset"]
            entity_id = None
            if asset:
                entity_type = "asset" if item["category"] == "finance" else "subject"
                connection.execute("INSERT OR IGNORE INTO entities(entity_type,canonical_name,created_at,updated_at) VALUES(?,?,?,?)", (entity_type, asset, now(), now()))
                entity_id = connection.execute("SELECT id FROM entities WHERE entity_type=? AND canonical_name=?", (entity_type, asset)).fetchone()["id"]
            value = {"asset": asset, "amount": item["amount"], "currency": item["currency"], "details": json.loads(item["details_json"] or "{}")}
            cursor = connection.execute(
                """INSERT INTO facts(legacy_structured_id,document_id,chunk_id,subject_entity_id,category,fact_type,occurred_on,value_json,summary,confidence,extractor,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item["id"], document["id"], chunk["id"] if chunk else None, entity_id, item["category"], item["type"], item["occurred_on"], json.dumps(value, ensure_ascii=False), item["summary"], 0.5, "legacy", item["created_at"]),
            )
            fact_id = cursor.lastrowid
            connection.execute("INSERT OR IGNORE INTO fact_reviews(fact_id,state,reason,created_at) VALUES(?,?,?,?)", (fact_id, "pending", "旧形式の抽出データです。原文と照合してください。", now()))
            if item["category"] == "finance" and item["type"] == "transaction":
                sync_finance_transaction(connection, fact_id)


MUTABLE_FACT_TYPES = {"plan", "schedule", "preference", "status", "income", "asset_balance", "holding", "goal"}
HISTORICAL_FACT_TYPES = {"transaction", "event", "visit"}


def evidence_is_sufficient(fact: dict, evidence_text: str = "") -> bool:
    """Conservatively detect whether extracted content is grounded in the source."""
    details = fact.get("details") if isinstance(fact.get("details"), dict) else {}
    quote = str(fact.get("evidence_quote") or details.get("evidence_quote") or "").strip()
    source = str(evidence_text or "").strip().lower()
    if quote and quote.lower() in source:
        return True
    if not source:
        return False
    summary = str(fact.get("summary") or "").lower()
    asset = str(fact.get("asset") or "").lower()
    amount = str(fact.get("amount") or "").lower()
    if asset and len(asset) >= 2 and asset in source and (not amount or amount in source):
        return True
    tokens = [token for token in re.findall(r"[a-z0-9一-龥ぁ-んァ-ヶ]{2,}", summary) if token not in {"です", "ます", "した", "いる"}]
    if not tokens:
        return False
    overlap = sum(1 for token in tokens if token in source)
    return overlap >= 2 and overlap / max(len(tokens), 1) >= 0.35


def _legacy_fact_review_decision(fact: dict, confidence: float, user_confirmed: bool = False) -> tuple[str, str]:
    """Resolve whether a Fact needs a person without weakening sensitive-data rules.

    Only facts explicitly classified as low-risk ``auto`` are confirmed here.
    Finance, work, health, relationship, reference, and low-confidence facts
    remain pending until the owner reviews them.
    """
    if user_confirmed:
        return "confirmed", "ユーザー確認済み"
    policy = fact_policy(fact)
    if policy == "auto" and confidence >= AUTO_CONFIRM_CONFIDENCE_THRESHOLD:
        return "confirmed", "低リスクFactを自動確定"
    if policy == "never_auto":
        return "pending", "センシティブ情報のため本人確認が必要"
    if policy == "confirm":
        return "pending", "重要情報のため本人確認が必要"
    return "pending", "信頼度が低いため本人確認が必要"


def fact_review_decision(fact: dict, confidence: float, user_confirmed: bool = False,
                         evidence_text: str = "", transaction_validation: dict | None = None) -> tuple[str, str]:
    """Resolve review from evidence, not from a category-wide confirmation rule."""
    if user_confirmed:
        return "confirmed", "ユーザー確認済み"
    if is_ai_speculation(fact) or fact_policy(fact) == "exclude":
        return "rejected", "明確な非FactまたはAI推測のため自動除外"
    if transaction_validation:
        state = str(transaction_validation.get("state", "pending"))
        if state == "excluded":
            return "rejected", f"金融取引ではないと判定: {transaction_validation.get('reason', '')}"
        if state == "auto_confirmed":
            return "confirmed", "本人の実取引として自動確定"
        if transaction_validation.get("is_actual") is True and evidence_is_sufficient(fact, evidence_text) and confidence >= AUTO_CONFIRM_CONFIDENCE_THRESHOLD:
            return "confirmed", "Evidenceが明確な本人の実取引として自動確定"
        return "pending", "実取引か自動判定できないため確認が必要"
    if not evidence_is_sufficient(fact, evidence_text):
        return "pending", "Evidenceが不足しているため確認が必要"
    if confidence >= AUTO_CONFIRM_CONFIDENCE_THRESHOLD:
        return "confirmed", "Evidenceが明確で信頼度が閾値以上のため自動確定"
    return "pending", "Evidenceはあるが信頼度が閾値未満のため確認が必要"


def auto_confirm_low_risk_facts() -> int:
    """Auto-resolve pending reviews using source Evidence and explicitness."""
    changed = 0
    with db() as connection:
        rows = connection.execute(
            """SELECT f.id,f.category,f.fact_type,f.confidence,f.summary,f.value_json,c.text AS evidence,
                      c.is_active AS source_chunk_active,d.source AS document_source
               FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
               LEFT JOIN chunks c ON c.id=f.source_chunk_id
               LEFT JOIN documents d ON d.id=f.document_id
               WHERE r.state='pending'
                 AND NOT (d.source='chatgpt-export' AND f.source_chunk_id IS NULL)
                 AND NOT (d.source='chatgpt-export' AND COALESCE(c.is_active,0)=0)"""
        ).fetchall()
        for row in rows:
            try:
                value = json.loads(row["value_json"] or "{}")
            except json.JSONDecodeError:
                value = {}
            fact = {"category": row["category"], "type": row["fact_type"], "summary": row["summary"],
                    "asset": value.get("asset"), "amount": value.get("amount"), "details": value.get("details", {})}
            validation = None
            if row["category"] == "finance" and row["fact_type"] == "transaction":
                validation = validate_transaction_candidate({
                    "category": row["category"], "fact_type": row["fact_type"], "summary": row["summary"],
                    "value_json": row["value_json"], "confidence": row["confidence"], "document_title": "",
                }, row["evidence"] or "")
            state, reason = fact_review_decision(fact, float(row["confidence"] or 0), evidence_text=row["evidence"] or "", transaction_validation=validation)
            if state not in {"confirmed", "rejected"}:
                continue
            if row["category"] == "finance" and row["fact_type"] == "transaction":
                sync_finance_transaction(connection, row["id"], confirmed=state == "confirmed")
            cursor = connection.execute(
                """UPDATE fact_reviews SET state=?,reason=?,review_note=?,reviewed_at=?
                   WHERE fact_id=? AND state='pending'""",
                (state, reason, "自動判定（Evidence）", now(), row["id"]),
            )
            changed += cursor.rowcount
    return changed


def fact_policy(fact: dict) -> str:
    category = str(fact.get("category", "other"))
    fact_type = str(fact.get("type", "note"))
    text = " ".join(str(value or "") for value in (
        fact.get("summary"), fact.get("details"), fact.get("evidence_quote"),
    )).lower()
    inference_markers = (
        "\u63a8\u6e2c", "\u304b\u3082\u3057\u308c\u306a\u3044", "\u53ef\u80fd\u6027",
        "\u304a\u305d\u3089\u304f", "\u3068\u601d\u308f\u308c\u308b", "\u3068\u8003\u3048\u3089\u308c\u308b",
        "maybe", "likely", "probably", "inferred", "speculation", "guess",
    )
    if category == "reference" or str(fact.get("evidence_strength") or "").lower() == "inferred":
        return "exclude"
    if any(marker in text for marker in inference_markers):
        return "exclude"
    # Sensitivity can require stronger Evidence, but never forces a review by
    # itself.  fact_review_decision() makes the final evidence/confidence call.
    if category in {"relationship", "health", "finance", "work"} or fact_type in {
        "income", "asset_balance", "holding", "plan", "schedule", "goal",
    }:
        return "evidence_required"
    return "auto"


def is_ai_speculation(fact: dict) -> bool:
    if str(fact.get("evidence_strength") or "").lower() == "inferred":
        return True
    text = " ".join(str(value or "") for value in (
        fact.get("summary"), fact.get("details"), fact.get("evidence_quote"),
    )).lower()
    return any(marker in text for marker in (
        "\u63a8\u6e2c", "\u304b\u3082\u3057\u308c\u306a\u3044", "\u53ef\u80fd\u6027",
        "\u304a\u305d\u3089\u304f", "\u3068\u601d\u308f\u308c\u308b", "\u3068\u8003\u3048\u3089\u308c\u308b",
        "maybe", "likely", "probably", "inferred", "speculation", "guess",
    ))


def _fact_value(fact: sqlite3.Row | dict) -> dict:
    try:
        value = json.loads(fact.get("value_json") if isinstance(fact, dict) else fact["value_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def user_evidence_text(source_text: str) -> str:
    """Return the user's side of an imported exchange.

    Assistant explanations are useful raw context but cannot by themselves
    establish that an external person, place, company, or product belongs to
    the owner. Manual notes and screenshots have no role prefix, so their full
    text remains Evidence.
    """
    source = str(source_text or "").strip()
    if not source:
        return ""
    # ChatGPT exports are not consistent: some turns are separated by blank
    # lines, while others place ``user:`` and ``assistant:`` on adjacent
    # lines.  Parse role spans rather than treating the whole chunk as one
    # speaker.  This prevents assistant prose from becoming user Evidence in
    # a mixed chunk while preserving genuine user text.
    spans = list(re.finditer(
        r"(?ims)(?:^|\n)\s*(user|assistant|system|external)\s*:\s*(.*?)(?=\n\s*(?:user|assistant|system|external)\s*:|\Z)",
        source,
    ))
    if spans:
        user_parts = [match.group(2).strip() for match in spans if match.group(1).lower() == "user"]
        return "\n".join(part for part in user_parts if part)
    if re.match(r"(?is)^\s*assistant\s*:", source):
        return ""
    return source


def source_role_for_text(source_text: str, source_type: str = "unknown") -> str:
    """Classify the speaker/source without treating an assistant statement as user Evidence."""
    source = str(source_text or "")
    lowered = source.lower()
    if source_type in {"calendar", "gmail", "photo", "financial"}:
        return "external"
    roles = {match.group(1).lower() for match in re.finditer(r"(?im)^\s*(user|assistant|system|external)\s*:", source)}
    if roles == {"user"}:
        return "user"
    if roles == {"assistant"}:
        return "assistant"
    if roles == {"system"}:
        return "system"
    if roles == {"external"}:
        return "external"
    if "user" in roles and "assistant" in roles:
        return "mixed"
    if "user" in roles:
        return "user"
    if "assistant" in roles:
        return "assistant"
    if source_type in {"manual", "ai-ingest", "screenshot"} or not lowered:
        return "user"
    return "unknown"


def classify_entity_type(fact: dict, source_text: str = "") -> str:
    """Resolve an LLM Entity Candidate through a contextual deterministic gate."""
    details = fact.get("details") if isinstance(fact.get("details"), dict) else {}
    explicit = str(fact.get("entity_type") or details.get("entity_type") or "").strip().lower()
    category = str(fact.get("category") or "other")
    fact_type = str(fact.get("type") or "note")
    if explicit == "project" and category == "finance" and fact_type in {
        "asset_balance", "holding", "transaction", "income",
    }:
        explicit = "asset"
    if explicit == "aicharacter":
        explicit = "ai_character"
    explicit_hint = explicit if explicit in ENTITY_TYPES else ""
    value_text = str(fact.get("asset") or "")
    text = " ".join(str(item or "") for item in (
        fact.get("summary"), value_text, details, source_text,
    )).lower()
    marker_groups = {
        "fictional_character": (
            "\u30ad\u30e3\u30e9\u30af\u30bf\u30fc", "\u767b\u5834\u4eba\u7269", "\u30a2\u30cb\u30e1", "\u6f2b\u753b",
            "\u30b2\u30fc\u30e0\u30ad\u30e3\u30e9", "\u30dc\u30a4\u30b9\u30ed\u30a4\u30c9", "voicevox", "\u30de\u30b9\u30b3\u30c3\u30c8",
            "fictional character", "media character", "\u305a\u3093\u3060\u3082\u3093", "\u30c9\u30e9\u3048\u3082\u3093", "\u6771\u5317\u305a\u3093\u5b50",
        ),
        "media_character": ("\u30e1\u30c7\u30a3\u30a2\u30ad\u30e3\u30e9\u30af\u30bf\u30fc", "\u52d5\u753b\u30ad\u30e3\u30e9\u30af\u30bf\u30fc", "youtuber", "youtube", "vtuber", "hikakin", "\u30d2\u30ab\u30ad\u30f3"),
        "ai_character": ("ai\u30ad\u30e3\u30e9\u30af\u30bf\u30fc", "ai\u30ad\u30e3\u30e9", "\u30d0\u30fc\u30c1\u30e3\u30eb\u30ad\u30e3\u30e9\u30af\u30bf\u30fc", "virtual character"),
        "project": ("\u30d7\u30ed\u30b8\u30a7\u30af\u30c8", "project", "\u30b7\u30ea\u30fc\u30ba", "\u516c\u5f0f\u8a2d\u5b9a", "\u30d5\u30a1\u30f3\u30b3\u30f3\u30c6\u30f3\u30c4"),
        "work": ("\u4f5c\u54c1", "\u5c0f\u8aac", "\u6620\u753b", "\u6f2b\u753b", "\u30a2\u30cb\u30e1", "\u756a\u7d44", "\u30c9\u30e9\u30de", "work"),
        "organization": ("\u4f1a\u793e", "\u4f01\u696d", "\u56e3\u4f53", "\u6cd5\u4eba", "organization", "company", "openai", "\u697d\u5929"),
        # Avoid one-character markers such as 市/県: they appear in ordinary
        # Japanese sentences and would over-classify unrelated Facts as places.
        "place": ("\u5730\u65b9", "\u5730\u57df", "\u99c5", "\u7a7a\u6e2f", "\u5e02\u753a\u6751", "place", "location"),
        "product": ("\u5546\u54c1", "\u88fd\u54c1", "\u6a5f\u5668", "\u30a2\u30d7\u30ea", "product"),
        "service": ("\u30b5\u30fc\u30d3\u30b9", "service", "\u30e2\u30c7\u30eb"),
        "brand": ("\u30d6\u30e9\u30f3\u30c9", "brand"),
    }
    person_markers = (
        "\u53cb\u4eba", "\u5bb6\u65cf", "\u540c\u50da", "\u604b\u4eba", "\u77e5\u4eba", "\u76f8\u624b", "\u6bcd", "\u7236", "\u59c9", "\u5144",
        "\u4f1a\u3063\u305f", "\u8a71\u3057\u305f", "\u9023\u7d61", "\u53cb\u9054", "\u540d\u524d\u3092\u547c\u3093\u3060",
        "\u4ea4\u969b", "\u4ed8\u304d\u5408", "\u30c7\u30fc\u30c8", "\u5a5a\u7d04", "\u7d50\u5a5a", "line", "\u7530\u4e2d", "\u5c71\u7530",
        "friend", "colleague", "partner", "met", "contacted",
    )
    named_person = bool(re.search(
        r"[A-Za-z一-龥ぁ-んァ-ヶ]{1,30}(?:さん|くん|君|ちゃん|氏)",
        f"{value_text} {text}",
    ))
    if category == "relationship" and "\u305a\u3093\u3060\u3082\u3093" in text and "\u52d5\u753b" in text:
        return "media_character"
    if category == "relationship" and any(marker in text for marker in marker_groups["media_character"]):
        return "media_character"
    if category == "relationship":
        # Media/project relations are not People, but a person's use of an
        # app, workplace, or venue must not turn the person into a product,
        # organization, or place.
        if any(marker in text for marker in marker_groups["fictional_character"]):
            return "fictional_character"
        if not (named_person or any(marker in text for marker in person_markers)):
            if any(marker in text for marker in marker_groups["project"]):
                return "project"
        if named_person or any(marker in text for marker in person_markers):
            return "person"
    # Fictional/media markers have precedence over generic project/place words
    # for non-relationship domains.
    for entity_type in ("ai_character", "media_character", "fictional_character", "project", "work", "organization", "product", "service", "brand", "place"):
        if any(marker in text for marker in marker_groups[entity_type]):
            return entity_type
    if explicit_hint and explicit_hint != "person":
        return explicit_hint
    if category == "finance" and value_text:
        return "asset"
    if category == "relationship":
        return "unknown"
    return "unknown"


def classify_personal_relevance(fact: dict, source_text: str = "", entity_type: str | None = None) -> str:
    """Classify whether a candidate belongs to this user's Personal OS.

    ``archive_only`` is retained only as provenance; it is never eligible for
    current memory or ordinary retrieval.  ``unknown`` is deliberately kept
    pending rather than promoted to a personal Fact.
    """
    details = fact.get("details") if isinstance(fact.get("details"), dict) else {}
    explicit = str(fact.get("personal_relevance") or details.get("personal_relevance") or "").strip().lower()
    explicit_aliases = {
        "true": "personal", "yes": "personal", "self": "personal",
        "false": "archive_only", "no": "archive_only", "external": "archive_only",
        "reference": "archive_only", "archive": "archive_only",
    }
    explicit = explicit_aliases.get(explicit, explicit)
    category = str(fact.get("category") or "other")
    entity_type = entity_type or classify_entity_type(fact, source_text)
    evidence = user_evidence_text(source_text)
    fallback_text = " ".join(str(item or "") for item in (
        fact.get("summary"), fact.get("asset"), details,
    )).lower()
    has_role_prefix = bool(re.search(r"(?im)^(?:user|assistant):", str(source_text or "")))
    text = evidence.lower() if evidence else ("" if has_role_prefix else fallback_text)
    generic_markers = (
        "一般的", "一般論", "概要", "とは", "ニュース", "報道", "市場全体",
        "企業の売上", "会社の売上", "決算", "公式設定", "キャラクター同士",
        "教えて", "どのような", "どういう", "相場", "シミュレーション", "試算",
        "ガイダンス", "配当利回り", "質問と回答", "何歳", "どれくらい", "計算結果",
        "予測時期", "推奨時期", "一般的な寿命", "目安", "換算", "シナリオ",
        "利用可能である", "作成可能", "定義されている",
        "機能カテゴリ", "指示が与えられた",
        "自分とは関係ない", "私とは関係ない", "無関係",
        "in general", "overview", "news", "company revenue",
    )
    linked_markers = (
        "候補", "比較したい", "比較して", "検討している", "検討中", "どう思う",
        "相談したい", "調べて", "選択肢", "転職先", "引っ越し先",
        "candidate", "considering", "compare",
    )
    # These markers express the user's own state, action, property, or plan.
    personal_markers = (
        "自分", "私", "本人", "俺", "僕", "自宅", "所有", "持っている", "保有",
        "使っている", "住んでいる", "働いている", "勤めている", "行った", "行きたい", "希望",
        "買った", "購入した", "売った", "売却した", "会った", "連絡した",
        "受診した", "診断された", "処方された", "決めた", "選んだ",
        # English first-person markers are handled as word boundaries below;
        # substring matching would mistake the ``i`` in "OpenAI" for the
        # user speaking about themselves.
    )
    explicitly_generic = any(marker.lower() in text for marker in generic_markers)
    english_first_person = bool(re.search(r"\b(?:i|my|i'm)\b", text))
    if not explicitly_generic and (any(marker.lower() in text for marker in personal_markers) or english_first_person):
        return "personal"
    # Some Japanese personal statements omit the pronoun but contain an
    # unambiguous first-person action or domain-specific state.
    implicit_personal = {
        "finance": (
            "入れた", "積立", "購入した", "買った", "売却した", "売った", "持株会", "支払った",
            "総資産", "年収", "給与", "現金残高", "保有している", "受け取った",
        ),
        "health": ("症状", "処方", "受診", "検査結果", "診断された"),
        "work": ("年収", "職種", "休日", "働き方", "勤務"),
        "travel": ("訪問した", "旅行した", "行きたい", "予約した", "旅行予定", "旅費", "マイル", "温泉が好き"),
        "housing": ("家賃", "間取り", "最寄駅", "更新", "引っ越した"),
        "relationship": ("友人", "家族", "同僚", "恋人", "会った", "連絡した", "交際", "付き合い", "デート"),
        "lifestyle": ("習慣", "生活費", "疲労度", "睡眠"),
        "hobby": ("好き", "趣味", "推し", "集めている"),
        "food": ("食べた", "好きな料理", "好物"),
    }
    if category == "relationship" and any(marker in text for marker in ("\u6bcd", "\u7530\u4e2d", "\u5c71\u7530", "line", "\u3055\u3093\u3068", "\u3055\u3093\u304b\u3089")):
        return "personal"
    if not explicitly_generic and any(marker.lower() in text for marker in implicit_personal.get(category, ())):
        return "personal"
    if category == "reference":
        return "archive_only"
    if any(marker.lower() in text for marker in linked_markers):
        return "linked_context"
    if explicitly_generic:
        return "archive_only"
    if explicit == "linked_context":
        return "linked_context"
    if entity_type in {
        "organization", "place", "product", "service", "brand", "project", "work",
        "fictional_character", "media_character", "ai_character", "AI_character",
    }:
        return "archive_only"
    if explicit == "archive_only":
        return "archive_only"
    # The model's ``personal_relevance=true`` is a hint, not Evidence.
    # Personal promotion requires an explicit user state/action marker above.
    return "unknown"


def _subject_scope(entity_type: str) -> str:
    if entity_type == "person":
        return "person"
    if entity_type in {"fictional_character", "media_character", "ai_character", "AI_character"}:
        return "fictional_character"
    if entity_type == "asset":
        return "asset"
    if entity_type in {"organization", "project", "work", "place", "product", "service", "brand"}:
        return "reference"
    return "unknown"


def _quality_reclassification(category: str, entity_type: str, text: str) -> str | None:
    if category == "relationship" and entity_type not in {"person", "unknown"}:
        personal_markers = ("好き", "推し", "趣味", "見た", "聴いた", "集め", "グッズ")
        return "hobby" if any(marker in text for marker in personal_markers) else "reference"
    if category == "housing":
        housing_markers = ("家賃", "間取り", "物件", "住居", "引っ越", "最寄", "更新時期", "広さ")
        item_markers = ("マットレス", "家具", "家電", "ベッド", "ソファ", "冷蔵庫", "洗濯機")
        if any(marker in text for marker in item_markers) and not any(marker in text for marker in housing_markers):
            return "shopping"
    return None


def is_numeric_outlier(candidate: float | int | None, history: list[float | int]) -> bool:
    """Detect an order-of-magnitude error without rejecting ordinary change.

    Two prior values are required. A tenfold deviation is intentionally much
    more conservative than the old pairwise 2x warning and is used as a gate,
    not as proof that the candidate is wrong.
    """
    try:
        number = abs(float(candidate))
    except (TypeError, ValueError):
        return False
    peers = []
    for value in history:
        try:
            peer = abs(float(value))
        except (TypeError, ValueError):
            continue
        if peer > 0:
            peers.append(peer)
    if number <= 0 or len(peers) < 2:
        return False
    baseline = float(statistics.median(peers))
    if baseline <= 0:
        return False
    ratio = max(number / baseline, baseline / number)
    return ratio >= 10.0 and abs(number - baseline) >= max(10_000.0, baseline * 5.0)


def fact_has_numeric_outlier(connection: sqlite3.Connection, row: sqlite3.Row, value: dict) -> bool:
    amount = value.get("amount")
    if amount is None or not row["fact_key"]:
        return False
    candidate = normalize_transaction_amount(amount, value.get("currency"), row["summary"]).get("normalized_amount")
    peers = connection.execute(
        """SELECT other.value_json,other.summary
           FROM facts other JOIN fact_reviews review ON review.fact_id=other.id
           WHERE other.id!=? AND other.fact_key=? AND review.state='confirmed'
             AND COALESCE(other.personal_relevance,'unknown')='personal'""",
        (row["id"], row["fact_key"]),
    ).fetchall()
    history: list[float] = []
    for peer in peers:
        peer_value = _fact_value(peer)
        normalized = normalize_transaction_amount(
            peer_value.get("amount"), peer_value.get("currency"), peer["summary"]
        ).get("normalized_amount")
        if normalized is not None:
            history.append(float(normalized))
    return is_numeric_outlier(candidate, history)


def _record_memory_correction(connection: sqlite3.Connection, *, fact_id: int | None,
                               entity_id: int | None, correction_type: str,
                               before: dict, after: dict, reason: str,
                               source: str = "automatic") -> None:
    if before == after:
        return
    before_json = json.dumps(before, ensure_ascii=False, sort_keys=True)
    after_json = json.dumps(after, ensure_ascii=False, sort_keys=True)
    clipped_reason = reason[:1000]
    duplicate = connection.execute(
        """SELECT 1 FROM memory_corrections
           WHERE fact_id IS ? AND entity_id IS ? AND correction_type=?
             AND before_json=? AND after_json=? AND reason=? AND source=? AND quality_version=?
           LIMIT 1""",
        (fact_id, entity_id, correction_type, before_json, after_json, clipped_reason, source, MEMORY_QUALITY_VERSION),
    ).fetchone()
    if duplicate:
        return
    connection.execute(
        """INSERT INTO memory_corrections(fact_id,entity_id,correction_type,before_json,after_json,reason,source,quality_version,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (fact_id, entity_id, correction_type, before_json, after_json, clipped_reason, source, MEMORY_QUALITY_VERSION, now()),
    )


def apply_memory_quality_to_fact(connection: sqlite3.Connection, fact_id: int, source: str = "automatic") -> dict[str, object]:
    row = connection.execute(
        """SELECT f.*,r.state AS review_state,r.reason AS review_reason,r.review_note,r.reviewed_at,
                  c.text AS evidence,c.is_active AS source_chunk_active,
                  d.source AS document_source,e.entity_type AS existing_entity_type,
                  e.canonical_name AS entity_name
           FROM facts f LEFT JOIN fact_reviews r ON r.fact_id=f.id
           LEFT JOIN chunks c ON c.id=f.source_chunk_id
           LEFT JOIN documents d ON d.id=f.document_id
           LEFT JOIN entities e ON e.id=f.subject_entity_id
           WHERE f.id=?""", (fact_id,)
    ).fetchone()
    if not row:
        return {"fact_id": fact_id, "changed": False, "reason": "not_found"}
    value = _fact_value(row)
    details = value.get("details") if isinstance(value.get("details"), dict) else {}
    fact = {"category": row["category"], "type": row["fact_type"], "summary": row["summary"],
            "asset": value.get("asset"), "amount": value.get("amount"), "details": details,
            "evidence_strength": details.get("evidence_strength"), "confidence": row["confidence"],
            "occurred_on": row["occurred_on"]}
    evidence_text = row["evidence"] or ""
    trust = fact_trust_evaluation(connection, fact_id, fact, row["created_at"])
    entity_type = classify_entity_type(fact, evidence_text)
    personal_relevance = classify_personal_relevance(fact, evidence_text, entity_type)
    subject_scope = _subject_scope(entity_type)
    text = " ".join(str(item or "") for item in (row["summary"], value.get("asset"), details, evidence_text)).lower()
    new_category = _quality_reclassification(row["category"], entity_type, text)
    review_state = row["review_state"] or "pending"
    review_note = str(row["review_note"] or "")
    # Only an explicit user marker protects a review from automatic
    # re-evaluation.  Older AI-generated notes often had reviewed_at populated,
    # so timestamp/non-empty-note alone is not proof of human confirmation.
    manual_review = bool(
        "ユーザー" in str(row["review_reason"] or "")
        or "ユーザー" in review_note
    )
    validation_status = "confirmed" if review_state == "confirmed" else "pending"
    validation_reason = "EvidenceとEntity種別を確認済み"
    eligibility = "eligible" if review_state == "confirmed" else "pending"
    source_is_coarse = (
        row["document_source"] == "chatgpt-export"
        and not row["source_attachment_id"]
        and (row["source_chunk_id"] is None or row["source_chunk_active"] != 1)
    )
    if personal_relevance == "archive_only":
        validation_status, validation_reason, eligibility = "excluded", "Personal OSの正本ではない外部情報のため保管のみ", "excluded"
        if not manual_review:
            review_state = "rejected"
    elif personal_relevance == "linked_context":
        validation_status, validation_reason, eligibility = "context", "本人の比較・検討に関係する参考情報（原文検索のみ）", "pending"
        if not manual_review:
            review_state = "rejected"
    elif personal_relevance == "unknown":
        validation_status, validation_reason, eligibility = "pending", "本人との関係をEvidenceから確定できないため保留", "pending"
    if source_is_coarse and personal_relevance != "archive_only":
        validation_status, validation_reason, eligibility = "pending", "会話チャンクの根拠が粗いため再解析待ち", "pending"
    if row["category"] == "reference" or fact_policy(fact) == "exclude":
        validation_status, validation_reason, eligibility = "excluded", "明確な非FactまたはAI推測", "excluded"
    if entity_type == "unknown" and row["category"] == "relationship":
        old_unknown_rejection = (
            review_state == "rejected"
            and "Entity種別=unknown" in str(row["review_reason"] or row["review_note"] or "")
        )
        if old_unknown_rejection:
            review_state = "pending"
            connection.execute(
                """UPDATE fact_reviews
                   SET state='pending',reason=?,review_note=?,reviewed_at=?
                   WHERE fact_id=?""",
                ("旧ルールによるunknown自動却下を解除", "Entity種別を再確認", now(), fact_id),
            )
        validation_status = "pending"
        validation_reason = "人物Factの可能性があるがEntity種別を確定できないため保留"
        eligibility = "pending"
    elif entity_type != "person" and row["category"] == "relationship":
        validation_status = "reclassified"
        validation_reason = f"relationshipではなくEntity種別={entity_type}"
        eligibility = "excluded"
        review_state = "rejected"
        if new_category:
            new_category = ensure_memory_category(connection, new_category)
    if review_state == "rejected":
        validation_status, eligibility = "excluded", "excluded"
    if personal_relevance == "personal" and eligibility != "excluded" and not source_is_coarse and not manual_review:
        if trust["contradiction_count"]:
            review_state = "pending"
            validation_status = "conflict"
            validation_reason = "独立した反証Evidenceがあるため自動解決できません"
            eligibility = "conflict"
        elif trust["explicit"] and float(trust["score"]) >= 0.68:
            review_state = "confirmed"
            validation_status = "confirmed"
            validation_reason = "明示Evidenceと独立性を評価して自動確定"
            eligibility = "eligible"
        elif eligibility != "excluded":
            review_state = "pending"
            validation_status = "pending"
            validation_reason = "Evidenceの明示性または信頼スコアが自動確定基準未満"
            eligibility = "pending"
    if review_state in {"pending", "deferred"} and eligibility not in {"excluded", "conflict"}:
        eligibility = "pending"
    if eligibility == "eligible" and fact_has_numeric_outlier(connection, row, value):
        validation_status = "conflict"
        validation_reason = "同一Factの過去値から桁違いに外れているため自動採用を保留"
        eligibility = "conflict"
    extraction_confidence = float(row["confidence"] or 0.0)
    truth_confidence = float(trust["score"])
    before_entity_type = row["resolved_entity_type"] or row["existing_entity_type"] or "unknown"
    before_fact = {
        "category": row["category"], "subject_scope": row["subject_scope"],
        "personal_relevance": row["personal_relevance"],
        "validation_status": row["validation_status"], "retrieval_eligibility": row["retrieval_eligibility"],
        "review_state": row["review_state"], "entity_type": before_entity_type,
        "trust_score": float(row["truth_confidence"] or 0.0),
        "support_count": int(row["evidence_support_count"] or 0),
        "contradiction_count": int(row["evidence_contradiction_count"] or 0),
    }
    if new_category:
        connection.execute("UPDATE facts SET category=? WHERE id=?", (new_category, fact_id))
        fact_key = canonical_fact_key(new_category, row["fact_type"], value.get("asset"), details, row["summary"])
        connection.execute("UPDATE facts SET fact_key=? WHERE id=?", (fact_key, fact_id))
        # Re-run currentness after changing the domain.  A reclassified
        # reference must not remain a current People Fact.
        apply_fact_timeline(connection, fact_id)
    if review_state != row["review_state"] and not manual_review:
        connection.execute(
            """UPDATE fact_reviews SET state=?,reason=?,review_note=?,reviewed_at=? WHERE fact_id=?""",
            (review_state, validation_reason, "自動判定（Evidence信頼評価）",
             now() if review_state in {"confirmed", "rejected"} else None, fact_id),
        )
    connection.execute(
        """UPDATE facts SET subject_scope=?,resolved_entity_type=?,personal_relevance=?,extraction_confidence=?,truth_confidence=?,validation_status=?,
                  validation_reason=?,validated_at=?,retrieval_eligibility=?,evidence_support_count=?,
                  evidence_contradiction_count=?,trust_details_json=?,trust_updated_at=? WHERE id=?""",
        (subject_scope, entity_type, personal_relevance, extraction_confidence, truth_confidence, validation_status,
         validation_reason, now(), eligibility, trust["support_count"], trust["contradiction_count"],
         json.dumps(trust, ensure_ascii=False), now(), fact_id),
    )
    entity_id = row["subject_entity_id"]
    if entity_id and entity_type in ENTITY_TYPES and entity_type != "unknown":
        existing_type = row["existing_entity_type"] or "unknown"
        if existing_type != entity_type:
            existing = connection.execute("SELECT id FROM entities WHERE entity_type=? AND canonical_name=?", (entity_type, row["entity_name"])).fetchone()
            if not existing:
                connection.execute(
                    "INSERT OR IGNORE INTO entities(entity_type,canonical_name,created_at,updated_at) VALUES(?,?,?,?)",
                    (entity_type, row["entity_name"], now(), now()),
                )
                existing = connection.execute(
                    "SELECT id FROM entities WHERE entity_type=? AND canonical_name=?",
                    (entity_type, row["entity_name"]),
                ).fetchone()
            if existing:
                connection.execute("UPDATE facts SET subject_entity_id=? WHERE id=?", (existing["id"], fact_id))
                entity_id = existing["id"]
            _record_memory_correction(connection, fact_id=fact_id, entity_id=entity_id, correction_type="entity_resolution",
                                       before={"entity_type": existing_type}, after={"entity_type": entity_type}, reason=validation_reason, source=source)
    mention_text = str(value.get("asset") or row["summary"] or "")[:400]
    if mention_text:
        exists = connection.execute("SELECT 1 FROM entity_mentions WHERE fact_id=? AND mention_text=?", (fact_id, mention_text)).fetchone()
        if exists:
            connection.execute(
                """UPDATE entity_mentions
                   SET resolved_entity_id=?,entity_type=?,resolution_status=?,confidence=?
                   WHERE fact_id=? AND mention_text=?""",
                (entity_id, entity_type, "resolved" if entity_type != "unknown" else "ambiguous",
                 truth_confidence, fact_id, mention_text),
            )
        else:
            connection.execute(
                """INSERT INTO entity_mentions(fact_id,document_id,chunk_id,mention_text,resolved_entity_id,entity_type,resolution_status,confidence,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (fact_id, row["document_id"], row["source_chunk_id"], mention_text, entity_id, entity_type,
                 "resolved" if entity_type != "unknown" else "ambiguous", truth_confidence, now()),
            )
    after_fact = {
        "category": new_category or row["category"], "subject_scope": subject_scope,
        "personal_relevance": personal_relevance,
        "validation_status": validation_status, "retrieval_eligibility": eligibility,
        "review_state": review_state, "entity_type": entity_type, "trust_score": truth_confidence,
        "support_count": trust["support_count"], "contradiction_count": trust["contradiction_count"],
    }
    _record_memory_correction(connection, fact_id=fact_id, entity_id=entity_id, correction_type="fact_quality",
                               before=before_fact, after=after_fact, reason=validation_reason, source=source)
    return {"fact_id": fact_id, "changed": before_fact != after_fact, **after_fact, "reason": validation_reason}


def audit_memory_quality() -> dict[str, int]:
    """Re-evaluate persisted facts without deleting their source material."""
    counts = {"scanned": 0, "changed": 0, "reclassified": 0, "excluded": 0, "eligible": 0, "pending": 0}
    with db() as connection:
        rows = connection.execute("SELECT id FROM facts ORDER BY id").fetchall()
        for row in rows:
            result = apply_memory_quality_to_fact(connection, row["id"])
            counts["scanned"] += 1
            if result.get("changed"):
                counts["changed"] += 1
            if result.get("validation_status") == "reclassified":
                counts["reclassified"] += 1
            if result.get("retrieval_eligibility") == "excluded":
                counts["excluded"] += 1
            elif result.get("retrieval_eligibility") == "eligible":
                counts["eligible"] += 1
            else:
                counts["pending"] += 1
    return counts


def repair_memory_state(reason: str = "manual") -> dict[str, object]:
    """Run the existing-data repair pipeline without deleting raw Evidence."""
    with db() as connection:
        running = connection.execute("SELECT id FROM repair_jobs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
        if running:
            return {"job_id": running["id"], "status": "running", "message": "既に修復Jobが実行中です。"}
        cursor = connection.execute(
            "INSERT INTO repair_jobs(status,reason,started_at) VALUES('running',?,?)",
            (str(reason or "manual")[:500], now()),
        )
        job_id = int(cursor.lastrowid)
    try:
        backfill_fact_keys()
        migrate_current_truth()
        evidence_created = backfill_fact_evidence()
        quality = audit_memory_quality()
        reevaluate_finance_transactions()
        queued = queue_analysis_jobs()
        with db() as connection:
            connection.execute(
                """UPDATE repair_jobs SET status='completed',scanned_count=?,changed_count=?,reclassified_count=?,
                   excluded_count=?,finished_at=?,error=? WHERE id=?""",
                (quality.get("scanned", 0), quality.get("changed", 0), quality.get("reclassified", 0),
                 quality.get("excluded", 0), now(), f"evidence_created={evidence_created}; queued={queued}", job_id),
            )
        return {"job_id": job_id, "status": "completed", **quality, "evidence_created": evidence_created, "queued": queued}
    except Exception as error:
        with db() as connection:
            connection.execute(
                "UPDATE repair_jobs SET status='failed',finished_at=?,error=? WHERE id=?",
                (now(), str(error)[:2000], job_id),
            )
        raise


def prepare_conversation_reanalysis(*, queue_jobs: bool = True) -> dict[str, int]:
    """Create turn-level source revisions and quarantine coarse ChatGPT Facts.

    Nothing is deleted.  Facts whose provenance points to an inactive legacy
    chunk remain available in correction history but cannot enter retrieval
    until a turn-level Job reanchors or replaces them.
    """
    resegmented = 0
    quarantined = 0
    with db() as connection:
        entry_ids = [row["id"] for row in connection.execute(
            "SELECT id FROM entries WHERE source='chatgpt-export' ORDER BY id"
        )]
    for entry_id in entry_ids:
        ensure_document_for_entry(entry_id)
        resegmented += 1
    with db() as connection:
        rows = connection.execute(
            """SELECT f.id,f.source_chunk_id,f.retrieval_eligibility,f.validation_status
               FROM facts f JOIN documents d ON d.id=f.document_id
               LEFT JOIN chunks c ON c.id=f.source_chunk_id
               WHERE d.source='chatgpt-export'
                 AND (f.source_chunk_id IS NULL OR COALESCE(c.is_active,0)=0)
                 AND (COALESCE(f.retrieval_eligibility,'pending')='eligible' OR COALESCE(f.validation_status,'pending')='confirmed')"""
        ).fetchall()
        for row in rows:
            connection.execute(
                """UPDATE facts SET validation_status='pending',validation_reason=?,retrieval_eligibility='pending',truth_confidence=MIN(COALESCE(truth_confidence,0.0),0.49) WHERE id=?""",
                ("会話の根拠が発言単位でないため再解析待ち", row["id"]),
            )
            _record_memory_correction(
                connection, fact_id=row["id"], entity_id=None,
                correction_type="source_quarantine",
                before={"source_chunk_id": row["source_chunk_id"], "retrieval_eligibility": row["retrieval_eligibility"]},
                after={"retrieval_eligibility": "pending"},
                reason="粗い会話チャンクをPersonal OSの検索対象から隔離", source="resegmentation",
            )
            quarantined += 1
    queued = queue_analysis_jobs() if queue_jobs else 0
    return {"documents": resegmented, "quarantined": quarantined, "queued": queued}


def is_transaction_candidate(fact: dict) -> bool:
    """Allow the finance validator to see transaction candidates without trusting them."""
    return str(fact.get("category", "")) == "finance" and str(fact.get("type", "")) == "transaction"


def create_memory_proposal(entry_id: int, facts: list[dict]) -> dict | None:
    """Stage only Facts that remain pending after evidence-based evaluation."""
    with db() as connection:
        pending_keys = {
            row["fact_key"] for row in connection.execute(
                """SELECT f.fact_key FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
                   WHERE f.document_id IN (SELECT id FROM documents WHERE legacy_entry_id=?) AND r.state='pending'""",
                (entry_id,),
            )
        }
    protected = []
    for fact in facts:
        if fact_policy(fact) == "exclude" or classify_personal_relevance(fact) == "archive_only":
            continue
        category = normalize_category_slug(fact.get("category", "other"))
        fact_type = str(fact.get("type", "note"))[:80]
        details = fact.get("details", {}) if isinstance(fact.get("details", {}), dict) else {}
        key = canonical_fact_key(category, fact_type, fact.get("asset"), details, str(fact.get("summary", "")))
        if key in pending_keys:
            protected.append(fact)
    if not protected:
        return None
    policy = "confirm"
    with db() as connection:
        cursor = connection.execute(
            "INSERT INTO memory_proposals(entry_id,facts_json,policy,created_at) VALUES(?,?,?,?)",
            (entry_id, json.dumps(protected, ensure_ascii=False), policy, now()),
        )
    return {"id": cursor.lastrowid, "policy": policy, "facts": protected}


def normalize_key_part(value: object) -> str:
    text = re.sub(r"[\s　\-_・/]+", "", str(value or "").lower())
    replacements = {
        "積立投資": "monthly_investment",
        "毎月積立": "monthly_investment",
        "月間積立": "monthly_investment",
        "持株会": "employee_stock_plan",
        "総資産": "total_assets",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text[:120] or "total"


def canonical_fact_key(category: str, fact_type: str, asset: object, details: object, summary: str) -> str:
    """Identify a concept by domain, predicate and scope, never by its value."""
    detail_scope = details.get("scope") if isinstance(details, dict) else None
    scope = normalize_key_part(detail_scope or asset)
    if category == "finance" and fact_type == "plan":
        recurring = any(word in summary for word in ("毎月", "月間", "積立")) or "monthly" in scope
        predicate = "monthly_investment" if recurring else "plan"
        return f"finance.{predicate}.{scope}"
    if category == "finance" and fact_type == "asset_balance":
        return f"finance.asset_balance.{scope}"
    if category == "finance" and fact_type == "income":
        return f"finance.income.{scope}"
    return f"{normalize_key_part(category)}.{normalize_key_part(fact_type)}.{scope}"


def backfill_fact_keys() -> None:
    with db() as connection:
        rows = connection.execute("SELECT id,category,fact_type,value_json,summary FROM facts WHERE fact_key IS NULL OR fact_key=''").fetchall()
        for row in rows:
            value = json.loads(row["value_json"] or "{}")
            connection.execute(
                "UPDATE facts SET fact_key=?, extractor_model=COALESCE(extractor_model, extractor), prompt_version=COALESCE(prompt_version, 'legacy-v1'), extracted_at=COALESCE(extracted_at, created_at) WHERE id=?",
                (canonical_fact_key(row["category"], row["fact_type"], value.get("asset"), value.get("details"), row["summary"]), row["id"]),
            )


def fact_timeline_kind(row: sqlite3.Row) -> tuple[str, str | None]:
    """Return a temporal state and identity key for the fact."""
    fact_type = row["fact_type"]
    if fact_type in HISTORICAL_FACT_TYPES:
        return "historical", None
    if fact_type not in MUTABLE_FACT_TYPES:
        return "unknown", None
    return "current", row["fact_key"] or f"{row['category']}:{fact_type}:{row['subject_entity_id'] or 'total'}"


def _timeline_temporal_value(row: sqlite3.Row) -> str:
    """Return the best content-time value, never using insertion order first."""
    for key in ("effective_at", "observed_at", "source_created_at", "occurred_on", "created_at"):
        value = row[key] if key in row.keys() else None
        if value:
            return str(value)
    return ""


def timeline_start(row: sqlite3.Row) -> str:
    return _timeline_temporal_value(row)


def _timeline_payload(row: sqlite3.Row) -> object:
    try:
        payload = json.loads(row["value_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = row["value_json"]
    return canonical_factual_payload(payload)


_FACT_METADATA_KEYS = {
    "note", "notes", "comment", "comments", "debug", "debug_info", "reason",
    "confidence", "extraction_confidence", "evidence_quote", "evidence_strength",
    "source_text", "source_chunk_id", "source_attachment_id", "extractor",
    "extractor_model", "prompt_version", "ai_reasoning", "analysis_reasoning",
}


def canonical_factual_payload(payload: object) -> object:
    """Keep semantic Fact value fields while ignoring extraction metadata.

    Two extractions with the same amount/asset/scope but different notes or
    model diagnostics are the same Fact value and must not become a conflict.
    The original ``value_json`` remains untouched for auditability.
    """
    if isinstance(payload, dict):
        cleaned: dict[str, object] = {}
        for key, value in payload.items():
            normalized_key = str(key)
            if normalized_key.lower() in _FACT_METADATA_KEYS:
                continue
            cleaned[normalized_key] = canonical_factual_payload(value)
        return cleaned
    if isinstance(payload, list):
        return [canonical_factual_payload(item) for item in payload]
    return payload


def _rebuild_fact_timeline(connection: sqlite3.Connection, current_key: str, log_change: bool = True) -> None:
    """Reconstruct one mutable Fact series from temporal evidence.

    The newest effective/observed/source date wins. Creation/import order is
    only a final tie-breaker for identical values. Conflicting values on the
    same effective date are deliberately left without a Current state.
    """
    rows = connection.execute(
        """SELECT f.*,d.source_created_at AS source_created_at
           FROM facts f JOIN documents d ON d.id=f.document_id
           WHERE f.fact_key=? AND f.fact_type IN ('plan','schedule','preference','status','income','asset_balance','holding','goal')
           ORDER BY COALESCE(f.effective_at,f.observed_at,d.source_created_at,f.occurred_on,f.created_at),f.created_at,f.id""",
        (current_key,),
    ).fetchall()
    if not rows:
        return
    before_current = next((row["id"] for row in rows if row["status"] == "current"), None)
    dated = [(row, timeline_start(row)) for row in rows]
    latest_value = max(value for _, value in dated)
    latest = [row for row, value in dated if value == latest_value]
    payloads = {_stable_json(_timeline_payload(row)) for row in latest}
    conflict = len(latest) > 1 and len(payloads) > 1
    chosen = None
    if not conflict:
        chosen = max(latest, key=lambda row: (str(row["created_at"] or ""), int(row["id"])))
    predecessor = None
    if chosen:
        earlier = [row for row, value in dated if value < latest_value]
        if earlier:
            predecessor = max(earlier, key=lambda row: (timeline_start(row), int(row["id"])))
    for row, temporal_value in dated:
        if conflict and row in latest:
            status, valid_to, replaced_by, state = "unknown", None, None, "unknown"
        elif chosen is not None and row["id"] == chosen["id"]:
            status, valid_to, replaced_by, state = "current", None, None, "current"
        else:
            status = "superseded"
            newer_values = sorted({value for _, value in dated if value and value > temporal_value})
            # Close a historical interval at the next known effective date,
            # not at the newest date in the entire series.  Same-day duplicate
            # values are retained with a zero-length boundary (never invalid).
            valid_to = (newer_values[0] if newer_values else latest_value) if chosen else None
            replaced_by = chosen["id"] if chosen else None
            state = "superseded"
        supersedes = predecessor["id"] if chosen is not None and row["id"] == chosen["id"] and predecessor else None
        connection.execute(
            """UPDATE facts SET status=?,valid_from=?,valid_to=?,supersedes_fact_id=? WHERE id=?""",
            (status, temporal_value or None, valid_to, supersedes, row["id"]),
        )
        connection.execute(
            """INSERT INTO fact_currentness(fact_id,state,current_key,valid_from,valid_until,replaced_by_fact_id,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(fact_id) DO UPDATE SET state=excluded.state,current_key=excluded.current_key,
                 valid_from=excluded.valid_from,valid_until=excluded.valid_until,
                 replaced_by_fact_id=excluded.replaced_by_fact_id,updated_at=excluded.updated_at""",
            (row["id"], state, current_key, temporal_value or None, valid_to, replaced_by, now()),
        )
    if not log_change:
        return
    after_current = chosen["id"] if chosen else None
    if before_current != after_current:
        if after_current:
            current_row = next(row for row in rows if row["id"] == after_current)
            detail = {"current_fact_id": after_current, "temporal_value": latest_value, "conflict": False}
            change_type = "updated" if before_current else "added"
            connection.execute(
                "INSERT INTO memory_changes(fact_id,previous_fact_id,change_type,summary,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                (after_current, before_current, change_type, f"時系列Current: {current_row['summary']}", json.dumps(detail, ensure_ascii=False), now()),
            )
        elif conflict:
            connection.execute(
                "INSERT INTO memory_changes(fact_id,previous_fact_id,change_type,summary,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                (rows[0]["id"], before_current, "conflict", "同一有効日に異なるFactがあるためCurrentを保留", json.dumps({"fact_key": current_key, "fact_ids": [row["id"] for row in latest]}, ensure_ascii=False), now()),
            )


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def apply_fact_timeline(connection: sqlite3.Connection, fact_id: int, log_change: bool = True) -> None:
    """Rebuild the complete Fact series instead of replacing by insert order."""
    row = connection.execute(
        "SELECT f.*,d.source_created_at AS source_created_at FROM facts f JOIN documents d ON d.id=f.document_id WHERE f.id=?",
        (fact_id,),
    ).fetchone()
    if not row:
        return
    state, current_key = fact_timeline_kind(row)
    if state == "current" and current_key:
        _rebuild_fact_timeline(connection, current_key, log_change=log_change)
        return
    started_at = timeline_start(row)
    connection.execute("UPDATE facts SET status=?,valid_from=?,valid_to=NULL WHERE id=?", (state, started_at or None, fact_id))
    connection.execute(
        """INSERT INTO fact_currentness(fact_id,state,current_key,valid_from,valid_until,replaced_by_fact_id,updated_at)
           VALUES(?,?,?,?,?,?,?) ON CONFLICT(fact_id) DO UPDATE SET state=excluded.state,current_key=excluded.current_key,
             valid_from=excluded.valid_from,valid_until=excluded.valid_until,replaced_by_fact_id=NULL,updated_at=excluded.updated_at""",
        (fact_id, state, None, started_at or None, None, None, now()),
    )


def migrate_current_truth() -> None:
    """Rebuild every mutable series without creating noisy visible changes."""
    with db() as connection:
        connection.execute(
            "UPDATE facts SET effective_at=occurred_on,temporal_source='explicit_date' WHERE (effective_at IS NULL OR effective_at='') AND occurred_on IS NOT NULL AND occurred_on!=''"
        )
        rows = connection.execute(
            "SELECT DISTINCT fact_key FROM facts WHERE fact_type IN ('plan','schedule','preference','status','income','asset_balance','holding','goal') AND fact_key IS NOT NULL AND fact_key!=''"
        ).fetchall()
        for row in rows:
            _rebuild_fact_timeline(connection, row["fact_key"], log_change=False)
        other_rows = connection.execute(
            "SELECT id FROM facts WHERE fact_type NOT IN ('plan','schedule','preference','status','income','asset_balance','holding','goal') AND id NOT IN (SELECT fact_id FROM fact_currentness)"
        ).fetchall()
        for row in other_rows:
            apply_fact_timeline(connection, row["id"], log_change=False)


BENCHMARK_STAT_TYPES = {"mean", "median", "percentile", "proportion", "count", "distribution", "index"}
BENCHMARK_COMPATIBILITY = {"exact", "comparable", "reference_only", "incompatible"}
# A comparison defaults to the exact metric key. Total assets and financial
# assets (or individual and household measures) are never interchangeable.
# Add a new key only after its full definition/scope contract is reviewed.
BENCHMARK_METRIC_CONTRACTS: dict[str, dict[str, object]] = {
    "finance.total_assets": {"personal_fact_keys": ("finance.total_assets", "finance.asset_balance.total_assets"), "canonical_unit": "JPY", "statistical_unit": "individual", "measurement_kind": "balance", "time_basis": "current"},
    "finance.financial_assets": {"personal_fact_keys": ("finance.financial_assets", "finance.asset_balance.financial_assets"), "canonical_unit": "JPY", "statistical_unit": "individual", "measurement_kind": "balance", "time_basis": "current"},
    "work.annual_income": {"personal_fact_keys": ("work.annual_income",), "canonical_unit": "JPY", "statistical_unit": "individual", "measurement_kind": "flow", "time_basis": "annual"},
    "housing.monthly_rent": {"personal_fact_keys": ("housing.monthly_rent",), "canonical_unit": "JPY", "statistical_unit": "individual", "measurement_kind": "flow", "time_basis": "monthly"},
    "life.sleep_duration": {"personal_fact_keys": ("life.sleep_duration",), "canonical_unit": "hours", "statistical_unit": "individual", "measurement_kind": "duration", "time_basis": "daily"},
}
BENCHMARK_CONTRACT_FIELDS = ("metric_key", "statistical_unit", "measurement_kind", "time_basis", "canonical_unit")


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def benchmark_metric_contract(metric_key: str, supplied: object = None) -> dict[str, object]:
    """Return a complete, reviewed contract or reject an underspecified one."""
    contract = dict(BENCHMARK_METRIC_CONTRACTS.get(metric_key, {}))
    contract.update(_json_object(supplied))
    contract["metric_key"] = str(contract.get("metric_key") or metric_key)
    if not contract.get("personal_fact_keys"):
        contract["personal_fact_keys"] = (metric_key,)
    missing = [field for field in BENCHMARK_CONTRACT_FIELDS if not str(contract.get(field, "")).strip()]
    if missing:
        raise ValueError("metric contract requires " + ", ".join(missing))
    return contract


def resolve_personal_metric_contract(fact_key: str | None) -> dict[str, object] | None:
    """Resolve a reviewed personal fact key without inferring from a summary.

    This is intentionally a one-way Registry lookup.  It never maps a broad
    asset fact to financial assets, or an individual fact to household data.
    """
    key = str(fact_key or "").strip()
    if not key:
        return None
    for metric_key, definition in BENCHMARK_METRIC_CONTRACTS.items():
        if key in tuple(definition.get("personal_fact_keys", ())):
            return benchmark_metric_contract(metric_key, definition)
    return None


def embedded_contract_matches_registry(embedded: dict[str, object], registry: dict[str, object]) -> bool:
    """Check saved diagnostic metadata without granting it any authority."""
    return all(str(embedded.get(field, "")) == str(registry.get(field, "")) for field in BENCHMARK_CONTRACT_FIELDS)


def normalize_benchmark_value(value: object, unit: object, canonical_unit: object) -> tuple[float | None, str | None]:
    """Normalize only an explicit unit; guessing units makes comparisons unsafe."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None, None
    source = str(unit or "").strip().lower()
    target = str(canonical_unit or "").strip().lower()
    money = {"jpy": 1.0, "円": 1.0, "¥": 1.0, "万円": 10000.0, "万": 10000.0, "億円": 100000000.0, "億": 100000000.0}
    duration = {"hours": 1.0, "hour": 1.0, "h": 1.0, "時間": 1.0, "minutes": 1 / 60, "minute": 1 / 60, "min": 1 / 60, "分": 1 / 60}
    factors = money if target == "jpy" else duration if target == "hours" else {target: 1.0}
    if source not in factors:
        return None, None
    return numeric * factors[source], str(canonical_unit)


def benchmark_percentile_band(distribution: object, personal_value: object) -> str | None:
    """Return a band, never a fabricated percentile point."""
    points = _json_object(distribution)
    required = ("p10", "p25", "p50", "p75", "p90")
    try:
        values = [float(points[key]) for key in required]
        personal = float(personal_value)
    except (KeyError, TypeError, ValueError):
        return None
    if values != sorted(values):
        return None
    labels = ("below_p10", "p10_p25", "p25_p50", "p50_p75", "p75_p90", "above_p90")
    return next((labels[index] for index, boundary in enumerate(values) if personal < boundary), labels[-1])


def benchmark_comparison_group_key(series: dict[str, object]) -> str:
    """Return a stable group key only when comparison conditions are identical.

    Statistic type is deliberately excluded: mean and median describe the same
    population context and belong on one card.  Every scope, period, source,
    and measurement distinction is retained so unlike series cannot be
    visually blended into a false comparison.
    """
    latest = (series.get("observations") or [{}])[0]
    contract = _json_object(series.get("metric_contract"))
    context = {
        "metric_key": series.get("metric_key"),
        "source_url": series.get("source_url"),
        "publisher": series.get("publisher"),
        "population_scope": series.get("population_scope"),
        "geography": series.get("geography"),
        "segment_definition": _json_object(series.get("segment_definition")),
        "reference_period": latest.get("reference_period") if isinstance(latest, dict) else None,
        "version": series.get("version"),
        "statistical_unit": contract.get("statistical_unit"),
        "measurement_kind": contract.get("measurement_kind"),
        "time_basis": contract.get("time_basis"),
        "canonical_unit": contract.get("canonical_unit"),
    }
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "benchmark:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
PERSONAL_SPACE_COLORS = {
    "finance": "#22C55E", "travel": "#38BDF8", "housing": "#F59E0B", "relationship": "#F472B6",
    "work": "#6366F1", "health": "#EF4444", "life": "#EAB308", "lifestyle": "#EAB308",
    "learning": "#14B8A6", "hobby": "#A855F7", "food": "#84CC16", "shopping": "#C2410C", "other": "#94A3B8",
}


def migrate_visualization_benchmark() -> None:
    """Keep public reference data separate from private facts and raw memory."""
    backup_before_migration("012_visualization_benchmark")
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS benchmark_sources (
              id INTEGER PRIMARY KEY AUTOINCREMENT, source_name TEXT NOT NULL, publisher TEXT NOT NULL,
              source_url TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'official', methodology TEXT NOT NULL DEFAULT '',
              retrieval_mode TEXT NOT NULL DEFAULT 'manual_import', expected_frequency TEXT NOT NULL DEFAULT 'irregular',
              last_checked_at TEXT, last_successful_at TEXT, usage_notes TEXT NOT NULL DEFAULT '', is_demo INTEGER NOT NULL DEFAULT 0,
              import_channel TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS benchmark_series (
              id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL, metric_key TEXT NOT NULL, metric_name TEXT NOT NULL,
              domain TEXT NOT NULL, unit TEXT NOT NULL, statistic_type TEXT NOT NULL, definition TEXT NOT NULL,
              population_scope TEXT NOT NULL, segment_definition_json TEXT NOT NULL DEFAULT '{}', geography TEXT NOT NULL DEFAULT '',
              metric_contract_json TEXT NOT NULL DEFAULT '{}',
              frequency TEXT NOT NULL DEFAULT 'irregular', version TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(source_id) REFERENCES benchmark_sources(id),
              UNIQUE(source_id, metric_key, statistic_type, population_scope, version)
            );
            CREATE TABLE IF NOT EXISTS benchmark_observations (
              id INTEGER PRIMARY KEY AUTOINCREMENT, series_id INTEGER NOT NULL, reference_period TEXT NOT NULL, published_at TEXT,
              value REAL, statistic_type TEXT NOT NULL, segment_values_json TEXT NOT NULL DEFAULT '{}', sample_size INTEGER,
              distribution_json TEXT NOT NULL DEFAULT '{}', revision TEXT NOT NULL DEFAULT '', raw_reference TEXT NOT NULL DEFAULT '',
              checksum TEXT NOT NULL DEFAULT '', imported_at TEXT NOT NULL, FOREIGN KEY(series_id) REFERENCES benchmark_series(id),
              UNIQUE(series_id, reference_period, revision, segment_values_json)
            );
            CREATE TABLE IF NOT EXISTS benchmark_refresh_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
              new_observations INTEGER NOT NULL DEFAULT 0, revised_observations INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
              adapter_version TEXT NOT NULL DEFAULT 'manual-v1', FOREIGN KEY(source_id) REFERENCES benchmark_sources(id)
            );
            CREATE INDEX IF NOT EXISTS idx_benchmark_series_metric ON benchmark_series(metric_key,active);
            CREATE INDEX IF NOT EXISTS idx_benchmark_observations_series ON benchmark_observations(series_id,reference_period DESC);
            """
        )
        source_columns = {row["name"] for row in connection.execute("PRAGMA table_info(benchmark_sources)")}
        if "is_demo" not in source_columns:
            connection.execute("ALTER TABLE benchmark_sources ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")
        if "import_channel" not in source_columns:
            connection.execute("ALTER TABLE benchmark_sources ADD COLUMN import_channel TEXT NOT NULL DEFAULT 'manual'")
        series_columns = {row["name"] for row in connection.execute("PRAGMA table_info(benchmark_series)")}
        if "metric_contract_json" not in series_columns:
            connection.execute("ALTER TABLE benchmark_series ADD COLUMN metric_contract_json TEXT NOT NULL DEFAULT '{}'")
        record_schema_migrations(connection)
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
            ("012_visualization_benchmark", now()),
        )


def migrate_decision_replay() -> None:
    """Record the additive, local-only B-3 schema migration.

    The table is created by ``initialize`` for new installations.  Keeping the
    migration marker separate means a production start takes a recoverable
    backup before the first existing database receives the new table.
    """
    with db() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS ux_feedback (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 screen TEXT NOT NULL,
                 feedback_type TEXT NOT NULL DEFAULT 'improvement',
                 body TEXT NOT NULL,
                 expected_behavior TEXT NOT NULL DEFAULT '',
                 severity TEXT NOT NULL DEFAULT 'medium',
                 status TEXT NOT NULL DEFAULT 'open',
                 created_at TEXT NOT NULL,
                 resolved_at TEXT,
                 CHECK(feedback_type IN ('improvement','bug','confusing','praise')),
                 CHECK(severity IN ('low','medium','high')),
                 CHECK(status IN ('open','resolved','dismissed'))
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_ux_feedback_status ON ux_feedback(status,created_at DESC)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)", ("013_decision_replay", now()))


def migrate_action_center_review_inbox() -> None:
    """Add display-only queue metadata without rewriting review history."""
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS fact_review_queue_state (
              fact_id INTEGER PRIMARY KEY,
              snoozed_until TEXT,
              last_presented_at TEXT,
              presentation_count INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(fact_id) REFERENCES facts(id)
            );
            CREATE INDEX IF NOT EXISTS idx_fact_review_queue_snoozed
              ON fact_review_queue_state(snoozed_until, updated_at);
            CREATE TABLE IF NOT EXISTS action_center_snoozes (
              action_key TEXT PRIMARY KEY,
              snoozed_until TEXT,
              keep_in_inbox INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_action_center_snoozes_until
              ON action_center_snoozes(snoozed_until, updated_at);
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
            ("014_action_center_review_inbox", now()),
        )


def migrate_action_center_review_inbox_stabilization() -> None:
    """Add proposal-only presentation metadata without changing user content."""
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_proposal_queue_state (
              proposal_id INTEGER PRIMARY KEY,
              snoozed_until TEXT,
              is_deferred INTEGER NOT NULL DEFAULT 0,
              last_presented_at TEXT,
              presentation_count INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(proposal_id) REFERENCES memory_proposals(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_memory_proposal_queue_snoozed
              ON memory_proposal_queue_state(snoozed_until,updated_at);
            CREATE INDEX IF NOT EXISTS idx_fact_reviews_state_fact
              ON fact_reviews(state,fact_id);
            CREATE INDEX IF NOT EXISTS idx_facts_review_priority
              ON facts(retrieval_eligibility,validation_status,created_at,id);
            CREATE INDEX IF NOT EXISTS idx_facts_fact_key_status
              ON facts(fact_key,status);
            CREATE INDEX IF NOT EXISTS idx_memory_proposals_status_created
              ON memory_proposals(status,created_at,id);
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(memory_proposal_queue_state)")}
        if "is_deferred" not in columns:
            connection.execute("ALTER TABLE memory_proposal_queue_state ADD COLUMN is_deferred INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
            ("015_action_center_review_inbox_stabilization", now()),
        )


def benchmark_projection(metric_key: str | None = None) -> dict[str, object]:
    """Project local benchmark data with an explicit, conservative comparison contract."""
    with db() as connection:
        sql = """SELECT s.id,s.metric_key,s.metric_name,s.domain,s.unit,s.statistic_type,s.definition,s.population_scope,
                         s.segment_definition_json,s.metric_contract_json,s.geography,s.frequency,s.version,src.source_name,src.publisher,src.source_url,
                         src.source_type,src.methodology,src.expected_frequency,src.last_checked_at,src.last_successful_at,src.is_demo,src.import_channel
                  FROM benchmark_series s JOIN benchmark_sources src ON src.id=s.source_id WHERE s.active=1"""
        params: list[object] = []
        if metric_key:
            sql += " AND s.metric_key=?"; params.append(metric_key)
        series = [dict(row) for row in connection.execute(sql + " ORDER BY s.domain,s.metric_name", params)]
        for item in series:
            item["segment_definition"] = _json_object(item.pop("segment_definition_json", "{}"))
            try:
                contract = benchmark_metric_contract(str(item["metric_key"]), item.pop("metric_contract_json", "{}"))
            except ValueError:
                contract = {}
            item["metric_contract"] = contract
            observations = [dict(row) for row in connection.execute(
                "SELECT * FROM benchmark_observations WHERE series_id=? ORDER BY reference_period DESC,id DESC LIMIT 24", (item["id"],)
            )]
            for observation in observations:
                observation["distribution"] = _json_object(observation.pop("distribution_json", "{}"))
                observation["segment_values"] = _json_object(observation.pop("segment_values_json", "{}"))
            item["observations"] = observations
            item["comparison_group_key"] = benchmark_comparison_group_key(item)
            item["personal"] = None
            item["compatibility"] = "reference_only"
            item["comparison"] = {"compatibility": "reference_only", "reasons": [{"code": "no_confirmed_current_fact"}]}
            if not contract:
                continue
            keys = tuple(contract.get("personal_fact_keys") or (item["metric_key"],))
            fact = connection.execute(
                f"""SELECT f.id,f.fact_key,f.value_json,f.summary,f.valid_from,f.created_at,f.subject_scope
                    FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
                    WHERE f.fact_key IN ({','.join('?' for _ in keys)}) AND f.status='current' AND r.state='confirmed'
                      AND COALESCE(f.retrieval_eligibility,'pending')='eligible'
                    ORDER BY COALESCE(f.valid_from,f.created_at) DESC,f.id DESC LIMIT 1""", keys,
            ).fetchone()
            if not fact:
                continue
            fact_value = _json_object(fact["value_json"])
            details = fact_value.get("details") if isinstance(fact_value.get("details"), dict) else {}
            embedded_contract = _json_object(details.get("benchmark_contract"))
            registry_contract = resolve_personal_metric_contract(str(fact["fact_key"]))
            # A Fact can carry old or model-produced contract metadata. It is
            # useful for audits, but cannot authorize a numeric comparison.
            # Only the reviewed Registry is allowed to define the Fact side.
            fact_contract = dict(registry_contract or {})
            amount = fact_value.get("amount")
            unit = str(fact_value.get("currency") or fact_value.get("unit") or "")
            reasons: list[dict[str, object]] = []
            if not fact_contract:
                reasons.append({"code": "personal_contract_missing", "subject_scope": fact["subject_scope"] or "unknown"})
            embedded_matches_registry = (
                embedded_contract_matches_registry(embedded_contract, fact_contract)
                if embedded_contract and fact_contract else None
            )
            if embedded_matches_registry is False:
                reasons.append({"code": "embedded_contract_conflicts_with_registry"})
            for field in ("metric_key", "statistical_unit", "measurement_kind", "time_basis"):
                expected, actual = contract.get(field), fact_contract.get(field)
                if expected and actual and str(expected) != str(actual):
                    reasons.append({"code": f"{field}_mismatch", "personal": actual, "reference": expected})
                elif expected and not actual:
                    reasons.append({"code": f"{field}_unverified", "reference": expected})
            canonical_unit = contract["canonical_unit"]
            personal_value, _ = normalize_benchmark_value(amount, unit, canonical_unit)
            latest = observations[0] if observations else {}
            reference_value, _ = normalize_benchmark_value(latest.get("value"), item["unit"], canonical_unit)
            if amount is not None and personal_value is None:
                reasons.append({"code": "personal_unit_unverified", "personal": unit or None, "reference": canonical_unit})
            if latest.get("value") is not None and reference_value is None:
                reasons.append({"code": "reference_unit_unverified", "reference": item["unit"], "canonical": canonical_unit})
            blocking_reasons = [reason for reason in reasons if reason.get("code") != "embedded_contract_conflicts_with_registry"]
            incompatible = any(str(reason["code"]).endswith("_mismatch") for reason in blocking_reasons)
            compatibility = "incompatible" if incompatible else "reference_only" if blocking_reasons else (
                "exact" if unit.lower() == str(item["unit"]).lower() else "comparable"
            )
            item["compatibility"] = compatibility
            item["personal"] = {"fact_id": fact["id"], "fact_key": fact["fact_key"], "value": amount, "unit": unit,
                                "summary": fact["summary"], "valid_from": fact["valid_from"], "contract": fact_contract,
                                "contract_source": "registry" if registry_contract else "none",
                                "embedded_contract_present": bool(embedded_contract),
                                "embedded_contract_matches_registry": embedded_matches_registry}
            comparison: dict[str, object] = {"compatibility": compatibility, "personal_value": personal_value,
                                             "reference_value": reference_value, "unit": canonical_unit, "reasons": reasons}
            if compatibility in {"exact", "comparable"} and personal_value is not None and reference_value is not None:
                comparison.update({"absolute_difference": personal_value - reference_value,
                                   "ratio": personal_value / reference_value if reference_value else None,
                                   "percentage_difference": (personal_value - reference_value) / reference_value if reference_value else None,
                                   "percentile_band": benchmark_percentile_band(latest.get("distribution"), personal_value)})
            item["comparison"] = comparison
    return {"series": series, "statistic_types": sorted(BENCHMARK_STAT_TYPES),
            "privacy": "参照データはローカルに保存します。本人のFactを出典元へ送信せず、デモデータは相談時の文脈から除外します。"}


def benchmark_compatibility_audit() -> dict[str, object]:
    """Read-only production diagnostic for Registry-derived personal metrics."""
    metrics: list[dict[str, object]] = []
    with db() as connection:
        for metric_key, definition in BENCHMARK_METRIC_CONTRACTS.items():
            keys = tuple(definition.get("personal_fact_keys", ()))
            if not keys:
                continue
            marks = ",".join("?" for _ in keys)
            rows = connection.execute(f"SELECT fact_key,COUNT(*) AS count FROM facts WHERE fact_key IN ({marks}) GROUP BY fact_key ORDER BY fact_key", keys).fetchall()
            current = connection.execute(
                f"""SELECT f.id,f.fact_key FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
                    WHERE f.fact_key IN ({marks}) AND f.status='current' AND r.state='confirmed'
                      AND COALESCE(f.retrieval_eligibility,'pending')='eligible' LIMIT 1""", keys,
            ).fetchone()
            metrics.append({"metric_key": metric_key, "candidate_fact_keys": [dict(row) for row in rows],
                            "matched_current_fact": bool(current), "matched_fact_key": current["fact_key"] if current else None,
                            "compatibility": "exact" if current else "reference_only",
                            "reasons": [] if current else [{"code": "no_confirmed_current_fact"}]})
    return {"metrics": metrics, "read_only": True}


def _benchmark_json(value: object, field: str) -> str:
    """Serialize a benchmark metadata object after enforcing a small, local schema."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def import_benchmark_reference(payload: dict[str, object]) -> dict[str, object]:
    """Import documented public-reference data without contacting any external service.

    Benchmark values deliberately live in their own tables.  This import path is
    useful for a CSV/API export that the user has reviewed, and makes provenance
    mandatory so a comparison never looks more authoritative than its source.
    """
    source = payload.get("source")
    series = payload.get("series")
    observations = payload.get("observations")
    if not isinstance(source, dict) or not isinstance(series, dict) or not isinstance(observations, list) or not observations:
        raise ValueError("source, series, and at least one observation are required")
    source_name = str(source.get("source_name", "")).strip()
    publisher = str(source.get("publisher", "")).strip()
    source_url = str(source.get("source_url", "")).strip()
    if not source_name or not publisher or not source_url.startswith(("https://", "http://")):
        raise ValueError("source_name, publisher, and an http(s) source_url are required")
    source_type = str(source.get("source_type", "official")).strip()
    if source_type not in {"official", "quasi_official", "research", "other", "sample"}:
        raise ValueError("source_type is invalid")
    metric_key = str(series.get("metric_key", "")).strip()
    metric_name = str(series.get("metric_name", "")).strip()
    domain = str(series.get("domain", "other")).strip() or "other"
    unit = str(series.get("unit", "")).strip()
    definition = str(series.get("definition", "")).strip()
    population_scope = str(series.get("population_scope", "")).strip()
    statistic_type = str(series.get("statistic_type", "")).strip()
    if not all((metric_key, metric_name, unit, definition, population_scope)):
        raise ValueError("metric_key, metric_name, unit, definition, and population_scope are required")
    if statistic_type not in BENCHMARK_STAT_TYPES:
        raise ValueError("series statistic_type is invalid")
    metric_contract = benchmark_metric_contract(metric_key, series.get("metric_contract"))
    segment_definition = _benchmark_json(series.get("segment_definition"), "segment_definition")
    timestamp = now()
    new_observations = 0
    revised_observations = 0
    with db() as connection:
        existing_source = connection.execute(
            "SELECT id FROM benchmark_sources WHERE source_name=? AND source_url=?", (source_name, source_url)
        ).fetchone()
        if existing_source:
            source_id = int(existing_source["id"])
            connection.execute(
                """UPDATE benchmark_sources SET publisher=?,source_type=?,methodology=?,expected_frequency=?,usage_notes=?,
                   last_checked_at=?,last_successful_at=?,updated_at=? WHERE id=?""",
                (publisher, source_type, str(source.get("methodology", "")), str(source.get("expected_frequency", "irregular")),
                 str(source.get("usage_notes", "")), timestamp, timestamp, timestamp, source_id),
            )
        else:
            source_id = int(connection.execute(
                """INSERT INTO benchmark_sources(source_name,publisher,source_url,source_type,methodology,retrieval_mode,
                   expected_frequency,last_checked_at,last_successful_at,usage_notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,'manual_import',?,?,?,?,?,?)""",
                (source_name, publisher, source_url, source_type, str(source.get("methodology", "")),
                 str(source.get("expected_frequency", "irregular")), timestamp, timestamp, str(source.get("usage_notes", "")), timestamp, timestamp),
            ).lastrowid)
        version = str(series.get("version", "")).strip()
        row = connection.execute(
            """SELECT id FROM benchmark_series WHERE source_id=? AND metric_key=? AND statistic_type=?
               AND population_scope=? AND version=?""", (source_id, metric_key, statistic_type, population_scope, version)
        ).fetchone()
        if row:
            series_id = int(row["id"])
            connection.execute(
                """UPDATE benchmark_series SET metric_name=?,domain=?,unit=?,definition=?,segment_definition_json=?,metric_contract_json=?,geography=?,
                   frequency=?,active=1,updated_at=? WHERE id=?""",
                (metric_name, domain, unit, definition, segment_definition, _benchmark_json(metric_contract, "metric_contract"), str(series.get("geography", "")),
                 str(series.get("frequency", "irregular")), timestamp, series_id),
            )
        else:
            series_id = int(connection.execute(
                """INSERT INTO benchmark_series(source_id,metric_key,metric_name,domain,unit,statistic_type,definition,
                   population_scope,segment_definition_json,metric_contract_json,geography,frequency,version,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source_id, metric_key, metric_name, domain, unit, statistic_type, definition, population_scope,
                 segment_definition, _benchmark_json(metric_contract, "metric_contract"), str(series.get("geography", "")), str(series.get("frequency", "irregular")), version, timestamp, timestamp),
            ).lastrowid)
        run_id = int(connection.execute(
            "INSERT INTO benchmark_refresh_runs(source_id,status,started_at,adapter_version) VALUES(?,?,?,?)",
            (source_id, "running", timestamp, "manual-v1"),
        ).lastrowid)
        for observation in observations:
            if not isinstance(observation, dict):
                raise ValueError("each observation must be an object")
            period = str(observation.get("reference_period", "")).strip()
            observation_type = str(observation.get("statistic_type", statistic_type)).strip()
            if not period or observation_type not in BENCHMARK_STAT_TYPES:
                raise ValueError("each observation needs reference_period and a valid statistic_type")
            raw_value = observation.get("value")
            if raw_value in (None, ""):
                numeric_value = None
            else:
                try:
                    numeric_value = float(raw_value)
                except (TypeError, ValueError) as error:
                    raise ValueError("observation value must be numeric") from error
            segments = _benchmark_json(observation.get("segment_values"), "segment_values")
            distribution = _benchmark_json(observation.get("distribution"), "distribution")
            revision = str(observation.get("revision", "")).strip()
            existing = connection.execute(
                "SELECT id FROM benchmark_observations WHERE series_id=? AND reference_period=? AND revision=? AND segment_values_json=?",
                (series_id, period, revision, segments),
            ).fetchone()
            values = (series_id, period, str(observation.get("published_at", "")).strip() or None, numeric_value, observation_type,
                      segments, observation.get("sample_size") or None, distribution, revision, str(observation.get("raw_reference", "")),
                      str(observation.get("checksum", "")), timestamp)
            if existing:
                revised_observations += 1
                connection.execute(
                    """UPDATE benchmark_observations SET published_at=?,value=?,statistic_type=?,sample_size=?,distribution_json=?,
                       raw_reference=?,checksum=?,imported_at=? WHERE id=?""",
                    (values[2], values[3], values[4], values[6], values[7], values[9], values[10], values[11], existing["id"]),
                )
            else:
                new_observations += 1
                connection.execute(
                    """INSERT INTO benchmark_observations(series_id,reference_period,published_at,value,statistic_type,segment_values_json,
                       sample_size,distribution_json,revision,raw_reference,checksum,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", values)
        connection.execute(
            "UPDATE benchmark_refresh_runs SET status='completed',finished_at=?,new_observations=?,revised_observations=? WHERE id=?",
            (now(), new_observations, revised_observations, run_id),
        )
    return {"source_id": source_id, "series_id": series_id, "new_observations": new_observations,
            "revised_observations": revised_observations, "message": "Reference data imported locally. No personal fact was transmitted."}


def _validate_benchmark_dataset(dataset: dict[str, object]) -> None:
    """Shared preview/write validation.  It must be strict before any write."""
    source, series, observations = dataset.get("source"), dataset.get("series"), dataset.get("observations")
    if not isinstance(source, dict) or not isinstance(series, dict) or not isinstance(observations, list) or not observations:
        raise ValueError("Each dataset requires source, series, and observations")
    source_type = str(source.get("source_type", "official")).strip()
    if source_type not in {"official", "quasi_official", "research", "other", "sample"}:
        raise ValueError("source_type is invalid")
    if not str(source.get("source_name", "")).strip() or not str(source.get("publisher", "")).strip() or not str(source.get("source_url", "")).startswith(("https://", "http://")):
        raise ValueError("Each dataset requires source name, publisher and an http(s) source_url")
    for field in ("metric_key", "metric_name", "unit", "statistic_type", "definition", "population_scope"):
        if not str(series.get(field, "")).strip():
            raise ValueError("Each series requires metric key/name, unit, statistic type, definition, and population scope")
    if str(series["statistic_type"]) not in BENCHMARK_STAT_TYPES:
        raise ValueError("Unsupported statistic_type")
    benchmark_metric_contract(str(series["metric_key"]), series.get("metric_contract"))
    _benchmark_json(series.get("segment_definition"), "segment_definition")
    seen: set[tuple[str, str, str]] = set()
    for item in observations:
        if not isinstance(item, dict) or not str(item.get("reference_period", "")).strip():
            raise ValueError("Every observation requires reference_period")
        if str(item.get("statistic_type", series["statistic_type"])) not in BENCHMARK_STAT_TYPES:
            raise ValueError("observation statistic_type is invalid")
        if item.get("value") not in (None, ""):
            try:
                float(item["value"])
            except (TypeError, ValueError) as error:
                raise ValueError("observation value must be numeric") from error
        sample_size = item.get("sample_size")
        if sample_size not in (None, "") and (not isinstance(sample_size, int) or isinstance(sample_size, bool) or sample_size <= 0):
            raise ValueError("sample_size must be a positive integer")
        distribution = _json_object(item.get("distribution"))
        if item.get("distribution") is not None and not isinstance(item.get("distribution"), dict):
            raise ValueError("distribution must be an object")
        if distribution:
            values = []
            for key in ("p10", "p25", "p50", "p75", "p90"):
                if key in distribution:
                    try:
                        values.append(float(distribution[key]))
                    except (TypeError, ValueError) as error:
                        raise ValueError("distribution percentiles must be numeric") from error
            if values and values != sorted(values):
                raise ValueError("distribution percentiles must be ordered")
        _benchmark_json(item.get("segment_values"), "segment_values")
        key = (str(item["reference_period"]), str(item.get("revision", "")), _benchmark_json(item.get("segment_values"), "segment_values"))
        if key in seen:
            raise ValueError("duplicate observation in dataset")
        seen.add(key)


def _import_benchmark_reference_write(connection: sqlite3.Connection, payload: dict[str, object], channel: str) -> dict[str, object]:
    """Write a validated dataset into the caller's transaction."""
    _validate_benchmark_dataset(payload)
    source, series, observations = payload["source"], payload["series"], payload["observations"]
    assert isinstance(source, dict) and isinstance(series, dict) and isinstance(observations, list)
    source_name, publisher, source_url = str(source["source_name"]).strip(), str(source["publisher"]).strip(), str(source["source_url"]).strip()
    source_type = str(source.get("source_type", "official")).strip()
    metric_key, statistic_type = str(series["metric_key"]).strip(), str(series["statistic_type"]).strip()
    metric_contract = _benchmark_json(benchmark_metric_contract(metric_key, series.get("metric_contract")), "metric_contract")
    timestamp = now()
    existing_source = connection.execute("SELECT id FROM benchmark_sources WHERE source_name=? AND source_url=?", (source_name, source_url)).fetchone()
    is_demo = int(source_type == "sample")
    if existing_source:
        source_id = int(existing_source["id"])
        connection.execute("""UPDATE benchmark_sources SET publisher=?,source_type=?,methodology=?,expected_frequency=?,usage_notes=?,last_checked_at=?,last_successful_at=?,is_demo=?,import_channel=?,updated_at=? WHERE id=?""",
                           (publisher, source_type, str(source.get("methodology", "")), str(source.get("expected_frequency", "irregular")), str(source.get("usage_notes", "")), timestamp, timestamp, is_demo, channel, timestamp, source_id))
    else:
        source_id = int(connection.execute("""INSERT INTO benchmark_sources(source_name,publisher,source_url,source_type,methodology,retrieval_mode,expected_frequency,last_checked_at,last_successful_at,usage_notes,is_demo,import_channel,created_at,updated_at) VALUES(?,?,?,?,?,'manual_import',?,?,?,?,?,?,?,?)""",
                                           (source_name, publisher, source_url, source_type, str(source.get("methodology", "")), str(source.get("expected_frequency", "irregular")), timestamp, timestamp, str(source.get("usage_notes", "")), is_demo, channel, timestamp, timestamp)).lastrowid)
    version = str(series.get("version", "")).strip()
    population_scope = str(series["population_scope"]).strip()
    row = connection.execute("SELECT id FROM benchmark_series WHERE source_id=? AND metric_key=? AND statistic_type=? AND population_scope=? AND version=?", (source_id, metric_key, statistic_type, population_scope, version)).fetchone()
    values = (str(series["metric_name"]).strip(), str(series.get("domain", "other")).strip() or "other", str(series["unit"]).strip(), str(series["definition"]).strip(), _benchmark_json(series.get("segment_definition"), "segment_definition"), metric_contract, str(series.get("geography", "")), str(series.get("frequency", "irregular")), timestamp)
    if row:
        series_id = int(row["id"])
        connection.execute("UPDATE benchmark_series SET metric_name=?,domain=?,unit=?,definition=?,segment_definition_json=?,metric_contract_json=?,geography=?,frequency=?,active=1,updated_at=? WHERE id=?", (*values, series_id))
    else:
        series_id = int(connection.execute("""INSERT INTO benchmark_series(source_id,metric_key,metric_name,domain,unit,statistic_type,definition,population_scope,segment_definition_json,metric_contract_json,geography,frequency,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                           (source_id, metric_key, values[0], values[1], values[2], statistic_type, values[3], population_scope, values[4], values[5], values[6], values[7], version, timestamp, timestamp)).lastrowid)
    run_id = int(connection.execute("INSERT INTO benchmark_refresh_runs(source_id,status,started_at,adapter_version) VALUES(?,?,?,?)", (source_id, "running", timestamp, "manual-v1")).lastrowid)
    created = revised = 0
    for observation in observations:
        assert isinstance(observation, dict)
        period, revision = str(observation["reference_period"]).strip(), str(observation.get("revision", "")).strip()
        segments = _benchmark_json(observation.get("segment_values"), "segment_values")
        existing = connection.execute("SELECT id FROM benchmark_observations WHERE series_id=? AND reference_period=? AND revision=? AND segment_values_json=?", (series_id, period, revision, segments)).fetchone()
        row_values = (series_id, period, str(observation.get("published_at", "")).strip() or None, float(observation["value"]) if observation.get("value") not in (None, "") else None, str(observation.get("statistic_type", statistic_type)).strip(), segments, observation.get("sample_size") or None, _benchmark_json(observation.get("distribution"), "distribution"), revision, str(observation.get("raw_reference", "")), str(observation.get("checksum", "")), timestamp)
        if existing:
            revised += 1
            connection.execute("UPDATE benchmark_observations SET published_at=?,value=?,statistic_type=?,sample_size=?,distribution_json=?,raw_reference=?,checksum=?,imported_at=? WHERE id=?", (row_values[2], row_values[3], row_values[4], row_values[6], row_values[7], row_values[9], row_values[10], row_values[11], existing["id"]))
        else:
            created += 1
            connection.execute("INSERT INTO benchmark_observations(series_id,reference_period,published_at,value,statistic_type,segment_values_json,sample_size,distribution_json,revision,raw_reference,checksum,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row_values)
    connection.execute("UPDATE benchmark_refresh_runs SET status='completed',finished_at=?,new_observations=?,revised_observations=? WHERE id=?", (now(), created, revised, run_id))
    return {"source_id": source_id, "series_id": series_id, "new_observations": created, "revised_observations": revised}


def import_benchmark_reference(payload: dict[str, object]) -> dict[str, object]:
    """Import one reference dataset using the same validation as Bundle preview/save."""
    with db() as connection:
        result = _import_benchmark_reference_write(connection, payload, "manual")
    return {**result, "message": "Reference data imported locally. No personal fact was transmitted."}


def parse_benchmark_bundle(raw_payload: object) -> list[dict[str, object]]:
    """Accept a legacy one-dataset payload or a fenced multi-dataset Bundle."""
    if isinstance(raw_payload, str):
        text = raw_payload.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            raw_payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("Benchmark JSON could not be parsed") from error
    if not isinstance(raw_payload, dict):
        raise ValueError("Benchmark payload must be a JSON object")
    datasets = raw_payload.get("datasets") if "datasets" in raw_payload else [raw_payload]
    if not isinstance(datasets, list) or not datasets:
        raise ValueError("datasets must contain at least one dataset")
    if len(datasets) > 30:
        raise ValueError("A Bundle may contain at most 30 datasets")
    if not all(isinstance(dataset, dict) for dataset in datasets):
        raise ValueError("Every dataset must be an object")
    return [dict(dataset) for dataset in datasets]


def validate_benchmark_bundle(raw_payload: object) -> dict[str, object]:
    """Validate all datasets before any write, preventing partial user imports."""
    datasets = parse_benchmark_bundle(raw_payload)
    warnings: list[str] = []
    metrics: list[str] = []
    observations = 0
    for dataset in datasets:
        _validate_benchmark_dataset(dataset)
        source, series, items = dataset["source"], dataset["series"], dataset["observations"]
        assert isinstance(source, dict) and isinstance(series, dict) and isinstance(items, list)
        if str(source.get("source_type", "official")) == "sample":
            warnings.append(f"{series.get('metric_name')}: demo data")
        if not isinstance(series.get("segment_definition", {}), dict):
            raise ValueError("segment_definition must be an object")
        for item in items:
            if not isinstance(item, dict) or not str(item.get("reference_period", "")).strip():
                raise ValueError("Every observation requires reference_period")
        metrics.append(str(series["metric_name"]))
        observations += len(items)
    return {"datasets": len(datasets), "observations": observations, "metrics": metrics, "warnings": warnings, "payload": datasets}


def import_benchmark_bundle(raw_payload: object, channel: str = "manual") -> dict[str, object]:
    """Validate every dataset, then write all datasets in one SQLite transaction."""
    preview = validate_benchmark_bundle(raw_payload)
    datasets = preview.pop("payload")
    results = []
    with db() as connection:
        for dataset in datasets:
            results.append(_import_benchmark_reference_write(connection, dataset, channel))
    return {"datasets": len(results), "new_observations": sum(int(item["new_observations"]) for item in results),
            "revised_observations": sum(int(item["revised_observations"]) for item in results), "warnings": preview["warnings"]}


def demo_benchmark_bundle() -> dict[str, object]:
    """Synthetic data only. It is never loaded automatically or used by chat."""
    path = ROOT / "resources" / "benchmarks" / "demo_bundle.json"
    return json.loads(path.read_text(encoding="utf-8"))


def personal_space_projection(include_sensitive: bool = False, limit: int = 180) -> dict[str, object]:
    """Stable, bounded, local graph payload for the Canvas/WebGL-independent renderer."""
    with db() as connection:
        facts = [dict(row) for row in connection.execute(
            """SELECT f.id,f.category,f.fact_type,f.status,f.summary,f.created_at,f.valid_from,f.truth_confidence,
                       (SELECT COUNT(*) FROM fact_evidence e WHERE e.fact_id=f.id) AS evidence_count
                FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
                WHERE r.state='confirmed' AND COALESCE(f.retrieval_eligibility,'pending')='eligible'
                ORDER BY CASE f.status WHEN 'current' THEN 0 ELSE 1 END,f.created_at DESC LIMIT ?""", (limit,)
        )]
        decisions = [dict(row) for row in connection.execute(
            "SELECT id,domain,title,decision_state,created_at,updated_at FROM decisions ORDER BY updated_at DESC LIMIT 60"
        )]
        recommendations = [dict(row) for row in connection.execute(
            "SELECT id,domain,title,status,created_at,updated_at FROM recommendations WHERE status!='dismissed' ORDER BY updated_at DESC LIMIT 40"
        )]
        plans = [dict(row) for row in connection.execute(
            "SELECT id,domain,title,status,result,decision_id,source_recommendation_id,created_at,updated_at FROM plans ORDER BY updated_at DESC LIMIT 40"
        )]
        execution_events = [dict(row) for row in connection.execute(
            "SELECT id,decision_id,plan_id,event_type,summary,occurred_at,created_at FROM execution_events ORDER BY COALESCE(occurred_at,created_at) DESC LIMIT 60"
        )]
    sensitive = {"health", "relationship"}
    nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    for row in facts:
        hidden = (row["category"] in sensitive or row["category"] == "finance") and not include_sensitive
        nodes.append({"id": f"fact-{row['id']}", "kind": "fact", "domain": row["category"], "label": "Sensitive fact" if hidden else row["summary"][:90],
                      "masked": hidden, "status": row["status"], "strength": min(1.0, 0.2 + 0.12 * row["evidence_count"] + float(row["truth_confidence"] or 0) * .45),
                      "updated_at": row["valid_from"] or row["created_at"], "target": f"/api/facts/{row['id']}/evidence", "evidence_count": row["evidence_count"]})
    for row in decisions:
        nodes.append({"id": f"decision-{row['id']}", "kind": "decision", "domain": row["domain"] or "other", "label": row["title"][:90],
                      "masked": False, "status": row["decision_state"], "strength": .82, "updated_at": row["updated_at"] or row["created_at"], "target": f"/api/decisions/{row['id']}"})
    for row in recommendations:
        nodes.append({"id": f"recommendation-{row['id']}", "kind": "recommendation", "domain": row["domain"] or "other", "label": row["title"][:90],
                      "masked": False, "status": row["status"], "strength": .55, "updated_at": row["updated_at"] or row["created_at"], "target": f"/api/recommendations/{row['id']}"})
    for row in plans:
        plan_id = f"plan-{row['id']}"
        nodes.append({"id": plan_id, "kind": "plan", "domain": row["domain"] or "other", "label": row["title"][:90],
                      "masked": False, "status": row["status"], "strength": .68, "updated_at": row["updated_at"] or row["created_at"], "target": f"/api/plans/{row['id']}"})
        if row["source_recommendation_id"]:
            edges.append({"from": f"recommendation-{row['source_recommendation_id']}", "to": plan_id, "kind": "lifecycle"})
        if row["decision_id"]:
            edges.append({"from": plan_id, "to": f"decision-{row['decision_id']}", "kind": "lifecycle"})
        if row["result"]:
            result_id = f"result-plan-{row['id']}"
            nodes.append({"id": result_id, "kind": "result", "domain": row["domain"] or "other", "label": str(row["result"])[:90],
                          "masked": False, "status": "result", "strength": .9, "updated_at": row["updated_at"] or row["created_at"], "target": f"/api/plans/{row['id']}"})
            edges.append({"from": plan_id, "to": result_id, "kind": "result"})
    for row in execution_events:
        event_id = f"result-event-{row['id']}"
        nodes.append({"id": event_id, "kind": "result", "domain": "life", "label": row["summary"][:90], "masked": False,
                      "status": row["event_type"], "strength": .72, "updated_at": row["occurred_at"] or row["created_at"], "target": f"/api/decisions/{row['decision_id']}"})
        if row["decision_id"]:
            edges.append({"from": f"decision-{row['decision_id']}", "to": event_id, "kind": "result"})
        if row["plan_id"]:
            edges.append({"from": f"plan-{row['plan_id']}", "to": event_id, "kind": "result"})
    known = {str(node["id"]) for node in nodes}
    edges = [edge for edge in edges if str(edge["from"]) in known and str(edge["to"]) in known]
    return {"layout_version": "personal-space-v2", "colors": PERSONAL_SPACE_COLORS, "nodes": nodes[:limit + 160], "edges": edges}


def personal_space_node_detail(kind: str, node_id: int, include_sensitive: bool = False) -> dict[str, object] | None:
    """Return a small in-app detail projection; raw conversation is never returned here."""
    allowed = {"fact", "decision", "recommendation", "plan", "result"}
    if kind not in allowed:
        return None
    with db() as connection:
        if kind == "fact":
            row = connection.execute("""SELECT f.id,f.category,f.summary,f.status,f.valid_from,f.valid_to,f.created_at,
                (SELECT COUNT(*) FROM fact_evidence e WHERE e.fact_id=f.id) AS evidence_count,d.title AS source_document
                FROM facts f JOIN documents d ON d.id=f.document_id WHERE f.id=?""", (node_id,)).fetchone()
            if not row:
                return None
            item = dict(row); item.update({"kind": kind, "temporal_bucket": _space_temporal_bucket(kind, row["status"]), "evidence": {"count": row["evidence_count"], "source_document": row["source_document"]}})
        elif kind == "decision":
            row = connection.execute("SELECT id,domain,title,question,decision,selected_option,rationale,result,later_evaluation,decision_state,decided_on,updated_at FROM decisions WHERE id=?", (node_id,)).fetchone()
            if not row:
                return None
            item = dict(row); item.update({"kind": kind, "status": row["decision_state"], "temporal_bucket": _space_temporal_bucket(kind, row["decision_state"])})
        elif kind == "recommendation":
            row = connection.execute("SELECT id,domain,title,rationale,status,updated_at FROM recommendations WHERE id=?", (node_id,)).fetchone()
            if not row:
                return None
            item = dict(row); item.update({"kind": kind, "temporal_bucket": _space_temporal_bucket(kind, row["status"])})
        elif kind == "plan":
            row = connection.execute("SELECT id,domain,title,steps_json,status,result,decision_id,updated_at FROM plans WHERE id=?", (node_id,)).fetchone()
            if not row:
                return None
            item = dict(row); item["steps"] = _json_value(item.pop("steps_json"), []); item.update({"kind": kind, "temporal_bucket": _space_temporal_bucket(kind, row["status"])})
        else:
            row = connection.execute("""SELECT e.id,e.summary,e.event_type,e.occurred_at,e.plan_id,e.decision_id,COALESCE(p.domain,d.domain,'other') AS domain
                FROM execution_events e LEFT JOIN plans p ON p.id=e.plan_id LEFT JOIN decisions d ON d.id=e.decision_id WHERE e.id=?""", (node_id,)).fetchone()
            if not row:
                return None
            item = dict(row); item.update({"kind": kind, "status": row["event_type"], "temporal_bucket": "history"})
    item["domain"] = canonical_domain(str(item.get("domain") or item.get("category") or "other"))
    if item["domain"] in {"finance", "relationship", "health"} and not include_sensitive:
        for field in ("summary", "title", "question", "decision", "selected_option", "rationale", "result", "later_evaluation", "steps"):
            if field in item:
                item[field] = "機微情報（マスク中）"
        item["masked"] = True
    else:
        item["masked"] = False
    return item


def _space_temporal_bucket(kind: str, status: object) -> str:
    state = str(status or "").lower()
    if kind == "fact":
        return "current" if state == "current" else "history"
    if kind == "recommendation":
        return "history" if state in {"dismissed", "converted"} else "current"
    if kind == "plan":
        return "history" if state in {"completed", "cancelled"} else "current"
    if kind == "decision":
        return "history" if state in {"result", "evaluated", "closed"} else "current"
    return "history"


def _space_node(node_id: str, kind: str, domain: object, label: object, status: object, updated_at: object,
                strength: float, include_sensitive: bool, **extra: object) -> dict[str, object]:
    normalized_domain = canonical_domain(str(domain or "other"))
    masked = normalized_domain in {"finance", "relationship", "health"} and not include_sensitive
    return {"id": node_id, "kind": kind, "domain": normalized_domain, "label": "機微情報（マスク中）" if masked else str(label or "")[:90],
            "masked": masked, "status": str(status or ""), "temporal_bucket": _space_temporal_bucket(kind, status),
            "strength": strength, "updated_at": updated_at, "detail": {"kind": kind, "id": node_id}, **extra}


def personal_space_projection(include_sensitive: bool = False, limit: int = 180) -> dict[str, object]:
    """Stable, bounded local graph.  Sensitive labels are masked for every node kind."""
    with db() as connection:
        facts = [dict(row) for row in connection.execute("""SELECT f.id,f.category,f.status,f.summary,f.created_at,f.valid_from,f.truth_confidence,
            (SELECT COUNT(*) FROM fact_evidence e WHERE e.fact_id=f.id) AS evidence_count FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
            WHERE r.state='confirmed' AND COALESCE(f.retrieval_eligibility,'pending')='eligible'
            ORDER BY CASE f.status WHEN 'current' THEN 0 ELSE 1 END,f.created_at DESC LIMIT ?""", (limit,))]
        decisions = [dict(row) for row in connection.execute("SELECT id,domain,title,decision_state,created_at,updated_at FROM decisions ORDER BY updated_at DESC LIMIT 60")]
        recommendations = [dict(row) for row in connection.execute("SELECT id,domain,title,status,created_at,updated_at FROM recommendations WHERE status!='dismissed' ORDER BY updated_at DESC LIMIT 40")]
        plans = [dict(row) for row in connection.execute("SELECT id,domain,title,status,result,decision_id,source_recommendation_id,created_at,updated_at FROM plans ORDER BY updated_at DESC LIMIT 40")]
        events = [dict(row) for row in connection.execute("""SELECT e.id,e.decision_id,e.plan_id,e.event_type,e.summary,e.occurred_at,e.created_at,
            COALESCE(p.domain,d.domain,'other') AS domain FROM execution_events e
            LEFT JOIN plans p ON p.id=e.plan_id LEFT JOIN decisions d ON d.id=e.decision_id
            ORDER BY COALESCE(e.occurred_at,e.created_at) DESC LIMIT 60""")]
    nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    for row in facts:
        nodes.append(_space_node(f"fact-{row['id']}", "fact", row["category"], row["summary"], row["status"], row["valid_from"] or row["created_at"], min(1.0, .2 + .12 * row["evidence_count"] + float(row["truth_confidence"] or 0) * .45), include_sensitive, evidence_count=row["evidence_count"]))
    for row in decisions:
        nodes.append(_space_node(f"decision-{row['id']}", "decision", row["domain"], row["title"], row["decision_state"], row["updated_at"] or row["created_at"], .82, include_sensitive))
    for row in recommendations:
        nodes.append(_space_node(f"recommendation-{row['id']}", "recommendation", row["domain"], row["title"], row["status"], row["updated_at"] or row["created_at"], .55, include_sensitive))
    for row in plans:
        plan_id = f"plan-{row['id']}"
        nodes.append(_space_node(plan_id, "plan", row["domain"], row["title"], row["status"], row["updated_at"] or row["created_at"], .68, include_sensitive))
        if row["source_recommendation_id"]:
            edges.append({"from": f"recommendation-{row['source_recommendation_id']}", "to": plan_id, "kind": "lifecycle"})
        if row["decision_id"]:
            edges.append({"from": plan_id, "to": f"decision-{row['decision_id']}", "kind": "lifecycle"})
        if row["result"]:
            result_id = f"result-plan-{row['id']}"
            nodes.append(_space_node(result_id, "result", row["domain"], row["result"], "result", row["updated_at"] or row["created_at"], .9, include_sensitive))
            edges.append({"from": plan_id, "to": result_id, "kind": "result"})
    for row in events:
        event_id = f"result-event-{row['id']}"
        nodes.append(_space_node(event_id, "result", row["domain"], row["summary"], row["event_type"], row["occurred_at"] or row["created_at"], .72, include_sensitive))
        if row["decision_id"]:
            edges.append({"from": f"decision-{row['decision_id']}", "to": event_id, "kind": "result"})
        if row["plan_id"]:
            edges.append({"from": f"plan-{row['plan_id']}", "to": event_id, "kind": "result"})
    known = {str(node["id"]) for node in nodes}
    return {"layout_version": "personal-space-v3", "colors": PERSONAL_SPACE_COLORS, "nodes": nodes[:limit + 160],
            "edges": [edge for edge in edges if str(edge["from"]) in known and str(edge["to"]) in known]}


def backfill_fact_evidence() -> int:
    """Create one provenance row for legacy facts without changing their values."""
    created = 0
    with db() as connection:
        rows = connection.execute(
            """SELECT f.id,f.source_chunk_id,f.source_attachment_id,f.summary,f.confidence,f.extractor,f.extractor_model,
                      f.prompt_version,c.text AS chunk_text
               FROM facts f LEFT JOIN chunks c ON c.id=f.source_chunk_id
               WHERE NOT EXISTS (SELECT 1 FROM fact_evidence e WHERE e.fact_id=f.id)"""
        ).fetchall()
        for row in rows:
            before = connection.total_changes
            record_fact_evidence(
                connection,
                row["id"],
                source_chunk_id=row["source_chunk_id"],
                source_attachment_id=row["source_attachment_id"],
                quote=(row["chunk_text"] or row["summary"] or "")[:4000],
                evidence_kind="image" if row["source_attachment_id"] else "conversation",
                source_group=f"{row['extractor'] or 'legacy'}:{row['extractor_model'] or ''}:{row['prompt_version'] or 'legacy-v1'}",
                reliability=row["confidence"],
            )
            created += max(0, connection.total_changes - before)
    return created


def detect_fact_anomalies(limit: int = 50) -> list[dict[str, object]]:
    """Find explainable conflicts/outliers without silently choosing a value."""
    with db() as connection:
        rows = connection.execute(
            """SELECT f.id,f.fact_key,f.category,f.fact_type,f.summary,f.value_json,f.status,f.occurred_on,
                      f.confidence,f.created_at,d.title AS document_title
               FROM facts f JOIN documents d ON d.id=f.document_id
               WHERE f.fact_key IS NOT NULL AND f.fact_key!=''
               ORDER BY f.fact_key,COALESCE(f.occurred_on,f.created_at),f.id"""
        ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["fact_key"]), []).append(row)
    anomalies: list[dict[str, object]] = []
    for key, items in grouped.items():
        for index, left in enumerate(items):
            try:
                left_value = json.loads(left["value_json"] or "{}").get("amount")
                left_number = float(left_value) if left_value is not None else None
            except (TypeError, ValueError, json.JSONDecodeError):
                left_number = None
            for right in items[index + 1:]:
                same_date = bool(left["occurred_on"] and left["occurred_on"] == right["occurred_on"])
                try:
                    right_value = json.loads(right["value_json"] or "{}").get("amount")
                    right_number = float(right_value) if right_value is not None else None
                except (TypeError, ValueError, json.JSONDecodeError):
                    right_number = None
                outlier = left_number is not None and right_number is not None and min(abs(left_number), abs(right_number)) > 0 and max(abs(left_number), abs(right_number)) / min(abs(left_number), abs(right_number)) >= 2.0
                if same_date or outlier:
                    anomalies.append({
                        "fact_key": key,
                        "kind": "contradiction" if same_date else "numeric_outlier",
                        "facts": [
                            {"id": left["id"], "summary": left["summary"], "status": left["status"], "value_json": left["value_json"], "document_title": left["document_title"]},
                            {"id": right["id"], "summary": right["summary"], "status": right["status"], "value_json": right["value_json"], "document_title": right["document_title"]},
                        ],
                    })
                    if len(anomalies) >= limit:
                        return anomalies
    return anomalies


QUERY_DOMAIN_MARKERS = {
    "money": ("資産", "投資", "株", "投信", "現金", "預金", "積立", "年収", "収入", "支出", "家計", "売却", "購入", "お金"),
    "travel": ("旅行", "旅", "ホテル", "旅館", "温泉", "観光", "連休", "週末", "マイル", "行きたい", "訪問", "宿泊"),
    "housing": ("住居", "家賃", "賃貸", "物件", "間取り", "引っ越", "内見", "最寄", "更新", "部屋"),
    "people": ("人間関係", "恋愛", "相手", "友人", "友達", "会った", "返信", "デート", "連絡", "話した"),
    "work": ("仕事", "勤務", "会社", "職種", "休日", "転職", "キャリア", "働き"),
    "health": ("健康", "体調", "症状", "病院", "薬", "睡眠", "疲労", "疲れ", "運動"),
}
QUERY_STOP_TERMS = {
    "について", "どうする", "どうしたら", "教えて", "相談", "ください", "したい",
    "いる", "ある", "です", "ます", "これ", "それ", "自分", "わたし", "私",
}
DOMAIN_CANONICAL = {
    "money": "finance",
    "finance": "finance",
    "travel": "travel",
    "housing": "housing",
    "people": "relationship",
    "relationship": "relationship",
    "work": "work",
    "health": "health",
    "technology": "technology",
    "development": "technology",
}
DOMAIN_CATEGORY = {key: value for key, value in DOMAIN_CANONICAL.items() if key not in {"finance", "relationship", "technology"}}


def canonical_domain(domain: str | None) -> str:
    value = str(domain or "other").strip().lower()
    return DOMAIN_CANONICAL.get(value, value)


def query_terms(message: str) -> list[str]:
    """Create conservative Japanese lexical terms without requiring a tokenizer."""
    text = str(message or "").strip().lower()
    terms: list[str] = []
    for token in re.findall(r"[a-z0-9][a-z0-9_.:+-]{1,}|[一-龥ぁ-んァ-ヶー]{2,}", text):
        if token not in QUERY_STOP_TERMS:
            terms.append(token)
    for markers in QUERY_DOMAIN_MARKERS.values():
        terms.extend(marker.lower() for marker in markers if marker.lower() in text)
    # Long Japanese runs are difficult for unicode61 FTS. Stable 2-4 character
    # fragments give LIKE/ranking a useful fallback without introducing a
    # language-specific external dependency.
    for run in re.findall(r"[一-龥ぁ-んァ-ヶー]{4,}", text):
        for width in (4, 3, 2):
            for index in range(max(0, len(run) - width + 1)):
                fragment = run[index:index + width]
                if fragment not in QUERY_STOP_TERMS:
                    terms.append(fragment)
    seen: set[str] = set()
    return [term for term in terms if len(term) >= 2 and not (term in seen or seen.add(term))][:40]


def query_plan(message: str) -> dict[str, object]:
    lowered = str(message or "").lower()
    domain_scores = {
        domain: sum(1 for marker in markers if marker.lower() in lowered)
        for domain, markers in QUERY_DOMAIN_MARKERS.items()
    }
    domains = [
        domain for domain, score in sorted(domain_scores.items(), key=lambda item: item[1], reverse=True)
        if score > 0
    ][:2]
    temporal = "history" if any(marker in lowered for marker in ("去年", "以前", "過去", "前回", "当時", "昔")) else "current"
    return {"terms": query_terms(message), "domains": domains, "temporal": temporal}


def _memory_relevance_score(text: str, plan: dict[str, object], *, domain: str = "",
                            current: bool = False, confidence: float | None = None) -> float:
    haystack = str(text or "").lower()
    terms = [str(term) for term in plan.get("terms", [])]
    domains = [str(item) for item in plan.get("domains", [])]
    score = 0.0
    for term in terms:
        if term in haystack:
            score += min(4.0, 1.0 + len(term) / 3.0)
    normalized_domain = {
        "finance": "money", "relationship": "people",
    }.get(domain, domain)
    if normalized_domain and normalized_domain in domains:
        score += 6.0
    if current:
        score += 1.5 if plan.get("temporal") == "current" else 0.25
    elif plan.get("temporal") == "history":
        score += 1.5
    if confidence is not None:
        score += max(0.0, min(1.0, float(confidence)))
    return score


def retrieval_context(message: str, limit: int = 18) -> dict[str, object]:
    """Select only query-relevant current facts, decisions, history and raw evidence."""
    plan = query_plan(message)
    terms = [str(term) for term in plan["terms"]]
    domains = [str(domain) for domain in plan["domains"]]
    categories = [DOMAIN_CATEGORY[domain] for domain in domains if domain in DOMAIN_CATEGORY]
    with db() as connection:
        entity_names = [
            str(row["canonical_name"]) for row in connection.execute(
                "SELECT canonical_name FROM entities WHERE length(canonical_name)>=2 ORDER BY length(canonical_name) DESC"
            )
            if str(row["canonical_name"]).lower() in str(message).lower()
        ][:8]
        fact_parameters: list[object] = []
        fact_filter = ""
        if categories:
            marks = ",".join("?" for _ in categories)
            fact_filter = f" AND f.category IN ({marks})"
            fact_parameters.extend(categories)
        fact_rows = connection.execute(
            f"""SELECT f.id,f.fact_key,f.category,f.fact_type,f.summary,f.value_json,f.status,
                       f.valid_from,f.valid_to,f.occurred_on,f.created_at,f.truth_confidence,
                       f.source_chunk_id,e.canonical_name AS subject,d.title AS document_title
                FROM facts f
                JOIN fact_reviews r ON r.fact_id=f.id
                JOIN documents d ON d.id=f.document_id
                LEFT JOIN entities e ON e.id=f.subject_entity_id
                WHERE r.state='confirmed'
                  AND COALESCE(f.retrieval_eligibility,'pending')='eligible'
                  AND f.personal_relevance='personal'
                  AND f.status IN ('current','superseded','historical')
                  AND NOT EXISTS (SELECT 1 FROM fact_currentness fc WHERE fc.fact_id=f.id AND fc.state='unknown')
                  AND NOT (f.category='relationship' AND COALESCE(f.resolved_entity_type,'unknown')!='person')
                  {fact_filter}
                ORDER BY COALESCE(f.valid_from,f.occurred_on,f.created_at) DESC
                LIMIT 500""",
            fact_parameters,
        ).fetchall()
        decision_parameters: list[object] = []
        decision_filter = ""
        decision_domains = [{"money": "finance", "people": "relationship"}.get(domain, domain) for domain in domains]
        if decision_domains:
            marks = ",".join("?" for _ in decision_domains)
            decision_filter = f" WHERE domain IN ({marks})"
            decision_parameters.extend(decision_domains)
        decision_rows = connection.execute(
            f"""SELECT id,domain,title,question,decision,selected_option,rationale,status,decision_state,decided_on,
                       result,later_evaluation,created_at,related_fact_ids_json,related_entity_ids_json
                FROM decisions{decision_filter}
                ORDER BY COALESCE(decided_on,created_at) DESC LIMIT 300""",
            decision_parameters,
        ).fetchall()
        answer_rows = connection.execute(
            "SELECT id,question,answer,domain,created_at FROM question_answers ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
        raw_rows: list[sqlite3.Row] = []
        raw_terms = [term for term in terms if len(term) >= 2][:12]
        if raw_terms:
            clauses = " OR ".join("(c.text LIKE ? OR d.title LIKE ?)" for _ in raw_terms)
            raw_parameters = [value for term in raw_terms for value in (f"%{term}%", f"%{term}%")]
            raw_rows = connection.execute(
                f"""SELECT c.id AS chunk_id,e.id,e.title,c.text AS body,e.tags,e.kind,e.source,e.created_at,
                           c.speaker_role,c.source_type,
                           (SELECT COUNT(*) FROM facts gf JOIN fact_reviews gr ON gr.fact_id=gf.id
                            WHERE gf.source_chunk_id=c.id AND gr.state='confirmed' AND gf.retrieval_eligibility='eligible') AS confirmed_fact_count,
                           (SELECT COUNT(*) FROM facts cf WHERE cf.source_chunk_id=c.id AND (cf.retrieval_eligibility='conflict' OR EXISTS (SELECT 1 FROM fact_currentness cc WHERE cc.fact_id=cf.id AND cc.state='unknown'))) AS conflict_count
                    FROM chunks c JOIN documents d ON d.id=c.document_id
                    JOIN entries e ON e.id=d.legacy_entry_id
                    WHERE c.is_active=1 AND ({clauses})
                      AND (
                        EXISTS (
                          SELECT 1 FROM facts good LEFT JOIN fact_reviews gr ON gr.fact_id=good.id
                          WHERE good.source_chunk_id=c.id
                            AND (
                              good.personal_relevance='linked_context'
                              OR (
                                good.personal_relevance='personal'
                                AND COALESCE(gr.state,'pending')!='rejected'
                                AND NOT (good.category='relationship' AND COALESCE(good.resolved_entity_type,'unknown')!='person')
                              )
                            )
                        )
                        OR NOT EXISTS (SELECT 1 FROM facts any_fact WHERE any_fact.source_chunk_id=c.id)
                      )
                    ORDER BY e.created_at DESC LIMIT 80""",
                raw_parameters,
            ).fetchall()
    plan["entities"] = entity_names
    scored_facts: list[tuple[float, dict[str, object]]] = []
    for row in fact_rows:
        text = " ".join(str(row[key] or "") for key in ("fact_key", "summary", "value_json", "subject", "document_title"))
        score = _memory_relevance_score(
            text, plan, domain=str(row["category"]), current=row["status"] == "current",
            confidence=row["truth_confidence"],
        )
        if entity_names and any(name.lower() in text.lower() for name in entity_names):
            score += 8.0
        if score <= 0:
            continue
        item = {
            "id": f"fact-{row['id']}",
            "fact_id": row["id"],
            "title": ("現在の情報: " if row["status"] == "current" else "過去の情報: ") + row["summary"],
            "body": f"{row['summary']}\n{row['value_json']}\n有効期間: {row['valid_from'] or row['occurred_on'] or '不明'}〜{row['valid_to'] or '現在'}",
            "tags": f"{row['status']}, {row['category']}, {row['fact_type']}",
            "kind": "current_fact" if row["status"] == "current" else "historical_fact",
            "created_at": row["created_at"],
            "score": round(score, 3),
            "source_chunk_id": row["source_chunk_id"],
        }
        scored_facts.append((score, item))
    scored_facts.sort(key=lambda pair: pair[0], reverse=True)
    current_facts = [item for _, item in scored_facts if item["kind"] == "current_fact"][:6]
    history = [item for _, item in scored_facts if item["kind"] == "historical_fact"][:5]
    scored_decisions: list[tuple[float, dict[str, object]]] = []
    for row in decision_rows:
        text = " ".join(str(row[key] or "") for key in (
            "title", "question", "decision", "selected_option", "rationale", "result", "later_evaluation",
        ))
        score = _memory_relevance_score(text, plan, domain=str(row["domain"]))
        if entity_names and any(name.lower() in text.lower() for name in entity_names):
            score += 8.0
        if score <= 0:
            continue
        body = f"結論: {row['decision']}\n理由: {row['rationale']}"
        if row["result"]:
            body += f"\n結果: {row['result']}"
        if row["later_evaluation"]:
            body += f"\n後日評価: {row['later_evaluation']}"
        scored_decisions.append((score, {
            "id": f"decision-{row['id']}",
            "decision_id": row["id"],
            "title": f"過去の判断: {row['title']}",
            "body": body,
            "tags": f"decision, {row['domain']}",
            "kind": "decision",
            "created_at": row["created_at"],
            "score": round(score, 3),
        }))
    scored_decisions.sort(key=lambda pair: pair[0], reverse=True)
    decisions = [item for _, item in scored_decisions[:5]]
    raw_candidates: dict[str, tuple[float, dict[str, object]]] = {}
    for row in raw_rows:
        text = f"{row['title']} {row['body']} {row['tags']}"
        score = _memory_relevance_score(text, plan)
        if entity_names and any(name.lower() in text.lower() for name in entity_names):
            score += 8.0
        if score <= 0:
            continue
        identity = f"chunk-{row['chunk_id']}"
        raw_candidates[identity] = (score, {
            "id": identity,
            "chunk_id": row["chunk_id"],
            "title": row["title"],
            "body": row["body"],
            "tags": row["tags"],
            "kind": "raw_memory",
            "source_role": row["speaker_role"] or "unknown",
            "source_type": row["source_type"] or row["source"] or "unknown",
            "trust_state": "conflicted" if row["conflict_count"] else ("confirmed_evidence" if row["confirmed_fact_count"] else "unverified"),
            "created_at": row["created_at"],
            "score": round(score, 3),
        })
    for item in semantic_search(message, 12) if terms else []:
        identity = f"semantic-{item.get('id')}"
        semantic_score = float(item.get("score") or 0) * 10
        existing = raw_candidates.get(identity)
        if not existing or semantic_score > existing[0]:
            raw_candidates[identity] = (semantic_score, dict(item) | {
                "id": identity, "kind": "semantic", "trust_state": "semantic_unverified",
            })
    profile_answers: list[dict[str, object]] = []
    for row in answer_rows:
        text = f"{row['question']} {row['answer']}"
        score = _memory_relevance_score(text, plan, domain=str(row["domain"]))
        if score > 0:
            profile_answers.append({
                "id": f"answer-{row['id']}", "title": row["question"], "body": row["answer"],
                "tags": row["domain"], "kind": "profile_answer", "created_at": row["created_at"],
                "score": round(score, 3),
            })
    raw_ranked = [
        item for _, item in sorted(raw_candidates.values(), key=lambda pair: pair[0], reverse=True)
    ][:max(0, limit - len(current_facts) - len(decisions) - len(history))]
    return {
        "query": plan,
        "current": current_facts,
        "decisions": decisions,
        "history": history,
        "profile": profile_answers[:3],
        "raw": raw_ranked,
    }


def relevant_memories(message: str) -> list[dict[str, object]]:
    """Compatibility wrapper returning ordered, query-relevant memory items."""
    context = retrieval_context(message)
    return (
        context["current"] + context["decisions"] + context["history"]
        + context["profile"] + context["raw"]
    )[:18]


def missing_context_for_query(message: str) -> list[dict[str, str]]:
    """Return only high-value, currently missing context for the current query.

    This is intentionally deterministic and conservative: it never creates a
    fact or infers a personal attribute.  The UI can offer these as optional
    follow-up questions when a recommendation would benefit from them.
    """
    text = str(message or "")
    if not text:
        return []
    if any(word in text for word in ("引っ越", "家賃", "住居", "部屋", "賃貸")):
        candidates = [
            ("housing.rent", "現在の家賃", "住居費の比較に必要です"),
            ("finance.asset_balance", "現在の総資産", "無理のない住居費か確認できます"),
            ("finance.monthly_investment", "月間積立額", "資産形成への影響を確認できます"),
        ]
    elif any(word in text for word in ("旅行", "連休", "ホテル", "旅", "どこに行")):
        candidates = [
            ("travel.plan", "旅行の候補日・日数", "候補地と移動負担を比較できます"),
            ("travel.budget", "旅行予算", "費用を含めて提案できます"),
            ("travel.preference", "旅行で重視すること", "過去の好みと照合できます"),
        ]
    elif any(word in text for word in ("株", "投資", "資産", "積立", "売る", "売却")):
        candidates = [
            ("finance.asset_balance", "現在の資産残高", "保有比率を確認できます"),
            ("finance.monthly_investment", "月間積立額", "継続可能性を確認できます"),
            ("finance.holding", "保有理由または整理候補", "売却判断の前提を確認できます"),
        ]
    else:
        candidates = []
    with db() as connection:
        rows = connection.execute(
            """SELECT fact_key, category, fact_type FROM facts f
               JOIN fact_reviews r ON r.fact_id=f.id
               WHERE f.status='current' AND r.state='confirmed'
                 AND COALESCE(f.retrieval_eligibility,'pending')='eligible'"""
        ).fetchall()
    existing = {str(row["fact_key"] or "") for row in rows}
    result = []
    for key, label, reason in candidates:
        if key not in existing and not any(key.split(".")[0] == e.split(".")[0] and key.split(".")[1] == e.split(".")[1] for e in existing if "." in e):
            result.append({"key": key, "label": label, "reason": reason})
    return result[:3]


def ask_openai(message: str, memories: list[dict[str, str]]) -> str | None:
    """OpenAI Responses adapter."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    context = formatted_memory_context(memories)
    payload = {
        "model": setting("openai_model", os.environ.get("PERSONAL_OS_MODEL", "gpt-4.1-mini")),
        "input": [
            {
                "role": "system",
                "content": "あなたは個人用OSの相談相手です。与えられた保存済み記憶を優先し、推測は推測と明示してください。医療・法律・投資の判断を断定せず、資産相談では一般的な情報提供に留めてください。日本語で簡潔に答えてください。",
            },
            {
                "role": "user",
                "content": f"保存済み記憶:\n{context}\n\n相談: {message}",
            },
        ],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json_bytes(payload),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result.get("output_text") or "モデルから本文を取得できませんでした。"
    except urllib.error.HTTPError as error:
        return f"モデル接続エラー: {error.code}"
    except urllib.error.URLError:
        return "モデルに接続できません。ネットワークまたはAPI設定を確認してください。"


def formatted_memory_context(memories: list[dict[str, object]]) -> str:
    labels = {
        "current_fact": "参照した現在情報",
        "historical_fact": "関連する過去情報",
        "decision": "関連する過去判断・結果",
        "profile_answer": "明示的な回答",
        "raw_memory": "関連する原文",
        "semantic": "意味検索で見つかった原文",
    }
    trust_labels = {
        "confirmed_evidence": "confirmed source evidence",
        "unverified": "related raw / unverified",
        "conflicted": "conflicted source",
        "semantic_unverified": "semantic match / unverified",
    }
    blocks = []
    for item in memories:
        trust = trust_labels.get(str(item.get("trust_state") or ""))
        trust_suffix = f"; trust={trust}" if trust else ""
        blocks.append(
            f"[{labels.get(str(item.get('kind')), 'memory')} {item['id']}{trust_suffix}] "
            f"{item['title']}\n{str(item['body'])[:1800]}"
        )
    return "\n\n".join(blocks) or "No related memories."

    return "\n\n".join(
        f"[{labels.get(str(item.get('kind')), '関連記憶')} {item['id']}] {item['title']}\n{str(item['body'])[:1800]}"
        for item in memories
    ) or "関連する保存済み記憶はありません。"


def chat_context(message: str, memories: list[dict[str, object]]) -> str:
    context = formatted_memory_context(memories)
    trust_policy = (
        "Retrieval trust policy: Confirmed current Facts are authoritative for current state. "
        "Historical Facts explain the past. Decisions and Results are separate. "
        "Raw or semantic context is unverified unless labeled confirmed source evidence; "
        "conflicted sources must not overwrite Current Facts.\n"
    )
    return (
        trust_policy +
        "あなたは個人用OSの相談相手です。質問に関係する「参照した現在情報」を優先し、"
        "過去情報・過去判断・原文は履歴として区別してください。結果や後日評価がある判断は次の候補へ反映し、"
        "保存情報からその場で推定した傾向はFactとして断定せず推定と明示してください。"
        "根拠が足りなければ必要最小限の不足情報を示してください。医療・法律・投資の判断を断定せず、"
        "日本語で簡潔に答えてください。\n\n"
        f"保存済み記憶:\n{context}\n\n相談: {message}"
    )


def local_base_url() -> str:
    return setting("local_llm_base_url", os.environ.get("LOCAL_LLM_BASE_URL", "")).rstrip("/")


def local_ollama_autostart_enabled() -> bool:
    return setting(
        "auto_start_local_llm",
        os.environ.get("PERSONAL_OS_AUTO_START_OLLAMA", "true"),
    ).strip().lower() == "true"


def _ollama_native_url() -> str | None:
    base = local_base_url()
    if not base or not ("127.0.0.1:11434" in base or "localhost:11434" in base):
        return None
    return base[:-3] if base.endswith("/v1") else base


def _ollama_reachable(native_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{native_url}/api/tags", timeout=2):
            return True
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False


def ensure_local_llm_available() -> bool:
    """Start local Ollama once when its configured native endpoint is down."""
    global OLLAMA_LAST_START
    native_url = _ollama_native_url()
    if not native_url:
        return False
    if _ollama_reachable(native_url):
        return True
    if not local_ollama_autostart_enabled():
        return False
    with OLLAMA_START_LOCK:
        if _ollama_reachable(native_url):
            return True
        if time.monotonic() - OLLAMA_LAST_START < 30:
            return False
        command = shutil.which("ollama") or shutil.which("ollama.exe")
        if not command:
            candidates = [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
                Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Ollama" / "ollama.exe",
            ]
            command = next((str(path) for path in candidates if path.is_file()), None)
        if not command:
            return False
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                [command, "serve"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                start_new_session=True,
            )
            OLLAMA_LAST_START = time.monotonic()
        except (OSError, ValueError):
            OLLAMA_LAST_START = time.monotonic()
            return False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _ollama_reachable(native_url):
                return True
            time.sleep(0.5)
    return False


def selected_provider(kind: str) -> str:
    default = os.environ.get(f"PERSONAL_OS_{kind.upper()}_PROVIDER", "auto")
    choice = setting(f"{kind}_provider", default).lower()
    if choice != "auto":
        return choice
    # Personal OS is local-first.  A configured Ollama endpoint wins in
    # ``auto`` mode; cloud is only reached through an explicit provider choice
    # or an allow_cloud_fallback_* setting after local execution fails.
    if local_base_url():
        return "local"
    # ``auto`` never selects a cloud provider merely because a key exists.
    # Cloud is available only when explicitly selected or via a purpose-gated
    # fallback after local execution fails.
    return "none"


def provider_status() -> dict[str, object]:
    return {
        "chat_provider": selected_provider("chat"),
        "extract_provider": selected_provider("extract"),
        "extract_parallel_providers": extraction_providers(),
        "extract_parallel_config": setting("extract_parallel_providers", ""),
        "providers": {
            "openai": bool(os.environ.get("OPENAI_API_KEY")),
            "gemini": bool(os.environ.get("GEMINI_API_KEY")),
            "local": bool(local_base_url()),
        },
        "api_keys": {"openai": bool(os.environ.get("OPENAI_API_KEY")), "gemini": bool(os.environ.get("GEMINI_API_KEY")), "storage": "process-memory-only"},
        "models": {
            "openai": setting("openai_model", os.environ.get("PERSONAL_OS_MODEL", "gpt-4.1-mini")),
            "gemini": setting("gemini_model", os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")),
            "local": setting("local_llm_model", os.environ.get("LOCAL_LLM_MODEL", "qwen3.5:9b")),
        },
        "local_base_url": local_base_url(),
        "local_auto_start": local_ollama_autostart_enabled(),
        "analysis_paused": analysis_paused(),
        "analysis_batch_size": analysis_batch_size(),
        "cloud_fallback": {
            "chat": cloud_fallback_allowed("chat"),
            "note": cloud_fallback_allowed("note"),
            "import": cloud_fallback_allowed("import"),
            "sensitive": sensitive_cloud_allowed(),
        },
    }


def provider_configured(kind: str) -> bool:
    provider = selected_provider(kind)
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    if provider == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY"))
    if provider == "local":
        return bool(local_base_url())
    return False


def extraction_providers() -> list[str]:
    """Return extraction workers explicitly enabled for parallel execution.

    Empty configuration preserves the legacy single-provider behavior.  The
    parallel list is never inferred from API keys, so merely configuring a
    key cannot start an unexpected cloud job.
    """
    configured = setting("extract_parallel_providers", "").strip()
    names = [item.strip().lower() for item in configured.split(",") if item.strip()]
    allowed = {"local", "openai", "gemini"}
    if names:
        result = []
        for name in names:
            if name not in allowed or name in result:
                continue
            if name == "local" and not local_base_url():
                continue
            if name in {"openai", "gemini"} and not os.environ.get(f"{name.upper()}_API_KEY"):
                continue
            result.append(name)
        return result
    provider = selected_provider("extract")
    return [provider] if provider != "none" and provider_configured("extract") else []


def cloud_fallback_allowed(purpose: str) -> bool:
    key = {
        "chat": "allow_cloud_fallback_chat",
        "note": "allow_cloud_fallback_note",
        "import": "allow_cloud_fallback_import",
    }[purpose]
    return setting(key, "false").lower() == "true"


def sensitive_cloud_allowed() -> bool:
    return setting("allow_sensitive_cloud", "false").lower() == "true"


def ask_gemini(message: str, memories: list[dict[str, str]]) -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": setting("gemini_model", os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")),
        "input": chat_context(message, memories),
        "store": False,
    }
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        data=json_bytes(payload),
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return interaction_text(json.loads(response.read().decode("utf-8"))) or "モデルから本文を取得できませんでした。"
    except urllib.error.HTTPError as error:
        return f"Gemini接続エラー: {error.code}"
    except urllib.error.URLError:
        return "Geminiに接続できません。ネットワークまたはAPI設定を確認してください。"


def local_chat_completion(messages: list[dict[str, object]], response_format: dict | None = None,
                          max_predict: int = 1024, images: list[bytes] | None = None) -> str | None:
    ensure_local_llm_available()
    client = OllamaClient(
        local_base_url(),
        setting("local_llm_model", os.environ.get("LOCAL_LLM_MODEL", "qwen3.5:9b")),
        os.environ.get("LOCAL_LLM_API_KEY"),
    )
    return client.chat(messages, response_format, max_predict, images)


def unload_local_model() -> bool:
    client = OllamaClient(
        local_base_url(),
        setting("local_llm_model", os.environ.get("LOCAL_LLM_MODEL", "qwen3.5:9b")),
        os.environ.get("LOCAL_LLM_API_KEY"),
    )
    return client.unload()


def ask_local(message: str, memories: list[dict[str, str]]) -> str | None:
    try:
        return local_chat_completion([{"role": "user", "content": chat_context(message, memories)}]) or "ローカルLLMから本文を取得できませんでした。"
    except urllib.error.HTTPError:
        return None
    except urllib.error.URLError:
        return None


def ask_model(message: str, memories: list[dict[str, str]], request_id: str = "") -> str | None:
    provider = selected_provider("chat")
    model = provider_model(provider, "chat")
    started = time.perf_counter()
    record_llm_trace("provider_selected", provider=provider, model=model, request_id=request_id)
    adapter = resolve_llm_provider(provider)
    if provider in {"openai", "gemini"} and adapter:
        record_llm_trace("llm_started", provider=provider, model=model, request_id=request_id)
        answer = adapter.chat(message, memories)
        record_llm_trace("llm_completed", provider=provider, model=model, request_id=request_id,
                         duration_ms=(time.perf_counter() - started) * 1000)
        return answer
    if provider == "local":
        record_llm_trace("llm_started", provider=provider, model=model, request_id=request_id)
        try:
            answer = adapter.chat(message, memories) if adapter else None
        except Exception:
            answer = None
            record_llm_trace("llm_error", provider=provider, model=model, request_id=request_id, error_type="provider_unavailable")
        if answer:
            record_llm_trace("llm_completed", provider=provider, model=model, request_id=request_id,
                             duration_ms=(time.perf_counter() - started) * 1000)
            return answer
        has_sensitive_context = any("relationship" in str(item.get("tags", "")) for item in memories)
        if os.environ.get("GEMINI_API_KEY") and cloud_fallback_allowed("chat") and (sensitive_cloud_allowed() or not has_sensitive_context):
            record_llm_trace("provider_selected", provider="gemini", model=provider_model("gemini", "chat"), request_id=request_id)
            return ask_gemini(message, memories)
        return "ローカルLLMに接続できません。Ollamaの起動状態とURLを確認してください。"
    return None


MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Use an existing Personal OS category slug when possible (finance, travel, housing, relationship, work, health, lifestyle, learning, hobby, food, shopping, other)."},
                    "type": {"type": "string", "description": "transaction, plan, schedule, preference, status, income, asset_balance, holding, goal, event, task, or note"},
                    "asset": {"type": ["string", "null"]},
                    "amount": {"type": ["number", "null"]},
                    "currency": {"type": ["string", "null"]},
                    "date": {"type": ["string", "null"], "description": "Use YYYY-MM-DD or YYYY-MM when known"},
                    "entity_type": {"type": ["string", "null"], "description": "candidate only: person, organization, place, product, service, brand, fictional_character, media_character, ai_character, work, project, asset, or unknown"},
                    "subject_scope": {"type": ["string", "null"], "description": "self, person, fictional_character, reference, asset, or unknown"},
                    "personal_relevance": {"type": ["boolean", "null"]},
                    "summary": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_quote": {"type": ["string", "null"], "description": "短い原文引用。原文にない推測は記録しない"},
                    "evidence_strength": {"type": "string", "enum": ["explicit", "uncertain", "inferred"]},
                    "details": {"type": "object", "additionalProperties": True},
                },
                "required": ["category", "type", "asset", "amount", "currency", "date", "summary", "confidence", "details"],
            },
        }
    },
    "required": ["facts"],
}


def interaction_text(payload: dict) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    texts = []
    for step in payload.get("steps", []):
        if step.get("type") == "model_output":
            texts.extend(str(item["text"]) for item in step.get("content", []) if item.get("type") == "text")
    return "".join(texts)


def extract_with_gemini(text: str) -> list[dict] | None:
    """Extract facts with Gemini, keeping requests stateless and keys out of the database."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    prompt = extraction_prompt(text)
    payload = {
        "model": setting("gemini_model", os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")),
        "input": prompt,
        "store": False,
        "response_format": {"type": "text", "mime_type": "application/json", "schema": MEMORY_SCHEMA},
    }
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        data=json_bytes(payload),
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            parsed = json.loads(response.read().decode("utf-8"))
        result = json.loads(interaction_text(parsed))
        facts = result.get("facts", [])
        return facts if isinstance(facts, list) else []
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise ValueError(f"Gemini抽出に失敗しました: {error}") from error


def extraction_prompt(text: str) -> str:
    return (
        "次の日本語入力から、長期的に役立つ個人メモリの事実を抽出してください。"
        "推測や補完はせず、本文に明記された固有名詞は asset に必ず入れてください。"
        "取引は asset・数値の amount・currency・date を、本文にあれば抽出します。"
        "現在の総資産・残高は asset_balance、年収は income、毎月の積立や将来の予定は plan、"
        "完了した一回限りの取引は transaction にします。健康情報は本人が明示した症状・薬・受診・検査値・診断だけを抽出し、病名を推測しないでください。"
        "指定スキーマに一致するJSONだけを返してください。Evidenceが直接確認できる場合だけevidence_quoteを付け、推測・可能性・診断の推定はfactsから除外してください。入力:\n" + text
    )


def extract_with_openai(text: str) -> list[dict] | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": setting("openai_model", os.environ.get("PERSONAL_OS_MODEL", "gpt-4.1-mini")),
        "input": extraction_prompt(text),
        "text": {"format": {"type": "json_schema", "name": "memory_facts", "schema": MEMORY_SCHEMA, "strict": True}},
        "store": False,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json_bytes(payload),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
        facts = json.loads(result.get("output_text", "{}")).get("facts", [])
        return facts if isinstance(facts, list) else []
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise ValueError(f"OpenAI抽出に失敗しました: {error}") from error


def extract_with_local(text: str) -> list[dict] | None:
    try:
        result = local_chat_completion(
            [{"role": "user", "content": extraction_prompt(text) + "\nExtract at most 8 durable facts. Return compact JSON only.\nSchema:\n" + json.dumps(MEMORY_SCHEMA, ensure_ascii=False)}],
            {"type": "json_object"},
            max_predict=2048,
        )
        try:
            facts = json.loads(result or "{}").get("facts", [])
        except json.JSONDecodeError:
            # A constrained repair pass avoids abandoning a long import solely
            # because a local model emitted a truncated or fenced JSON object.
            repaired = local_chat_completion(
                [{"role": "user", "content": "Return valid compact JSON only. Repair this fact-extraction output without adding facts:\n" + (result or "")[:12000]}],
                {"type": "json_object"},
                max_predict=2048,
            )
            facts = json.loads(repaired or "{}").get("facts", [])
        return facts if isinstance(facts, list) else []
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise ValueError(f"ローカルLLM抽出に失敗しました: {error}") from error


def extract_image_facts(image: bytes, context: str) -> list[dict]:
    """Extract only visible, durable facts from a screenshot with local Ollama vision."""
    prompt = (
        "You are extracting durable personal facts from a screenshot for a private Personal OS. "
        "Use the user's context to decide what matters: " + (context or "general information") + "\n"
        "Read the image directly. Do not infer unreadable text, identities, or values. "
        "Extract at most 10 facts that are explicitly visible and relevant. "
        "Use category=finance for money/asset information, travel for trips, housing for home, "
        "relationship only for explicitly supplied people facts, health only for explicitly visible health information without diagnosis, and other otherwise. "
        "Return compact JSON only following this schema:\n" + json.dumps(MEMORY_SCHEMA, ensure_ascii=False)
    )
    try:
        result = local_chat_completion(
            [{"role": "user", "content": prompt}], {"type": "json_object"}, max_predict=2048, images=[image]
        )
        parsed = json.loads(result or "{}")
        facts = parsed.get("facts", [])
        return facts if isinstance(facts, list) else []
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise ValueError(f"Local vision extraction failed: {error}") from error


_BASE_EXTRACTION_PROMPT = extraction_prompt


def extraction_prompt(text: str) -> str:
    return _BASE_EXTRACTION_PROMPT(text) + (
        "\n最重要: Personal OSへ保存するのは、user: の発言で本人が明示した本人自身の状態・行動・所有・好み・予定だけです。"
        "assistant: の説明、一般知識、企業業績、相場、試算、推薦、質問文に現れただけの候補を本人Factにしないでください。"
        "質問への回答から本人の属性を補完せず、該当する本人Factがなければ必ず {\"facts\":[]} を返してください。"
        "personal_relevance=true はEvidence引用内に本人の明示がある場合だけです。モデル自身の判断だけでtrueにしないでください。"
        "\nEntity resolution rules: distinguish Mention from Relationship. "
        "Use entity_type=person only for an explicitly real person connected to the user or a real interpersonal relationship. "
        "Anime/manga/game characters, mascots, fictional or media characters, projects, organizations, products, places, brands, and general knowledge are not People. "
        "For those use the appropriate entity_type and set personal_relevance=false. Do not force relationship category. "
        "Return evidence_strength=explicit only when directly stated; use inferred only when the source is an inference.\n"
    )


def extract_with_model(text: str, purpose: str = "note", provider_override: str | None = None) -> list[dict] | None:
    provider = provider_override or selected_provider("extract")
    model = provider_model(provider, "extract")
    started = time.perf_counter()
    record_llm_trace("provider_selected", provider=provider, model=model)
    adapter = resolve_llm_provider(provider)
    if provider in {"gemini", "openai"} and adapter:
        record_llm_trace("llm_started", provider=provider, model=model)
        result = adapter.extract(text)
        record_llm_trace("response_parsed", provider=provider, model=model,
                         duration_ms=(time.perf_counter() - started) * 1000)
        return result
    if provider == "local":
        try:
            record_llm_trace("llm_started", provider=provider, model=model)
            result = extract_with_local(text) if adapter else None
            record_llm_trace("response_parsed", provider=provider, model=model,
                             duration_ms=(time.perf_counter() - started) * 1000)
            return result
        except ValueError:
            sensitive_markers = ("恋愛", "人間関係", "彼女", "彼氏", "家族", "住所", "電話", "病気")
            sensitive = purpose == "import" or any(marker in text for marker in sensitive_markers)
            allowed = cloud_fallback_allowed(purpose) and (sensitive_cloud_allowed() or not sensitive)
            if os.environ.get("GEMINI_API_KEY") and allowed:
                record_llm_trace("provider_selected", provider="gemini", model=provider_model("gemini", "extract"))
                return extract_with_gemini(text)
            raise
    return None


class LLMProvider(Protocol):
    """Small provider boundary; routing remains in this modular monolith."""

    name: str

    def chat(self, message: str, memories: list[dict[str, str]]) -> str | None: ...

    def extract(self, text: str) -> list[dict] | None: ...


class OpenAIProvider:
    name = "openai"

    def chat(self, message: str, memories: list[dict[str, str]]) -> str | None:
        return ask_openai(message, memories)

    def extract(self, text: str) -> list[dict] | None:
        return extract_with_openai(text)


class GeminiProvider:
    name = "gemini"

    def chat(self, message: str, memories: list[dict[str, str]]) -> str | None:
        return ask_gemini(message, memories)

    def extract(self, text: str) -> list[dict] | None:
        return extract_with_gemini(text)


class OllamaProvider:
    name = "local"

    def chat(self, message: str, memories: list[dict[str, str]]) -> str | None:
        return ask_local(message, memories)

    def extract(self, text: str) -> list[dict] | None:
        return extract_with_local(text)


def resolve_llm_provider(name: str) -> LLMProvider | None:
    providers: dict[str, LLMProvider] = {
        "openai": OpenAIProvider(),
        "gemini": GeminiProvider(),
        "local": OllamaProvider(),
    }
    return providers.get(name)


PROMPT_VERSION = "memory-facts-jp-v3"
TRANSACTION_VALIDATOR_VERSION = "finance-validator-v1"


def provider_model(provider: str, purpose: str = "default") -> str:
    return str(provider_status()["models"].get(provider, "unknown"))


def normalize_transaction_amount(amount: object, currency: object, raw_text: str = "") -> dict[str, object]:
    """Normalize Japanese currency units without losing the original expression."""
    try:
        numeric = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        numeric = None
    currency_text = str(currency or "").strip()
    source = raw_text.strip()
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?\s*(億円|万円|千円|円|JPY|USD|EUR)", source, re.IGNORECASE)
    if match:
        source = match.group(0)
        if numeric is None:
            try:
                numeric = float(match.group(0).replace(",", "").replace(match.group(1), "").strip())
            except ValueError:
                numeric = None
        if not currency_text:
            currency_text = match.group(1)
    unit = ""
    multiplier = 1.0
    if currency_text in {"億円", "億"}:
        unit, multiplier, currency_text = "億円", 100_000_000.0, "JPY"
    elif currency_text in {"万円", "万"}:
        unit, multiplier, currency_text = "万円", 10_000.0, "JPY"
    elif currency_text in {"千円", "千"}:
        unit, multiplier, currency_text = "千円", 1_000.0, "JPY"
    elif currency_text in {"円", "JPY", "jpy"}:
        unit, currency_text = "円", "JPY"
    if not source and numeric is not None:
        source = f"{numeric:g}{unit or currency_text}"
    normalized = numeric * multiplier if numeric is not None and currency_text == "JPY" else None
    return {"raw_amount_text": source, "normalized_amount": normalized, "currency": currency_text or "", "unit": unit}


def validate_transaction_candidate(fact: sqlite3.Row | dict, evidence: str = "") -> dict[str, object]:
    """Deterministically decide whether a finance fact is a user transaction."""
    row = dict(fact)
    try:
        value = json.loads(row.get("value_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    details = value.get("details") if isinstance(value.get("details"), dict) else {}
    summary = str(row.get("summary") or "")
    title = str(row.get("document_title") or "")
    text = f"{summary} {title} {evidence}".lower()
    amount_info = normalize_transaction_amount(value.get("amount"), value.get("currency"), str(details.get("raw_amount_text") or summary))
    confidence = float(row.get("confidence") or 0.0)
    excluded_markers = {
        "company_financials": ("売上", "営業利益", "経常利益", "純利益", "赤字", "業績予想", "企業"),
        "asset_price": ("物件価格", "購入価格", "価格は", "価格（", "価格:"),
        "dividend_per_share": ("1株あたり", "一株あたり", "1株当たり"),
        "simulation": ("シミュレーション", "試算", "仮に", "の場合", "返済例", "予測", "予想", "将来"),
        "market_data": ("株価", "時価", "市場価格", "ベンチマーク", "統計"),
        "loan_example": ("借入", "ローン", "月々の返済額", "返済額"),
    }
    if row.get("category") != "finance" or row.get("fact_type") != "transaction":
        return {"state": "excluded", "actor": "unknown", "is_actual": False, "kind": "", "reason": "not_transaction", **amount_info}
    for reason, markers in excluded_markers.items():
        if any(marker.lower() in text for marker in markers):
            # An explicit completed repayment/purchase overrides generic loan/price words.
            if reason == "loan_example" and any(marker in text for marker in ("返済した", "返済しました", "支払った", "支払いました")):
                break
            return {"state": "excluded", "actor": "company" if reason == "company_financials" else "unknown", "is_actual": False, "kind": "", "reason": reason, **amount_info}
    actual_markers = ("入れた", "入金した", "出金した", "購入した", "買った", "買い付けた", "売った", "売却した", "受け取った", "受領した", "積み立てた", "積立を実行", "返済した", "支払った", "引き落とされた")
    if not any(marker in text for marker in actual_markers):
        reason = "repayment_example" if any(marker in text for marker in ("返済", "月々")) else "uncertain"
        return {"state": "excluded" if reason == "repayment_example" else "pending", "actor": "unknown", "is_actual": False if reason == "repayment_example" else None, "kind": "", "reason": reason, **amount_info}
    kind = "transfer"
    if any(marker in text for marker in ("購入", "買った", "買い付け")):
        kind = "buy"
    elif any(marker in text for marker in ("売却", "売った")):
        kind = "sell"
    elif any(marker in text for marker in ("積立", "持株会", "投資")):
        kind = "investment"
    elif "配当" in text:
        kind = "dividend"
    elif "利息" in text:
        kind = "interest"
    elif "手数料" in text:
        kind = "fee"
    elif "返済" in text:
        kind = "repayment"
    elif "入金" in text:
        kind = "deposit"
    elif "出金" in text:
        kind = "withdrawal"
    if amount_info["normalized_amount"] is None or not amount_info["currency"]:
        return {"state": "pending", "actor": "self", "is_actual": True, "kind": kind, "reason": "invalid_amount", **amount_info}
    state = "auto_confirmed" if confidence >= TRANSACTION_CONFIDENCE_THRESHOLD else "pending"
    return {"state": state, "actor": "self", "is_actual": True, "kind": kind, "reason": "eligible" if state == "auto_confirmed" else "low_confidence", **amount_info}


def _fact_with_source(connection: sqlite3.Connection, fact_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT f.*, d.title AS document_title, c.text AS evidence
           FROM facts f LEFT JOIN documents d ON d.id=f.document_id
           LEFT JOIN chunks c ON c.id=f.source_chunk_id WHERE f.id=?""", (fact_id,)
    ).fetchone()


def sync_finance_transaction(connection: sqlite3.Connection, fact_id: int, confirmed: bool = False) -> dict[str, object]:
    """Validate one fact and upsert only eligible rows into finance_transactions."""
    fact = _fact_with_source(connection, fact_id)
    if not fact:
        return {"state": "excluded", "reason": "missing_fact"}
    validation = validate_transaction_candidate(fact, fact["evidence"] or "")
    if validation["state"] == "pending" and confirmed:
        validation["state"] = "confirmed"
    connection.execute(
        """UPDATE finance_transactions SET normalized_amount=?,currency=?,unit=?,raw_amount_text=?,transaction_kind=?,actor=?,is_actual=?,eligibility_state=?,eligibility_reason=?,validator_version=?,validated_at=? WHERE fact_id=?""",
        (validation.get("normalized_amount"), validation.get("currency") or "", validation.get("unit") or "", validation.get("raw_amount_text") or "", validation.get("kind") or "", validation.get("actor") or "unknown", 1 if validation.get("is_actual") is True else 0 if validation.get("is_actual") is False else None, validation["state"], validation.get("reason") or "", TRANSACTION_VALIDATOR_VERSION, now(), fact_id),
    )
    if validation["state"] in {"pending", "excluded"}:
        connection.execute(
            """INSERT INTO finance_transaction_candidates(fact_id,asset_entity_id,amount,normalized_amount,currency,unit,raw_amount_text,transaction_kind,actor,is_actual,eligibility_state,eligibility_reason,validator_version,validated_at,occurred_on)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(fact_id) DO UPDATE SET asset_entity_id=excluded.asset_entity_id,amount=excluded.amount,normalized_amount=excluded.normalized_amount,currency=excluded.currency,unit=excluded.unit,raw_amount_text=excluded.raw_amount_text,transaction_kind=excluded.transaction_kind,actor=excluded.actor,is_actual=excluded.is_actual,eligibility_state=excluded.eligibility_state,eligibility_reason=excluded.eligibility_reason,validator_version=excluded.validator_version,validated_at=excluded.validated_at,occurred_on=excluded.occurred_on""",
            (fact_id, fact["subject_entity_id"], float(json.loads(fact["value_json"] or "{}").get("amount") or 0), validation.get("normalized_amount"), validation.get("currency") or "", validation.get("unit") or "", validation.get("raw_amount_text") or "", validation.get("kind") or "", validation.get("actor") or "unknown", 1 if validation.get("is_actual") is True else 0 if validation.get("is_actual") is False else None, validation["state"], validation.get("reason") or "", TRANSACTION_VALIDATOR_VERSION, now(), fact["occurred_on"]),
        )
        return validation
    connection.execute("DELETE FROM finance_transaction_candidates WHERE fact_id=?", (fact_id,))
    if validation["state"] in {"auto_confirmed", "confirmed"} and validation.get("normalized_amount") is not None and validation.get("actor") == "self":
        connection.execute(
            """INSERT INTO finance_transactions(fact_id,asset_entity_id,amount,normalized_amount,currency,unit,raw_amount_text,transaction_type,transaction_kind,actor,is_actual,eligibility_state,eligibility_reason,validator_version,validated_at,occurred_on)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(fact_id) DO UPDATE SET asset_entity_id=excluded.asset_entity_id,amount=excluded.amount,normalized_amount=excluded.normalized_amount,currency=excluded.currency,unit=excluded.unit,raw_amount_text=excluded.raw_amount_text,transaction_type=excluded.transaction_type,transaction_kind=excluded.transaction_kind,actor=excluded.actor,is_actual=excluded.is_actual,eligibility_state=excluded.eligibility_state,eligibility_reason=excluded.eligibility_reason,validator_version=excluded.validator_version,validated_at=excluded.validated_at,occurred_on=excluded.occurred_on""",
            (fact_id, fact["subject_entity_id"], float(json.loads(fact["value_json"] or "{}").get("amount") or 0), validation.get("normalized_amount"), validation.get("currency") or "", validation.get("unit") or "", validation.get("raw_amount_text") or "", "transaction", validation.get("kind") or "", validation.get("actor") or "unknown", 1, validation["state"], validation.get("reason") or "", TRANSACTION_VALIDATOR_VERSION, now(), fact["occurred_on"]),
        )
    return validation


def evidence_source_identity(connection: sqlite3.Connection, *, source_chunk_id: int | None,
                             source_attachment_id: int | None, fallback_group: str = "") -> tuple[str, str]:
    """Return exact-source identity and an independence group.

    Multiple chunks from one exported conversation belong to one group, while
    a duplicated screenshot belongs to the same content-hash group regardless
    of how many times it was uploaded.
    """
    if source_attachment_id:
        row = connection.execute(
            "SELECT content_hash FROM attachments WHERE id=?", (source_attachment_id,)
        ).fetchone()
        if row and row["content_hash"]:
            identity = f"attachment:{row['content_hash']}"
            return identity, identity
    if source_chunk_id:
        row = connection.execute(
            """SELECT c.text_hash,c.document_id,d.source,e.external_id
               FROM chunks c JOIN documents d ON d.id=c.document_id
               LEFT JOIN entries e ON e.id=d.legacy_entry_id WHERE c.id=?""",
            (source_chunk_id,),
        ).fetchone()
        if row:
            identity = f"chunk:{row['text_hash']}"
            document_identity = row["external_id"] or f"{row['source']}:{row['document_id']}"
            return identity, f"document:{document_identity}"
    fallback = fallback_group[:160] or "unknown"
    return f"fallback:{fallback}", f"fallback:{fallback}"


def record_fact_evidence(connection: sqlite3.Connection, fact_id: int, *, source_chunk_id: int | None,
                         source_attachment_id: int | None, quote: str = "", evidence_kind: str = "conversation",
                         source_group: str = "", support: str = "supports", reliability: float | None = None) -> None:
    """Persist provenance as an independent evidence row.

    A chunk/attachment is immutable source material; this table is the auditable
    link used by retrieval and UI.  Re-running the same extractor does not create
    an unbounded duplicate list for the same fact/source/model pass.
    """
    support = support if support in {"supports", "contradicts", "context"} else "supports"
    source_identity, independence_group = evidence_source_identity(
        connection,
        source_chunk_id=source_chunk_id,
        source_attachment_id=source_attachment_id,
        fallback_group=source_group,
    )
    connection.execute(
        """INSERT INTO fact_evidence(fact_id,evidence_kind,source_chunk_id,source_attachment_id,source_group,source_identity,quote,support,reliability,created_at)
           SELECT ?,?,?,?,?,?,?,?,?,?
           WHERE NOT EXISTS (
             SELECT 1 FROM fact_evidence WHERE fact_id=? AND source_identity=? AND support=?
           )""",
        (fact_id, evidence_kind, source_chunk_id, source_attachment_id, independence_group, source_identity,
         quote[:4000], support, reliability, now(), fact_id, source_identity, support),
    )


def backfill_fact_evidence_identities(connection: sqlite3.Connection) -> int:
    changed = 0
    rows = connection.execute(
        """SELECT id,source_chunk_id,source_attachment_id,source_group
           FROM fact_evidence WHERE source_identity='' OR source_group=''"""
    ).fetchall()
    for row in rows:
        identity, group = evidence_source_identity(
            connection,
            source_chunk_id=row["source_chunk_id"],
            source_attachment_id=row["source_attachment_id"],
            fallback_group=row["source_group"],
        )
        cursor = connection.execute(
            "UPDATE fact_evidence SET source_identity=?,source_group=? WHERE id=?",
            (identity, group, row["id"]),
        )
        changed += cursor.rowcount
    return changed


def fact_trust_evaluation(connection: sqlite3.Connection, fact_id: int, fact: dict,
                          source_created_at: str | None = None) -> dict[str, object]:
    """Evaluate truth confidence from independent Evidence rather than row count."""
    evidence_rows = connection.execute(
        """SELECT id,evidence_kind,source_group,source_identity,quote,support,reliability,created_at
           FROM fact_evidence WHERE fact_id=? ORDER BY created_at,id""",
        (fact_id,),
    ).fetchall()
    grouped: dict[tuple[str, str], sqlite3.Row] = {}
    for row in evidence_rows:
        identity = str(row["source_identity"] or f"legacy:{row['id']}")
        key = (identity, str(row["support"]))
        existing = grouped.get(key)
        if not existing or float(row["reliability"] or 0) > float(existing["reliability"] or 0):
            grouped[key] = row
    independent_support_groups = {
        str(row["source_group"] or row["source_identity"])
        for row in grouped.values() if row["support"] == "supports"
    }
    independent_contradiction_groups = {
        str(row["source_group"] or row["source_identity"])
        for row in grouped.values() if row["support"] == "contradicts"
    }
    support_rows = [row for row in grouped.values() if row["support"] == "supports"]
    quotes = "\n".join(str(row["quote"] or "") for row in support_rows)
    explicit = evidence_is_sufficient(fact, quotes)
    extraction = max(0.0, min(1.0, float(fact.get("confidence") or 0.0)))
    reliabilities = [max(0.0, min(1.0, float(row["reliability"] or 0.0))) for row in support_rows]
    reliability = statistics.mean(reliabilities) if reliabilities else 0.0
    source_kinds = {str(row["evidence_kind"]) for row in support_rows}
    score = extraction * 0.45
    if explicit:
        score += 0.25
    if independent_support_groups:
        score += 0.08
    score += min(0.12, max(0, len(independent_support_groups) - 1) * 0.06)
    score += reliability * 0.08
    if len(source_kinds) > 1:
        score += 0.04
    reasons = [
        f"抽出信頼度 {extraction:.2f}",
        f"独立支持 {len(independent_support_groups)}件",
        f"反証 {len(independent_contradiction_groups)}件",
    ]
    if explicit:
        reasons.append("原文に明示Evidenceあり")
    else:
        reasons.append("原文の明示性不足")
    if independent_contradiction_groups:
        score -= min(0.55, 0.28 + 0.12 * len(independent_contradiction_groups))
        reasons.append("独立した反証Evidenceあり")
    if fact_policy(fact) == "exclude" or is_ai_speculation(fact):
        score = 0.0
        reasons.append("非Factまたは推測")
    occurred_on = str(fact.get("occurred_on") or "")
    if occurred_on and re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", occurred_on):
        try:
            occurrence = datetime.fromisoformat(occurred_on + ("-01" if len(occurred_on) == 7 else ""))
            if occurrence.date() > datetime.now().date() + timedelta(days=2) and fact.get("type") == "transaction":
                score -= 0.25
                reasons.append("実取引の日付が未来")
        except ValueError:
            reasons.append("日付形式を解釈できない")
    score = max(0.0, min(1.0, score))
    return {
        "score": round(score, 4),
        "support_count": len(independent_support_groups),
        "contradiction_count": len(independent_contradiction_groups),
        "explicit": explicit,
        "evidence_ids": [int(row["id"]) for row in grouped.values()],
        "source_groups": sorted(independent_support_groups),
        "reasons": reasons,
    }


def reevaluate_finance_transactions() -> dict[str, int]:
    """Reclassify legacy rows without deleting them."""
    with db() as connection:
        rows = connection.execute("SELECT fact_id FROM finance_transactions ORDER BY fact_id").fetchall()
        counts = {state: 0 for state in TRANSACTION_STATES}
        for row in rows:
            result = sync_finance_transaction(connection, row["fact_id"])
            state = str(result.get("state", "pending"))
            counts[state] = counts.get(state, 0) + 1
        return counts


def eligible_finance_transactions(connection: sqlite3.Connection, include_pending: bool = False) -> list[dict]:
    states = ("auto_confirmed", "confirmed") if not include_pending else ("auto_confirmed", "confirmed", "pending")
    marks = ",".join("?" for _ in states)
    eligibility_filter = "" if include_pending else " AND t.actor='self' AND t.is_actual=1"
    rows = connection.execute(
        f"""SELECT t.*,f.summary,f.fact_key,f.status AS fact_status,f.confidence,f.source_chunk_id,
                  e.canonical_name AS asset,d.title AS document_title,c.text AS evidence,r.state AS review_state
           FROM finance_transactions t JOIN facts f ON f.id=t.fact_id
           LEFT JOIN entities e ON e.id=t.asset_entity_id
           LEFT JOIN documents d ON d.id=f.document_id LEFT JOIN chunks c ON c.id=f.source_chunk_id
           LEFT JOIN fact_reviews r ON r.fact_id=f.id
           WHERE t.eligibility_state IN ({marks}){eligibility_filter}
             AND COALESCE(r.state,'pending') != 'rejected'
           ORDER BY COALESCE(t.occurred_on,f.occurred_on,f.created_at) DESC""",
        states,
    ).fetchall()
    result = [dict(row) for row in rows]
    if include_pending:
        candidates = connection.execute(
            """SELECT c.*,f.summary,f.fact_key,f.status AS fact_status,f.confidence,f.source_chunk_id,
                      e.canonical_name AS asset,d.title AS document_title,ch.text AS evidence,r.state AS review_state
               FROM finance_transaction_candidates c JOIN facts f ON f.id=c.fact_id
               LEFT JOIN entities e ON e.id=c.asset_entity_id LEFT JOIN documents d ON d.id=f.document_id
               LEFT JOIN chunks ch ON ch.id=f.source_chunk_id LEFT JOIN fact_reviews r ON r.fact_id=f.id
               WHERE c.eligibility_state='pending' AND COALESCE(r.state,'pending') != 'rejected'
               ORDER BY COALESCE(c.occurred_on,f.occurred_on,f.created_at) DESC"""
        ).fetchall()
        result.extend(dict(row) for row in candidates)
    return result


def save_structured_facts(entry_id: int, facts: list[dict], extractor: str | None = None, model: str | None = None,
                          prompt_version: str = PROMPT_VERSION, source_attachment_id: int | None = None,
                          user_confirmed: bool = False, source_chunk_id: int | None = None) -> list[dict]:
    saved = []
    document_id = ensure_document_for_entry(entry_id)
    extractor = extractor or selected_provider("extract")
    model = model or provider_model(extractor)
    with db() as connection:
        chunk = None
        if source_chunk_id:
            chunk = connection.execute(
                "SELECT id,text FROM chunks WHERE id=? AND document_id=? AND is_active=1",
                (source_chunk_id, document_id),
            ).fetchone()
        if not chunk:
            chunk = connection.execute(
                "SELECT id,text FROM chunks WHERE document_id=? AND is_active=1 ORDER BY ordinal LIMIT 1",
                (document_id,),
            ).fetchone()
        document_row = connection.execute(
            "SELECT source_created_at FROM documents WHERE id=?", (document_id,)
        ).fetchone()
        source_created_at = document_row["source_created_at"] if document_row else None
        for fact in facts[:20]:
            category = ensure_memory_category(connection, fact.get("category", "other"))
            fact_type = str(fact.get("type", "note"))[:80]
            asset = fact.get("asset")
            amount = fact.get("amount")
            try:
                amount = float(amount) if amount is not None else None
            except (TypeError, ValueError):
                amount = None
            asset_name = str(asset)[:160] if asset else None
            currency = str(fact.get("currency"))[:16] if fact.get("currency") else None
            occurred_on = str(fact.get("date"))[:16] if fact.get("date") else None
            summary = str(fact.get("summary", ""))[:500] or "要約なし"
            try:
                confidence = max(0.0, min(1.0, float(fact.get("confidence", 0.5))))
            except (TypeError, ValueError):
                confidence = 0.5
            entity_id = None
            if asset_name:
                entity_type = "asset" if category == "finance" else "subject"
                connection.execute(
                    "INSERT OR IGNORE INTO entities(entity_type,canonical_name,created_at,updated_at) VALUES(?,?,?,?)",
                    (entity_type, asset_name, now(), now()),
                )
                entity_id = connection.execute(
                    "SELECT id FROM entities WHERE entity_type=? AND canonical_name=?", (entity_type, asset_name)
                ).fetchone()["id"]
            details = dict(fact.get("details", {})) if isinstance(fact.get("details", {}), dict) else {}
            for metadata_key in ("entity_type", "subject_scope", "personal_relevance", "evidence_strength"):
                if fact.get(metadata_key) is not None:
                    details.setdefault(metadata_key, fact.get(metadata_key))
            personal_relevance = str(fact.get("personal_relevance") or details.get("personal_relevance") or "unknown")
            value = {"asset": asset_name, "amount": amount, "currency": currency, "details": details}
            fact_key = canonical_fact_key(category, fact_type, asset_name, details, summary)
            encoded_value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            duplicate = connection.execute(
                """SELECT f.id FROM facts f LEFT JOIN fact_reviews r ON r.fact_id=f.id
                   WHERE f.fact_key=? AND f.value_json=? AND f.status='current'
                     AND COALESCE(r.state,'pending') != 'rejected' LIMIT 1""",
                (fact_key, encoded_value),
            ).fetchone()
            if duplicate:
                if chunk and source_chunk_id:
                    old_source = connection.execute(
                        """SELECT f.source_chunk_id,c.is_active
                           FROM facts f LEFT JOIN chunks c ON c.id=f.source_chunk_id
                           WHERE f.id=?""", (duplicate["id"],)
                    ).fetchone()
                    # Keep an exact active primary source stable. Re-anchor only
                    # legacy/coarse provenance; every other occurrence becomes
                    # an additional Evidence row.
                    if (
                        old_source
                        and old_source["source_chunk_id"] != chunk["id"]
                        and (old_source["source_chunk_id"] is None or old_source["is_active"] != 1)
                    ):
                        connection.execute(
                            "UPDATE facts SET chunk_id=?,source_chunk_id=?,validation_status='pending',validation_reason=?,retrieval_eligibility='pending' WHERE id=?",
                            (chunk["id"], chunk["id"], "根拠チャンクを発言単位に訂正", duplicate["id"]),
                        )
                        _record_memory_correction(
                            connection, fact_id=duplicate["id"], entity_id=None,
                            correction_type="source_reanchor",
                            before={"source_chunk_id": old_source["source_chunk_id"]},
                            after={"source_chunk_id": chunk["id"]},
                            reason="発言単位の再解析で根拠を訂正", source="reanalysis",
                        )
                    record_fact_evidence(
                        connection, duplicate["id"], source_chunk_id=chunk["id"],
                        source_attachment_id=source_attachment_id,
                        quote=str(fact.get("evidence_quote") or summary),
                        evidence_kind="image" if source_attachment_id else "conversation",
                        source_group=f"{extractor}:{model}:{prompt_version}", reliability=confidence,
                    )
                    apply_memory_quality_to_fact(connection, duplicate["id"], source="reanalysis")
                saved.append({"id": duplicate["id"], "category": category, "type": fact_type, "asset": asset_name, "amount": amount,
                              "date": occurred_on, "summary": summary, "confidence": confidence, "duplicate": True})
                continue
            cursor = connection.execute(
                """INSERT INTO facts(document_id,chunk_id,source_chunk_id,source_attachment_id,subject_entity_id,category,fact_type,occurred_on,effective_at,observed_at,temporal_source,fact_key,value_json,summary,confidence,extractor,extractor_model,prompt_version,extracted_at,created_at,personal_relevance)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (document_id, chunk["id"] if chunk else None, chunk["id"] if chunk else None, source_attachment_id, entity_id, category, fact_type,
                 occurred_on, occurred_on, source_created_at, "explicit_date" if occurred_on else "source_timestamp",
                 fact_key, encoded_value, summary, confidence, extractor, model, prompt_version, now(), now(), personal_relevance),
            )
            fact_id = cursor.lastrowid
            source_quote = str(details.get("evidence_quote") or summary)
            evidence_kind = "image" if source_attachment_id else "conversation"
            record_fact_evidence(
                connection,
                fact_id,
                source_chunk_id=chunk["id"] if chunk else None,
                source_attachment_id=source_attachment_id,
                quote=source_quote,
                evidence_kind=evidence_kind,
                source_group=f"{extractor}:{model}:{prompt_version}",
                reliability=confidence,
            )
            apply_fact_timeline(connection, fact_id)
            transaction_validation = None
            if category == "finance" and fact_type == "transaction":
                transaction_validation = sync_finance_transaction(connection, fact_id, confirmed=user_confirmed)
            if confidence < 0.8:
                reason = "AIの確信度が低いため確認が必要です。"
            elif fact_type == "transaction" and (amount is None or not asset_name):
                reason = "取引として抽出されましたが、金額または対象が不足しています。"
            else:
                reason = "AI抽出した事実です。原文と照合してください。"
            review_fact = {"category": category, "type": fact_type, "summary": summary,
                           "asset": asset_name, "amount": amount, "details": details,
                           "evidence_quote": fact.get("evidence_quote", ""),
                           "evidence_strength": fact.get("evidence_strength", ""),
                           "entity_type": fact.get("entity_type", details.get("entity_type", ""))}
            review_state, review_reason = fact_review_decision(
                review_fact, confidence, user_confirmed, evidence_text=chunk["text"] if chunk else "",
                transaction_validation=transaction_validation,
            )
            if category == "finance" and fact_type == "transaction" and review_state == "confirmed" and not user_confirmed:
                transaction_validation = sync_finance_transaction(connection, fact_id, confirmed=True)
            if fact_type == "transaction" and (amount is None or not asset_name) and not user_confirmed:
                review_state = "pending"
                review_reason = "取引候補ですが金額または対象が不足しているため確認が必要です"
            connection.execute(
                """INSERT INTO fact_reviews(fact_id,state,reason,review_note,reviewed_at,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (fact_id, review_state, review_reason,
                 "自動確定（Evidence）" if review_state == "confirmed" and not user_confirmed else "",
                now() if review_state == "confirmed" else None, now()),
            )
            quality = apply_memory_quality_to_fact(connection, fact_id, source="ingestion")
            saved.append({"id": fact_id, "category": quality.get("category", category), "type": fact_type,
                          "asset": asset_name, "amount": amount, "date": occurred_on, "summary": summary,
                          "confidence": confidence, "entity_type": quality.get("entity_type"),
                          "retrieval_eligibility": quality.get("retrieval_eligibility")})
    return saved


def analysis_content_hash(entry: sqlite3.Row) -> str:
    return hashlib.sha256(f"{entry['title']}\n{entry['body']}".encode("utf-8")).hexdigest()


def _queue_analysis_jobs_for_provider(provider: str) -> int:
    """Queue one idempotent job per active conversation turn/chunk."""
    global ANALYSIS_PREFILTER_SCOPE
    if ANALYSIS_PREFILTER_SCOPE is None and ANALYSIS_PREFILTER_SCOPES:
        ANALYSIS_PREFILTER_SCOPES.clear()
    provider = str(provider).lower()
    if provider == "none":
        return 0
    model = provider_model(provider, "extract")
    prefilter_scope = (provider, model, PROMPT_VERSION)
    queued = 0
    with db() as connection:
        stale = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=15)).isoformat(timespec="seconds")
        connection.execute("UPDATE analysis_jobs SET status='pending', error='stale running job', updated_at=? WHERE status='running' AND started_at < ?", (now(), stale))
        # A document-level job is no longer safe for imported conversations;
        # it can blend unrelated topics.  Keep its row as audit history and
        # supersede it with deterministic chunk jobs below.
        connection.execute(
            """UPDATE analysis_jobs SET status='completed',error='superseded by turn-level analysis',finished_at=?,updated_at=?
               WHERE job_kind='document' AND status IN ('pending','failed')
                 AND document_id IN (SELECT d.id FROM documents d JOIN entries e ON e.id=d.legacy_entry_id WHERE e.source='chatgpt-export')""",
            (now(), now()),
        )
        rows = connection.execute(
            """SELECT e.id AS entry_id,e.title,e.body,e.source,d.id AS document_id,
                      c.id AS chunk_id,c.text AS chunk_text,
                      a.id AS attachment_id,a.content_hash AS attachment_hash
               FROM entries e JOIN documents d ON d.legacy_entry_id=e.id
               JOIN chunks c ON c.document_id=d.id AND c.is_active=1
               LEFT JOIN attachments a ON a.entry_id=e.id
               WHERE e.source IN ('chatgpt-export','ai-ingest')
                 AND NOT EXISTS (
                   SELECT 1 FROM analysis_jobs current_job
                   WHERE current_job.job_kind='chunk'
                     AND current_job.source_chunk_id=c.id
                     AND current_job.provider=? AND current_job.model=?
                     AND current_job.prompt_version=?
                 )
               ORDER BY d.id,c.ordinal""",
            prefilter_scope,
        ).fetchall()
        # Existing queued chunks are also subjected to the same deterministic
        # local gate.  Non-personal reference turns remain in raw/chunk
        # storage but never consume an LLM call or become a memory candidate.
        if prefilter_scope not in ANALYSIS_PREFILTER_SCOPES:
            with ANALYSIS_PREFILTER_LOCK:
                if prefilter_scope not in ANALYSIS_PREFILTER_SCOPES:
                    existing_jobs = connection.execute(
                        """SELECT j.id,c.text,e.source
                           FROM analysis_jobs j JOIN chunks c ON c.id=j.source_chunk_id
                           JOIN documents d ON d.id=c.document_id JOIN entries e ON e.id=d.legacy_entry_id
                           WHERE j.job_kind='chunk' AND j.status IN ('pending','failed')
                             AND j.provider=? AND j.model=? AND j.prompt_version=?""",
                        prefilter_scope,
                    ).fetchall()
                    for job in existing_jobs:
                        if job["source"] == "chatgpt-export" and not chunk_may_contain_personal_memory(job["text"]):
                            connection.execute(
                                "UPDATE analysis_jobs SET status='completed',error='excluded by personal relevance prefilter',finished_at=?,updated_at=? WHERE id=?",
                                (now(), now(), job["id"]),
                            )
                    ANALYSIS_PREFILTER_SCOPES.add(prefilter_scope)
                    ANALYSIS_PREFILTER_SCOPE = prefilter_scope
        for row in rows:
            job_kind = "chunk"
            # Include the stable chunk id so identical repeated turns in one
            # conversation still receive independent, attributable Jobs.
            content_hash = hashlib.sha256(
                f"{row['chunk_id']}\n{row['chunk_text'] or ''}".encode("utf-8")
            ).hexdigest()
            if row["source"] == "chatgpt-export" and not chunk_may_contain_personal_memory(row["chunk_text"]):
                connection.execute(
                    """INSERT OR IGNORE INTO analysis_jobs(
                         document_id,provider,model,prompt_version,content_hash,status,error,
                         finished_at,job_kind,source_attachment_id,source_chunk_id,created_at,updated_at
                       ) VALUES(?,?,?,?,?,'completed','excluded by personal relevance prefilter',
                                ?,'chunk',NULL,?,?,?)""",
                    (row["document_id"], provider, model, PROMPT_VERSION, content_hash,
                     now(), row["chunk_id"], now(), now()),
                )
                continue
            cursor = connection.execute(
                """INSERT OR IGNORE INTO analysis_jobs(document_id,provider,model,prompt_version,content_hash,status,job_kind,source_attachment_id,source_chunk_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,'pending',?,?,?,?,?)""",
                (row["document_id"], provider, model, PROMPT_VERSION, content_hash, job_kind, None, row["chunk_id"], now(), now()),
            )
            queued += cursor.rowcount
    return queued


def queue_analysis_jobs() -> int:
    """Queue one job per enabled extraction provider and chunk."""
    return sum(_queue_analysis_jobs_for_provider(provider) for provider in extraction_providers())


def current_analysis_job_scope(alias: str = "j") -> tuple[str, tuple[object, ...]]:
    """SQL scope for the currently runnable extraction configuration.

    Historical model/prompt jobs, inactive chunks and orphaned attachments are
    audit history. They must not inflate the user's current backlog.
    """
    providers = extraction_providers()
    local_model = provider_model("local", "verification")
    chunk_parts: list[str] = []
    params: list[str] = []
    for provider in providers:
        chunk_parts.append(
            f"({alias}.job_kind='chunk' AND {alias}.provider=? AND {alias}.model=? AND {alias}.prompt_version=? "
            f"AND EXISTS (SELECT 1 FROM chunks scope_chunk WHERE scope_chunk.id={alias}.source_chunk_id AND scope_chunk.is_active=1))"
        )
        params.extend((provider, provider_model(provider, "extract"), PROMPT_VERSION))
    chunk_sql = " OR ".join(chunk_parts) or "0=1"
    where = f"(({chunk_sql}) OR ({alias}.job_kind='attachment' AND {alias}.provider='local' AND {alias}.model=? AND EXISTS (SELECT 1 FROM attachments scope_attachment WHERE scope_attachment.id={alias}.source_attachment_id)))"
    params.append(local_model)
    return where, tuple(params)


def pending_import_count() -> int:
    where, params = current_analysis_job_scope()
    with db() as connection:
        return connection.execute(
            f"SELECT COUNT(*) FROM analysis_jobs j WHERE {where} AND j.status IN ('pending','running')",
            params,
        ).fetchone()[0]


def runnable_analysis_count() -> int:
    """Pending jobs plus failed jobs that are still within the retry budget."""
    where, params = current_analysis_job_scope()
    with db() as connection:
        return connection.execute(
            f"""SELECT COUNT(*) FROM analysis_jobs j
                WHERE {where}
                  AND (j.status IN ('pending','running') OR (j.status='failed' AND j.attempts < 3))""",
            params,
        ).fetchone()[0]


def analysis_job_summary() -> dict[str, object]:
    where, params = current_analysis_job_scope()
    providers = extraction_providers()
    provider = providers[0] if providers else selected_provider("extract")
    model = provider_model(provider, "extract")
    stale_cutoff = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=15)).isoformat(timespec="seconds")
    with db() as connection:
        rows = connection.execute(
            f"""SELECT
                  CASE
                    WHEN j.status='completed' AND COALESCE(j.error,'')!='' THEN 'skipped'
                    ELSE j.status
                  END AS display_status,
                  COUNT(*) AS count
                FROM analysis_jobs j
                WHERE {where}
                GROUP BY display_status""",
            params,
        ).fetchall()
        current_job_units = connection.execute(
            f"""SELECT
                  COUNT(DISTINCT CASE WHEN j.job_kind='chunk' THEN j.source_chunk_id END) AS text_units,
                  COUNT(DISTINCT CASE WHEN j.job_kind='attachment' THEN j.source_attachment_id END) AS attachment_units,
                  SUM(CASE WHEN j.status='pending' AND j.priority<100 THEN 1 ELSE 0 END) AS prioritized_pending,
                  MAX(j.updated_at) AS last_activity_at
                FROM analysis_jobs j WHERE {where}""",
            params,
        ).fetchone()
        active_text_units = connection.execute(
            """SELECT COUNT(*)
               FROM chunks c JOIN documents d ON d.id=c.document_id
               JOIN entries e ON e.id=d.legacy_entry_id
               WHERE c.is_active=1 AND e.source IN ('chatgpt-export','ai-ingest')"""
        ).fetchone()[0]
        raw_attachments = connection.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
        historical_jobs = connection.execute(
            f"SELECT COUNT(*) FROM analysis_jobs j WHERE NOT ({where})",
            params,
        ).fetchone()[0]
        imported_entries = connection.execute(
            "SELECT COUNT(*) FROM entries WHERE source='chatgpt-export'"
        ).fetchone()[0]
        lock_active = bool(connection.execute(
            """SELECT 1 FROM analysis_locks
               WHERE lock_name=? AND acquired_at>=?""",
            ("gemini-import-analysis", stale_cutoff),
        ).fetchone())
    counts = {row["display_status"]: row["count"] for row in rows}
    pending = counts.get("pending", 0)
    running = counts.get("running", 0)
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    skipped = counts.get("skipped", 0)
    target_total = pending + running + completed + failed
    current_units = int(current_job_units["text_units"] or 0) + int(current_job_units["attachment_units"] or 0)
    raw_units = int(active_text_units) + int(raw_attachments)
    excluded_before_llm = max(0, raw_units - current_units) + skipped
    paused = analysis_paused()
    configured = bool(providers)
    is_running = running > 0 or lock_active
    if paused:
        status_label = "一時停止中"
    elif is_running:
        status_label = "解析中"
    elif failed:
        status_label = "一部失敗"
    elif pending:
        status_label = "解析待ち"
    elif not configured:
        status_label = "LLM未設定"
    else:
        status_label = "解析済み"
    return {
        "pending": pending,
        "running": running,
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "total": target_total,
        "progress": round((completed / target_total * 100), 1) if target_total else 100.0,
        "paused": paused,
        "configured": configured,
        "is_running": is_running,
        "status_label": status_label,
        "provider": provider,
        "model": model,
        "providers": providers,
        "models": {name: provider_model(name, "extract") for name in providers},
        "prompt_version": PROMPT_VERSION,
        "batch_limit": analysis_batch_size(),
        "prioritized_pending": int(current_job_units["prioritized_pending"] or 0),
        "scope": {
            "imported_entries": imported_entries,
            "active_text_units": int(active_text_units),
            "attachments": int(raw_attachments),
            "raw_units": raw_units,
            "analysis_targets": target_total,
            "excluded_before_llm": excluded_before_llm,
            "historical_jobs": int(historical_jobs),
            "last_activity_at": current_job_units["last_activity_at"],
            "basis": "現在の抽出Provider・モデル・Prompt versionに一致する、有効チャンクと添付画像のみ",
        },
    }


def operational_health() -> dict[str, object]:
    """Return local operational signals without contacting an LLM or network."""
    started = time.perf_counter()
    with db() as connection:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        job_latency = connection.execute(
            """SELECT COUNT(*) AS samples,
                      AVG((julianday(finished_at)-julianday(started_at))*86400000.0) AS average_ms,
                      MAX((julianday(finished_at)-julianday(started_at))*86400000.0) AS maximum_ms
               FROM analysis_jobs
               WHERE status='completed' AND started_at IS NOT NULL AND finished_at IS NOT NULL"""
        ).fetchone()
        stale_running = connection.execute(
            """SELECT COUNT(*) FROM analysis_jobs
               WHERE status='running' AND updated_at<?""",
            ((datetime.now(timezone.utc).astimezone() - timedelta(minutes=15)).isoformat(timespec="seconds"),),
        ).fetchone()[0]
    queue = analysis_job_summary()
    backup = backup_status()
    return {
        "ok": integrity == "ok" and int(stale_running) == 0,
        "environment": APP_ENV,
        "database": DB_PATH.name,
        "integrity": integrity,
        "response_ms": round((time.perf_counter() - started) * 1000, 3),
        "analysis": {
            "status": queue["status_label"],
            "pending": queue["pending"],
            "running": queue["running"],
            "failed": queue["failed"],
            "stale_running": int(stale_running),
            "completed_duration_samples": int(job_latency["samples"] or 0),
            "average_completed_job_ms": round(float(job_latency["average_ms"] or 0.0), 3),
            "maximum_completed_job_ms": round(float(job_latency["maximum_ms"] or 0.0), 3),
        },
        "backup": {
            "count": backup.get("backup_count", 0),
            "latest": backup.get("last_backup_path"),
            "latest_at": backup.get("last_backup_at"),
        },
        "network_boundary": {
            "default_bind": os.environ.get("PERSONAL_OS_HOST", "0.0.0.0"),
            "lan_auth_required": True,
            "access_password_configured": bool(configured_access_password()),
            "session": "HttpOnly SameSite=Lax expiring session",
            "csrf": "X-CSRF-Token required for LAN state changes",
            "allowed_origins": [
                item.strip() for item in setting("allowed_origins", "").split(",") if item.strip()
            ],
            "wildcard_cors": False,
        },
    }


def requeue_analysis_jobs(mode: str = "failed") -> int:
    """Make failed or version-stale jobs eligible again without duplicating facts."""
    if mode not in {"failed", "current-version"}:
        raise ValueError("Invalid requeue mode")
    with db() as connection:
        if mode == "failed":
            scope_sql, scope_params = current_analysis_job_scope("j")
            cursor = connection.execute(
                f"""UPDATE analysis_jobs
                    SET status='pending', error='', started_at=NULL, finished_at=NULL, updated_at=?
                    WHERE id IN (
                        SELECT j.id FROM analysis_jobs j
                        WHERE j.status='failed' AND {scope_sql}
                    )""",
                [now(), *scope_params],
            )
        else:
            provider = selected_provider("extract")
            model = provider_model(provider, "extract")
            stale_documents = [row["document_id"] for row in connection.execute(
                """SELECT DISTINCT document_id FROM analysis_jobs
                   WHERE status IN ('failed','completed')
                     AND (provider != ? OR model != ? OR prompt_version != ?)""",
                (provider, model, PROMPT_VERSION),
            )]
            if not stale_documents:
                return 0
            # queue_analysis_jobs creates the current-version job without
            # duplicating a document. The worker only claims current settings.
            connection.commit()
            queue_analysis_jobs()
            placeholders = ",".join("?" for _ in stale_documents)
            cursor = connection.execute(
                f"""UPDATE analysis_jobs SET status='pending', error='', started_at=NULL, finished_at=NULL, updated_at=?
                    WHERE document_id IN ({placeholders}) AND provider=? AND model=? AND prompt_version=?
                      AND status IN ('failed','completed')""",
                [now(), *stale_documents, provider, model, PROMPT_VERSION],
            )
    return cursor.rowcount


def prioritize_analysis_for_query(message: str) -> int:
    """Move query-relevant, still-unanalysed chunks ahead of bulk backfill."""
    terms = [term for term in query_terms(message) if len(term) >= 2][:12]
    if not terms:
        return 0
    clauses = " OR ".join("c.text LIKE ?" for _ in terms)
    timestamp = now()
    with db() as connection:
        chunk_ids = [
            int(row["id"]) for row in connection.execute(
                f"""SELECT c.id FROM chunks c
                    WHERE c.is_active=1 AND ({clauses})
                    ORDER BY c.created_at DESC LIMIT 100""",
                [f"%{term}%" for term in terms],
            )
        ]
        if not chunk_ids:
            return 0
        marks = ",".join("?" for _ in chunk_ids)
        cursor = connection.execute(
            f"""UPDATE analysis_jobs
                SET priority=MIN(priority,10),priority_reason='相談に関連',
                    requested_at=?,usage_count=usage_count+1,updated_at=?
                WHERE source_chunk_id IN ({marks})
                  AND (status='pending' OR (status='failed' AND attempts<3))""",
            [timestamp, timestamp, *chunk_ids],
        )
    return cursor.rowcount


def acquire_analysis_lock() -> bool:
    """Use a SQLite lock so two app processes cannot analyze the same entry."""
    cutoff = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=15)).isoformat(timespec="seconds")
    with db() as connection:
        connection.execute("DELETE FROM analysis_locks WHERE lock_name=? AND acquired_at < ?", ("gemini-import-analysis", cutoff))
        cursor = connection.execute(
            "INSERT OR IGNORE INTO analysis_locks(lock_name,acquired_at) VALUES(?,?)",
            ("gemini-import-analysis", now()),
        )
    return bool(cursor.rowcount)


def release_analysis_lock() -> None:
    with db() as connection:
        connection.execute("DELETE FROM analysis_locks WHERE lock_name=?", ("gemini-import-analysis",))


def _analyze_imported_conversations(limit: int, provider_name: str | None = None) -> dict:
    """Claim and execute bounded analysis jobs without reprocessing completed versions."""
    # A larger invocation batch reduces manual clicks after strict turn-level
    # chunking. Jobs still execute sequentially, so local VRAM is not multiplied.
    limit = max(1, min(limit, 200))
    analyzed = facts_saved = 0
    errors: list[str] = []
    provider = provider_name or selected_provider("extract")
    model = provider_model(provider, "extract")
    for _ in range(limit):
        with db() as connection:
            job = connection.execute(
                """SELECT j.*,e.id AS entry_id,e.title,e.body,c.text AS chunk_text,c.is_active AS chunk_active,
                          a.storage_path,a.id AS attachment_id
                   FROM analysis_jobs j
                   JOIN documents d ON d.id=j.document_id JOIN entries e ON e.id=d.legacy_entry_id
                   LEFT JOIN chunks c ON c.id=j.source_chunk_id
                   LEFT JOIN attachments a ON a.id=j.source_attachment_id
                   WHERE (j.status='pending' OR (j.status='failed' AND j.attempts < 3))
                     AND (j.job_kind='attachment' OR (j.job_kind='chunk' AND c.is_active=1))
                     AND ((j.provider=? AND j.model=? AND j.prompt_version=?) OR (j.job_kind='attachment' AND j.provider='local'))
                   ORDER BY CASE j.status WHEN 'pending' THEN 0 ELSE 1 END,
                            j.priority ASC,COALESCE(j.requested_at,j.created_at),j.created_at,j.id LIMIT 1""",
                (provider, model, PROMPT_VERSION),
            ).fetchone()
            if not job:
                break
            claimed = connection.execute(
                """UPDATE analysis_jobs SET status='running',attempts=attempts+1,started_at=?,updated_at=?
                   WHERE id=? AND (status='pending' OR (status='failed' AND attempts < 3))""",
                (now(), now(), job["id"]),
            )
            if not claimed.rowcount:
                continue
        # One conversation can be long. The beginning is sufficient for a first fact pass and bounds API cost.
        source_text = job["chunk_text"] or job["body"][:8000]
        input_text = f"会話タイトル: {job['title']}\n\n会話本文（このチャンクのみ）:\n{source_text}"
        try:
            if job["job_kind"] == "attachment" and job["storage_path"]:
                image_bytes = attachment_file_bytes(job["storage_path"])
                ocr = local_ocr_derivative(job["attachment_id"], image_bytes)
                if ocr_is_sufficient(ocr):
                    facts = extract_with_local(
                        "画像の補足情報:\n" + job["body"][:1000]
                        + "\n\nローカルOCRで読み取った本文:\n" + str(ocr.get("text") or "")[:12_000]
                    ) or []
                else:
                    facts = extract_image_facts(image_bytes, job["body"][:1000]) or []
            else:
                facts = None
            if facts is None:
                facts = extract_with_model(input_text, purpose="import", provider_override=str(job["provider"])) or []
            personal_candidates = [fact for fact in facts if fact_policy(fact) != "exclude"]
            facts_saved += len(save_structured_facts(
                job["entry_id"],
                personal_candidates,
                job["provider"], job["model"], job["prompt_version"],
                source_attachment_id=job["attachment_id"] if job["job_kind"] == "attachment" else None,
                source_chunk_id=job["source_chunk_id"] if job["job_kind"] == "chunk" else None,
            ))
            create_memory_proposal(job["entry_id"], personal_candidates)
            with db() as connection:
                connection.execute("UPDATE analysis_jobs SET status='completed',finished_at=?,error='',updated_at=? WHERE id=?", (now(), now(), job["id"]))
                connection.execute(
                    "INSERT OR REPLACE INTO analysis_status(entry_id,analyzer,analyzed_at) VALUES(?,?,?)",
                    (job["entry_id"], job["provider"], now()),
                )
            analyzed += 1
        except (ValueError, TimeoutError, OSError, urllib.error.URLError, sqlite3.Error) as error:
            errors.append(str(error))
            with db() as connection:
                connection.execute("UPDATE analysis_jobs SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?", (str(error)[:2000], now(), now(), job["id"]))
            break
    summary = analysis_job_summary()
    return {"analyzed": analyzed, "facts_saved": facts_saved, "remaining": runnable_analysis_count(), "errors": errors, "jobs": summary}


def analyze_imported_conversations(limit: int) -> dict:
    if analysis_paused():
        return {"skipped": True, "reason": "Analysis is paused", "remaining": pending_import_count(), "jobs": analysis_job_summary()}
    queue_analysis_jobs()
    providers = extraction_providers()
    if not providers:
        raise ValueError("抽出用LLMが未設定です。設定画面でプロバイダを選び、必要なAPIキーまたはローカルURLを設定してください。")
    if not ANALYSIS_THREAD_LOCK.acquire(blocking=False):
        return {"skipped": True, "reason": "このアプリ内で分析実行中です。", "remaining": pending_import_count()}
    try:
        try:
            acquired = acquire_analysis_lock()
        except sqlite3.OperationalError:
            return {
                "skipped": True,
                "reason": "DBが別処理を完了するのを待っています。少し後に自動再試行します。",
                "remaining": pending_import_count(),
                "jobs": analysis_job_summary(),
            }
        if not acquired:
            return {"skipped": True, "reason": "別のアプリプロセスが分析実行中です。", "remaining": pending_import_count()}
        try:
            if len(providers) == 1:
                return _analyze_imported_conversations(limit, providers[0])
            results: dict[str, dict] = {}
            workers = []
            def run_provider(provider: str) -> None:
                try:
                    results[provider] = _analyze_imported_conversations(limit, provider)
                except Exception as error:  # keep other provider progress
                    results[provider] = {"analyzed": 0, "facts_saved": 0, "errors": [str(error)]}
            for provider in providers:
                worker = threading.Thread(target=run_provider, args=(provider,), name=f"analysis-{provider}")
                worker.start()
                workers.append(worker)
            for worker in workers:
                worker.join()
            return {
                "analyzed": sum(int(result.get("analyzed", 0)) for result in results.values()),
                "facts_saved": sum(int(result.get("facts_saved", 0)) for result in results.values()),
                "remaining": runnable_analysis_count(),
                "errors": [error for result in results.values() for error in result.get("errors", [])],
                "providers": results,
                "jobs": analysis_job_summary(),
            }
        finally:
            try:
                release_analysis_lock()
            except sqlite3.OperationalError:
                pass
    finally:
        ANALYSIS_THREAD_LOCK.release()


def analysis_loop() -> None:
    """Process the import backlog continuously, unless the user pauses LLM work."""
    while True:
        if analysis_paused():
            time.sleep(5)
            continue
        runnable = runnable_analysis_count()
        if runnable and extraction_providers():
            try:
                result = analyze_imported_conversations(analysis_batch_size())
                if result.get("errors"):
                    time.sleep(20)
                elif result.get("skipped"):
                    time.sleep(5)
            except (ValueError, TimeoutError, OSError, urllib.error.URLError, sqlite3.Error) as error:
                print(f"[analysis] {error}")
                time.sleep(20)
        elif runnable:
            # A missing key cannot make progress; avoid a noisy retry loop.
            time.sleep(600)
        else:
            time.sleep(600)


def backup_loop() -> None:
    """Create at most one automatic generation per 24 hours.

    The worker waits until the previous generation is due, so launching the
    app does not itself create a backup. Explicit UI, restore, and migration
    backups remain available through their force/safety paths.
    """
    while True:
        try:
            delay = backup_wait_seconds()
        except sqlite3.Error as error:
            print(f"[backup] unable to read schedule: {error}")
            delay = BACKUP_INTERVAL_SECONDS
        time.sleep(delay)
        try:
            backup_database()
        except sqlite3.Error as error:
            print(f"[backup] {error}")


def task_plan(entry: sqlite3.Row | dict[str, str]) -> tuple[str, str, str]:
    """A conservative local task classifier; it never changes completion state."""
    text = f"{entry['title']} {entry['body']} {entry['tags']}".lower()
    area = next((label for words, label in [
        (("travel", "trip", "旅行", "ホテル", "航空", "flight"), "旅行"),
        (("恋愛", "彼", "彼女", "デート", "relationship"), "恋愛・人間関係"),
        (("資産", "投資", "家計", "保険", "銀行", "money"), "資産・家計"),
        (("仕事", "会議", "資料", "study", "学習"), "仕事・学習"),
    ] if any(word in text for word in words)), "生活・その他")
    urgency = "高" if any(word in text for word in ("今日", "至急", "締切", "urgent")) else "中" if any(word in text for word in ("今週", "soon")) else "低"
    first_line = next((line.strip(" -・[]") for line in entry["body"].splitlines() if line.strip()), entry["title"])
    return area, urgency, first_line[:160]


def organize_tasks() -> int:
    with db() as connection:
        tasks = connection.execute("SELECT * FROM entries WHERE kind='task' AND status != 'done'").fetchall()
        for entry in tasks:
            area, urgency, next_action = task_plan(entry)
            connection.execute(
                """INSERT INTO task_plans(entry_id,area,urgency,next_action,updated_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(entry_id) DO UPDATE SET area=excluded.area, urgency=excluded.urgency,
                   next_action=excluded.next_action, updated_at=excluded.updated_at""",
                (entry["id"], area, urgency, next_action, now()),
            )
    return len(tasks)


def _chatgpt_archive_names(archive: zipfile.ZipFile) -> list[str]:
    names = archive.namelist()
    single_file = next((name for name in names if Path(name).name.lower() == "conversations.json"), None)
    shard_files = sorted(
        name for name in names
        if re.fullmatch(r"conversations-\d+\.json", Path(name).name.lower())
    )
    filenames = [single_file] if single_file else shard_files
    if not filenames:
        raise ValueError("conversations.json または conversations-000.json がZIP内に見つかりません")
    return [str(name) for name in filenames if name]


def iter_json_array(stream, chunk_size: int = 1024 * 1024):
    """Yield a top-level JSON array without loading the whole member in memory."""
    utf8_decoder = codecs.getincrementaldecoder("utf-8-sig")()
    value_decoder = json.JSONDecoder()
    buffer = ""
    position = 0
    started = False
    expect_value = False
    finished = False
    eof = False

    while True:
        while True:
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if not started:
                if position >= len(buffer):
                    break
                if buffer[position] != "[":
                    raise ValueError("会話JSONの最上位は配列である必要があります")
                position += 1
                started = True
                expect_value = True
                continue
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if expect_value:
                if position >= len(buffer):
                    break
                if buffer[position] == "]":
                    position += 1
                    finished = True
                    break
                try:
                    value, end = value_decoder.raw_decode(buffer, position)
                except json.JSONDecodeError as error:
                    if eof:
                        raise ValueError(f"会話JSONを解析できません: {error.msg}") from error
                    break
                yield value
                position = end
                expect_value = False
                continue
            if position >= len(buffer):
                break
            if buffer[position] == ",":
                position += 1
                expect_value = True
                continue
            if buffer[position] == "]":
                position += 1
                finished = True
                break
            raise ValueError("会話JSONの要素区切りを認識できません")

        if finished:
            trailing = buffer[position:]
            while not eof:
                block = stream.read(chunk_size)
                if block:
                    trailing += utf8_decoder.decode(block)
                else:
                    trailing += utf8_decoder.decode(b"", final=True)
                    eof = True
            if trailing.strip():
                raise ValueError("会話JSONの配列末尾に不正なデータがあります")
            return
        if eof:
            raise ValueError("会話JSONが配列の途中で終了しました")
        if position:
            buffer = buffer[position:]
            position = 0
        block = stream.read(chunk_size)
        if block:
            buffer += utf8_decoder.decode(block)
        else:
            buffer += utf8_decoder.decode(b"", final=True)
            eof = True


def _import_chatgpt_batch(conversations: list[object]) -> tuple[int, int, list[int]]:
    """Commit a bounded conversation batch and return all rows needing documents."""
    created = skipped = 0
    entry_ids: list[int] = []
    with db() as connection:
        for conversation_value in conversations:
            if not isinstance(conversation_value, dict):
                skipped += 1
                continue
            conversation = conversation_value
            conversation_id = str(conversation.get("id") or conversation.get("conversation_id") or "")
            if not conversation_id:
                skipped += 1
                continue
            external_id = f"chatgpt:{conversation_id}"
            existing = connection.execute(
                "SELECT id FROM entries WHERE external_id=?", (external_id,)
            ).fetchone()
            if existing:
                skipped += 1
                entry_ids.append(int(existing["id"]))
                continue
            messages = []
            for node in (conversation.get("mapping") or {}).values():
                message = node.get("message") or {}
                author = (message.get("author") or {}).get("role")
                content_value = message.get("content") or {}
                parts = content_value.get("parts") or []
                text = "\n".join(str(part) for part in parts if isinstance(part, str)).strip()
                message_timestamp = message.get("create_time") or 0
                if author in {"user", "assistant"} and text:
                    messages.append((message_timestamp, author, text))
            messages.sort(key=lambda item: item[0] or 0)
            body = "\n\n".join(f"{role}: {text}" for _, role, text in messages)
            if not body:
                skipped += 1
                continue
            title = str(conversation.get("title") or "ChatGPTとの会話")
            source_timestamp = export_timestamp(conversation.get("update_time") or conversation.get("create_time"))
            cursor = connection.execute(
                """INSERT INTO entries(kind,title,body,source,tags,status,created_at,updated_at,external_id)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("conversation", title, body, "chatgpt-export", "chatgpt, imported", "note",
                 source_timestamp, source_timestamp, external_id),
            )
            entry_ids.append(int(cursor.lastrowid))
            created += 1
    return created, skipped, entry_ids


def _import_chatgpt_archive(archive: zipfile.ZipFile, *, file_hash: str,
                            file_name: str = "chatgpt-export.zip") -> tuple[int, int]:
    """Import shard-by-shard and checkpoint progress without external AI calls."""
    filenames = _chatgpt_archive_names(archive)
    created = skipped = 0
    resume_after: str | None = None
    timestamp = now()
    with db() as connection:
        existing = connection.execute(
            """SELECT status,last_shard,created_count,skipped_count FROM import_jobs
               WHERE source_kind='chatgpt' AND file_hash=?""",
            (file_hash,),
        ).fetchone()
        if existing and existing["status"] in {"running", "failed"} and existing["last_shard"] in filenames:
            resume_after = str(existing["last_shard"])
            created = int(existing["created_count"] or 0)
            skipped = int(existing["skipped_count"] or 0)
        connection.execute(
            """INSERT INTO import_jobs(source_kind,file_name,file_hash,status,started_at,updated_at)
               VALUES('chatgpt',?,?, 'running',?,?)
               ON CONFLICT(source_kind,file_hash) DO UPDATE SET
                 file_name=excluded.file_name,status='running',error='',updated_at=excluded.updated_at""",
            (file_name[:255], file_hash, timestamp, timestamp),
        )
    try:
        start_index = filenames.index(resume_after) + 1 if resume_after else 0
        for filename in filenames[start_index:]:
            batch: list[object] = []
            with archive.open(filename) as stream:
                for conversation in iter_json_array(stream):
                    batch.append(conversation)
                    if len(batch) < 250:
                        continue
                    batch_created, batch_skipped, entry_ids = _import_chatgpt_batch(batch)
                    created += batch_created
                    skipped += batch_skipped
                    for entry_id in entry_ids:
                        ensure_document_for_entry(entry_id)
                    with db() as connection:
                        connection.execute(
                            """UPDATE import_jobs SET created_count=?,skipped_count=?,updated_at=?
                               WHERE source_kind='chatgpt' AND file_hash=?""",
                            (created, skipped, now(), file_hash),
                        )
                    batch.clear()
            if batch:
                batch_created, batch_skipped, entry_ids = _import_chatgpt_batch(batch)
                created += batch_created
                skipped += batch_skipped
                for entry_id in entry_ids:
                    ensure_document_for_entry(entry_id)
            with db() as connection:
                connection.execute(
                    """UPDATE import_jobs SET last_shard=?,created_count=?,skipped_count=?,updated_at=?
                       WHERE source_kind='chatgpt' AND file_hash=?""",
                    (filename, created, skipped, now(), file_hash),
                )
        queue_analysis_jobs()
        with db() as connection:
            connection.execute(
                """UPDATE import_jobs SET status='completed',created_count=?,skipped_count=?,
                     finished_at=?,updated_at=? WHERE source_kind='chatgpt' AND file_hash=?""",
                (created, skipped, now(), now(), file_hash),
            )
    except Exception as error:
        with db() as connection:
            connection.execute(
                """UPDATE import_jobs SET status='failed',created_count=?,skipped_count=?,error=?,updated_at=?
                   WHERE source_kind='chatgpt' AND file_hash=?""",
                (created, skipped, str(error)[:2000], now(), file_hash),
            )
        raise
    return created, skipped


def import_chatgpt_export(content: bytes) -> tuple[int, int]:
    """Compatibility path for tests and small clients."""
    digest = hashlib.sha256(content).hexdigest()
    with zipfile.ZipFile(BytesIO(content)) as archive:
        return _import_chatgpt_archive(archive, file_hash=digest)


def import_chatgpt_export_path(path: Path, file_hash: str, file_name: str) -> tuple[int, int]:
    with zipfile.ZipFile(path) as archive:
        return _import_chatgpt_archive(archive, file_hash=file_hash, file_name=file_name)


def stream_request_file(stream, length: int) -> tuple[Path, str]:
    """Bound memory usage while receiving a direct application/zip upload."""
    import_dir = DB_PATH.parent / "imports"
    import_dir.mkdir(parents=True, exist_ok=True)
    path = import_dir / f".upload-{uuid.uuid4().hex}.zip"
    digest = hashlib.sha256()
    remaining = length
    try:
        with path.open("wb") as destination:
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("アップロードが途中で終了しました")
                destination.write(block)
                digest.update(block)
                remaining -= len(block)
        return path, digest.hexdigest()
    except Exception:
        path.unlink(missing_ok=True)
        raise


def store_screenshot(content: bytes, original_name: str, declared_mime: str, context: str) -> tuple[int, int]:
    """Persist the original image locally and create a raw entry for its provenance."""
    mime_type = detect_image_mime(content)
    if not mime_type:
        raise ValueError("Only PNG, JPEG, and WebP screenshots are supported")
    if len(content) > 12 * 1024 * 1024:
        raise ValueError("Screenshot must be 12 MB or smaller")
    digest = hashlib.sha256(content).hexdigest()
    extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime_type]
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    target = ATTACHMENT_DIR / f"{digest}{extension}"
    if not target.exists():
        target.write_bytes(content)
    try:
        storage_path = str(target.relative_to(ROOT))
    except ValueError:
        # Verification/test databases may deliberately place their attachment
        # directory outside the application root.
        storage_path = str(target)
    note = context.strip()[:1000]
    title = f"Screenshot: {note[:60] or original_name[:60]}"
    with db() as connection:
        cursor = connection.execute(
            """INSERT INTO entries(kind,title,body,source,tags,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("note", title, note or "Screenshot uploaded for local extraction.", "screenshot", "screenshot,image", "inbox", now(), now()),
        )
        entry_id = cursor.lastrowid
        attachment = connection.execute(
            """INSERT INTO attachments(entry_id,storage_path,original_name,mime_type,byte_size,content_hash,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (entry_id, storage_path, original_name[:255], mime_type, len(content), digest, now()),
        )
    ensure_document_for_entry(entry_id)
    return entry_id, attachment.lastrowid


def attachment_file_bytes(storage_path: str) -> bytes:
    path = (ROOT / storage_path).resolve()
    if not path.exists():
        fallback = (ATTACHMENT_DIR / Path(storage_path).name).resolve()
        path = fallback
    attachment_root = ATTACHMENT_DIR.resolve()
    if attachment_root not in path.parents or not path.is_file():
        raise ValueError("Attachment file is missing or outside local storage")
    return path.read_bytes()


def local_ocr_derivative(attachment_id: int, image_bytes: bytes) -> dict[str, object]:
    with db() as connection:
        existing = connection.execute(
            """SELECT engine,content,confidence,metadata_json FROM attachment_derivatives
               WHERE attachment_id=? AND derivative_kind='ocr' AND version='ocr-v1'
               ORDER BY id DESC LIMIT 1""",
            (attachment_id,),
        ).fetchone()
    if existing:
        try:
            metadata = json.loads(existing["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        return {
            "available": bool(metadata.get("available")),
            "text": existing["content"],
            "confidence": float(existing["confidence"] or 0.0),
            "engine": existing["engine"],
            **({"error": metadata["error"]} if metadata.get("error") else {}),
        }
    result = extract_ocr_text(image_bytes)
    with db() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO attachment_derivatives(
                 attachment_id,derivative_kind,engine,version,content,confidence,metadata_json,created_at
               ) VALUES(?,'ocr',?,'ocr-v1',?,?,?,?)""",
            (attachment_id, str(result.get("engine") or "none"), str(result.get("text") or "")[:100_000],
             float(result.get("confidence") or 0.0),
             json.dumps({"available": bool(result.get("available")), "error": result.get("error", "")}, ensure_ascii=False),
             now()),
        )
    return result


def _fact_payload(fact: dict) -> dict:
    try:
        value = json.loads(fact.get("value_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _amount_from_fact(fact: dict) -> float | None:
    value = _fact_payload(fact)
    raw = value.get("amount")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _money_summary(facts: list[dict], transactions: list[dict]) -> dict[str, object]:
    current = [fact for fact in facts if fact.get("status") == "current" and fact.get("review_state") == "confirmed"]
    breakdown: dict[str, float] = {}
    total: float | None = None
    monthly: float | None = None
    for fact in current:
        amount = _amount_from_fact(fact)
        if amount is None:
            continue
        payload = _fact_payload(fact)
        text = f"{fact.get('summary','')} {payload.get('asset','')} {payload.get('details','')}"
        if fact.get("fact_type") == "asset_balance":
            if any(token in text for token in ("総資産", "total_assets", "全資産")):
                total = amount
            else:
                bucket = "その他資産"
                for tokens, label in (
                    (("現金", "預金", "cash"), "現金"),
                    (("投資信託", "投信", "fund"), "投資信託"),
                    (("日本株", "国内株"), "日本株"),
                    (("海外株", "外国株"), "海外株"),
                    (("持株会", "employee_stock_plan"), "持株会"),
                    (("年金", "確定拠出", "pension"), "年金"),
                ):
                    if any(token.lower() in text.lower() for token in tokens):
                        bucket = label
                        break
                breakdown[bucket] = breakdown.get(bucket, 0.0) + amount
        elif fact.get("fact_type") == "plan" and any(token in text for token in ("積立", "monthly_investment", "毎月")):
            monthly = amount
    if total is None and breakdown:
        total = sum(breakdown.values())
    transaction_total = 0.0
    transaction_breakdown: dict[str, float] = {}
    asset_history: list[dict[str, object]] = []
    allocation_history: list[dict[str, object]] = []
    for item in transactions:
        try:
            amount = float(item.get("normalized_amount") if item.get("normalized_amount") is not None else item.get("amount") or 0)
            transaction_total += amount
            kind = str(item.get("transaction_kind") or "other")
            transaction_breakdown[kind] = transaction_breakdown.get(kind, 0.0) + amount
        except (TypeError, ValueError):
            continue
    for fact in facts:
        amount = _amount_from_fact(fact)
        if amount is None:
            continue
        payload = _fact_payload(fact)
        date = fact.get("valid_from") or fact.get("occurred_on") or fact.get("source_created_at")
        text = f"{fact.get('summary','')} {payload.get('asset','')}"
        if fact.get("fact_type") == "asset_balance":
            point = {"date": date, "amount": amount, "fact_id": fact.get("id"), "status": fact.get("status")}
            if any(token in text for token in ("総資産", "total_assets", "全資産")):
                asset_history.append(point)
            else:
                point["asset"] = payload.get("asset") or fact.get("entity_name") or "その他資産"
                allocation_history.append(point)
    return {
        "total_assets": total,
        "breakdown": breakdown,
        "monthly_investment": monthly,
        "transaction_count": len(transactions),
        "transaction_total": transaction_total,
        "transaction_breakdown": transaction_breakdown,
        "latest_transaction": transactions[0] if transactions else None,
        "asset_history": sorted(asset_history, key=lambda item: str(item.get("date") or "")),
        "allocation_history": sorted(allocation_history, key=lambda item: str(item.get("date") or "")),
    }


def _travel_summary(facts: list[dict]) -> dict[str, object]:
    current = [fact for fact in facts if fact.get("review_state") == "confirmed"]
    visited: list[str] = []
    wanted: list[str] = []
    hotels: list[str] = []
    transport: list[str] = []
    preferences: list[str] = []
    trips: list[dict[str, object]] = []
    hotel_history: list[dict[str, object]] = []
    ratings: list[dict[str, object]] = []
    cost = 0.0
    miles = 0.0
    for fact in current:
        payload = _fact_payload(fact)
        name = str(payload.get("asset") or fact.get("entity_name") or fact.get("summary") or "").strip()
        text = f"{fact.get('summary','')} {name} {payload.get('details','')}".lower()
        if any(token in text for token in ("行った", "訪問", "visit", "旅行した")) and name:
            visited.append(name)
            trips.append({"place": name, "date": fact.get("occurred_on") or fact.get("valid_from"),
                          "summary": fact.get("summary"), "fact_id": fact.get("id")})
        if any(token in text for token in ("行きたい", "候補", "want", "未訪問")) and name:
            wanted.append(name)
        if any(token in text for token in ("ホテル", "旅館", "宿")) and name:
            hotels.append(name)
            hotel_history.append({"hotel": name, "date": fact.get("occurred_on") or fact.get("valid_from"),
                                  "summary": fact.get("summary"), "fact_id": fact.get("id")})
        if any(token in text for token in ("電車", "新幹線", "飛行機", "航空", "バス", "車", "レンタカー", "フェリー")):
            transport.append(str(fact.get("summary") or name))
        if fact.get("fact_type") in {"preference", "like", "avoid"} or any(
            token in text for token in ("好き", "優先", "避けたい", "温泉", "海鮮", "スポーツ観戦", "混雑")
        ):
            preferences.append(str(fact.get("summary") or name))
        rating = payload.get("rating") or payload.get("evaluation") or payload.get("評価")
        if rating is not None:
            ratings.append({"subject": name, "rating": rating, "fact_id": fact.get("id")})
        amount = _amount_from_fact(fact)
        if amount is not None and any(token in text for token in ("費用", "料金", "宿泊", "旅行", "円")):
            cost += amount
        raw_miles = payload.get("miles") or payload.get("マイル")
        try:
            miles += float(raw_miles or 0)
        except (TypeError, ValueError):
            pass
    return {
        "visited_places": sorted(set(visited))[:30],
        "wanted_places": sorted(set(wanted))[:30],
        "unvisited_places": sorted(set(wanted) - set(visited))[:30],
        "hotels": sorted(set(hotels))[:30],
        "transport": sorted(set(transport))[:30],
        "preferences": sorted(set(preferences))[:30],
        "trips": trips[:50],
        "hotel_history": hotel_history[:50],
        "ratings": ratings[:50],
        "recorded_cost": cost,
        "miles": miles,
    }


def _housing_summary(facts: list[dict]) -> dict[str, object]:
    current = [fact for fact in facts if fact.get("status") == "current" and fact.get("review_state") == "confirmed"]
    current_items: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []
    current_rent: float | None = None
    current_profile: dict[str, object] = {}
    wishes: list[dict[str, object]] = []
    for fact in facts:
        payload = _fact_payload(fact)
        text = f"{fact.get('summary','')} {payload.get('details','')}"
        item = {"summary": fact.get("summary", ""), "value": payload, "fact_id": fact.get("id")}
        if fact.get("status") == "current" and fact.get("review_state") == "confirmed":
            current_items.append(item)
            if current_rent is None and any(token in text for token in ("家賃", "賃料", "rent")):
                current_rent = _amount_from_fact(fact)
                current_profile["rent"] = current_rent
                current_profile["rent_fact_id"] = fact.get("id")
            for key, markers in {
                "area": ("㎡", "平米", "広さ"),
                "layout": ("間取り", "1ldk", "1k", "2ldk"),
                "station": ("最寄", "駅"),
                "renewal": ("更新", "契約満了"),
                "equipment": ("設備", "洗面", "浴室", "宅配", "オートロック"),
            }.items():
                if key not in current_profile and any(marker.lower() in text.lower() for marker in markers):
                    current_profile[key] = payload.get(key) or payload.get("details") or fact.get("summary")
                    current_profile[f"{key}_fact_id"] = fact.get("id")
        if fact.get("fact_type") in {"preference", "requirement", "wish"} or any(
            token in text for token in ("希望", "必須", "ほしい", "欲しい")
        ):
            wishes.append(item)
        if any(token in text for token in ("候補", "物件", "引っ越し先", "内見")):
            candidates.append(item)
    comparisons = []
    for item in candidates[:30]:
        value = item.get("value") or {}
        try:
            candidate_rent = float(value.get("amount")) if value.get("amount") is not None else None
        except (TypeError, ValueError):
            candidate_rent = None
        if candidate_rent is None or current_rent is None:
            continue
        comparisons.append({
            "summary": item["summary"],
            "fact_id": item["fact_id"],
            "monthly_difference": candidate_rent - current_rent,
            "annual_difference": (candidate_rent - current_rent) * 12,
            "value": value,
        })
    return {
        "current": current_items[:30], "current_profile": current_profile,
        "wishes": wishes[:30], "candidates": candidates[:30],
        "current_rent": current_rent, "comparisons": comparisons,
    }


def _people_summary(facts: list[dict]) -> dict[str, object]:
    people: dict[int, dict[str, object]] = {}
    for fact in facts:
        if not fact.get("entity_id") or not fact.get("entity_name"):
            continue
        entity_id = int(fact["entity_id"])
        person = people.setdefault(entity_id, {
            "entity_id": entity_id, "name": str(fact["entity_name"]),
            "relationship": [], "preferences": [], "next_topics": [], "timeline": [],
        })
        summary = str(fact.get("summary") or "")
        item = {
            "date": fact.get("occurred_on") or fact.get("valid_from") or fact.get("source_created_at"),
            "summary": summary, "fact_id": fact.get("id"), "document_title": fact.get("document_title"),
        }
        person["timeline"].append(item)
        fact_type = str(fact.get("fact_type") or "")
        if fact_type in {"relationship", "relation"}:
            person["relationship"].append(summary)
        if fact_type in {"preference", "like", "avoid"} or any(marker in summary for marker in ("好き", "苦手", "興味")):
            person["preferences"].append(summary)
        if fact_type in {"plan", "schedule", "next_topic"} or any(marker in summary for marker in ("次に", "予定", "聞きたい")):
            person["next_topics"].append(summary)
    result = []
    for person in people.values():
        person["relationship"] = list(dict.fromkeys(person["relationship"]))
        person["preferences"] = list(dict.fromkeys(person["preferences"]))
        person["next_topics"] = list(dict.fromkeys(person["next_topics"]))
        person["timeline"].sort(key=lambda item: str(item.get("date") or ""), reverse=True)
        result.append(person)
    result.sort(key=lambda item: str(item["name"]))
    return {
        "people_count": len(result),
        "people": [item["name"] for item in result[:50]],
        "profiles": result[:50],
        "timeline_count": len(facts),
    }


def domain_projection(domain: str) -> dict[str, object]:
    requested_domain = domain
    domain = {"finance": "money", "relationship": "people"}.get(domain, domain)
    domain_filters = {
        "money": ("f.category='finance'", []),
        "travel": ("f.category='travel'", []),
        "housing": ("(f.category='housing' OR f.fact_key LIKE 'housing.%' OR f.summary LIKE '%家賃%' OR f.summary LIKE '%住居%' OR f.summary LIKE '%引っ越%')", []),
        "people": ("f.category='relationship' AND COALESCE(e.entity_type,f.resolved_entity_type)='person' AND f.subject_scope='person'", []),
    }
    where, parameters = domain_filters.get(domain, ("1=0", []))
    with db() as connection:
        facts = [dict(row) for row in connection.execute(
                    f"""SELECT f.id,f.fact_key,f.category,f.fact_type,f.status,r.state AS review_state,f.occurred_on,f.valid_from,f.valid_to,
                       f.subject_scope,f.retrieval_eligibility,f.truth_confidence,
                       f.summary,f.value_json,f.confidence,f.extractor,f.extractor_model,f.prompt_version,f.extracted_at,
                       (SELECT COUNT(*) FROM fact_evidence fe WHERE fe.fact_id=f.id) AS evidence_count,
                       e.id AS entity_id,e.canonical_name AS entity_name,COALESCE(e.entity_type,f.resolved_entity_type) AS entity_type,
                       d.title AS document_title,d.source_created_at,c.text AS evidence
                FROM facts f JOIN documents d ON d.id=f.document_id
                LEFT JOIN entities e ON e.id=f.subject_entity_id LEFT JOIN chunks c ON c.id=f.source_chunk_id
                LEFT JOIN fact_reviews r ON r.fact_id=f.id
                WHERE {where}
                  AND COALESCE(r.state,'pending') != 'rejected'
                  AND COALESCE(f.retrieval_eligibility,'pending')='eligible'
                ORDER BY CASE f.status WHEN 'current' THEN 0 ELSE 1 END,COALESCE(f.valid_from,f.occurred_on,f.created_at) DESC LIMIT 200""",
            parameters,
        )]
        decisions = [dict(row) for row in connection.execute(
            "SELECT * FROM decisions WHERE domain=? ORDER BY COALESCE(decided_on,created_at) DESC LIMIT 100",
            ({"money": "finance", "travel": "travel", "housing": "housing", "people": "relationship"}.get(domain, domain),),
        )]
        transactions = []
        transaction_candidates = []
        if domain == "money":
            transactions = eligible_finance_transactions(connection)[:200]
            transaction_candidates = [item for item in eligible_finance_transactions(connection, include_pending=True) if item.get("eligibility_state") == "pending"][:100]
    result = {
        "domain": requested_domain,
        "facts": facts,
        "current": [fact for fact in facts if fact["status"] == "current" and fact["review_state"] == "confirmed"],
        "history": [fact for fact in facts if fact["status"] != "current"],
        "entities": [{"id": item["entity_id"], "name": item["entity_name"]} for item in facts if item["entity_id"]],
        "decisions": decisions,
        "transactions": transactions,
        "transaction_candidates": transaction_candidates,
    }
    if domain == "money":
        summary = _money_summary(facts, transactions)
        moneyforward = moneyforward_projection(MONEYFORWARD_DB_PATH, snapshot_directory=ATTACHMENT_DIR / "integrations" / "moneyforward")
        result["moneyforward"] = moneyforward
        if moneyforward.get("connected"):
            if moneyforward.get("total_assets") is not None:
                summary["total_assets"] = moneyforward["total_assets"]
            if moneyforward.get("breakdown"):
                summary["breakdown"] = moneyforward["breakdown"]
            summary["source"] = "moneyforward"
        result["summary"] = summary
    elif domain == "travel":
        result["summary"] = _travel_summary(facts)
    elif domain == "housing":
        result["summary"] = _housing_summary(facts)
    elif domain == "people":
        result["summary"] = _people_summary(facts)
    return result


def recommendation_projection(domain: str | None = None) -> list[dict[str, object]]:
    with db() as connection:
        if domain:
            rows = connection.execute("SELECT * FROM recommendations WHERE domain=? ORDER BY updated_at DESC,id DESC LIMIT 100", (domain,)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM recommendations ORDER BY updated_at DESC,id DESC LIMIT 100").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in (
            "options_json", "criteria_json", "source_fact_ids_json", "source_decision_ids_json",
            "source_evidence_ids_json", "context_json", "tradeoffs_json", "missing_context_json",
        ):
            try:
                list_field = key.endswith("ids_json") or key in {"options_json", "tradeoffs_json", "missing_context_json"}
                item[key[:-5] if key.endswith("_json") else key] = json.loads(item[key] or ("[]" if list_field else "{}"))
            except json.JSONDecodeError:
                item[key] = [] if list_field else {}
        result.append(item)
    return result


def plan_projection(domain: str | None = None) -> list[dict[str, object]]:
    with db() as connection:
        if domain:
            rows = connection.execute("SELECT * FROM plans WHERE domain=? ORDER BY updated_at DESC,id DESC LIMIT 100", (domain,)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM plans ORDER BY updated_at DESC,id DESC LIMIT 100").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["steps"] = json.loads(item.pop("steps_json") or "[]")
        except json.JSONDecodeError:
            item["steps"] = []
        try:
            item["checkpoints"] = json.loads(item.pop("checkpoints_json") or "[]")
        except json.JSONDecodeError:
            item["checkpoints"] = []
        result.append(item)
    return result


def _json_value(value: object, default: object) -> object:
    try:
        parsed = json.loads(value or "") if isinstance(value, str) else value
        return parsed if parsed is not None else default
    except (TypeError, json.JSONDecodeError):
        return default


def cycle_snapshot(cycle_id: int) -> dict[str, object] | None:
    """Return one user-facing consultation cycle and its legal next actions.

    Recommendation is the stable cycle identifier.  Legacy records that were
    created without a recommendation remain accessible through their Plan or
    Decision projection, but new transitions always flow in this order.
    """
    with db() as connection:
        recommendation = connection.execute("SELECT * FROM recommendations WHERE id=?", (cycle_id,)).fetchone()
        if not recommendation:
            return None
        plan = connection.execute(
            "SELECT * FROM plans WHERE source_recommendation_id=? ORDER BY id DESC LIMIT 1", (cycle_id,)
        ).fetchone()
        decision = None
        if plan and plan["decision_id"]:
            decision = connection.execute("SELECT * FROM decisions WHERE id=?", (plan["decision_id"],)).fetchone()
        if not decision:
            decision = connection.execute(
                "SELECT * FROM decisions WHERE source_recommendation_id=? ORDER BY id DESC LIMIT 1", (cycle_id,)
            ).fetchone()
        events = []
        if decision:
            events = [dict(row) for row in connection.execute(
                "SELECT * FROM execution_events WHERE decision_id=? ORDER BY COALESCE(occurred_at,created_at),id", (decision["id"],)
            )]
    rec = dict(recommendation)
    for key in ("options_json", "criteria_json", "source_fact_ids_json", "source_decision_ids_json",
                "source_evidence_ids_json", "context_json", "tradeoffs_json", "missing_context_json"):
        rec[key[:-5]] = _json_value(rec.pop(key, None), [] if key.endswith("ids_json") or key in {"options_json", "tradeoffs_json", "missing_context_json"} else {})
    plan_data = None
    if plan:
        plan_data = dict(plan)
        plan_data["steps"] = _json_value(plan_data.pop("steps_json", "[]"), [])
        plan_data["checkpoints"] = _json_value(plan_data.pop("checkpoints_json", "[]"), [])
    decision_data = dict(decision) if decision else None
    if decision_data:
        for key in ("options_json", "related_fact_ids_json", "related_entity_ids_json"):
            decision_data[key[:-5]] = _json_value(decision_data.pop(key, None), [])
    if rec.get("status") == "dismissed":
        stage = "dismissed"
    elif not plan_data:
        stage = "recommended"
    elif not decision_data:
        stage = "planned"
    elif decision_data.get("later_evaluation"):
        stage = "evaluated"
    elif decision_data.get("result") or decision_data.get("decision_state") == "result":
        stage = "result"
    elif decision_data.get("decision_state") == "executed":
        stage = "executed"
    elif decision_data.get("decision_state") == "decided":
        stage = "decided"
    else:
        stage = "planned"
    action_map = {
        "recommended": ["create_plan", "dismiss"],
        "planned": ["create_decision", "cancel"],
        "decided": ["mark_executed", "cancel"],
        "executed": ["record_result"],
        "result": ["evaluate_later"],
        "evaluated": [],
        "dismissed": [],
    }
    if stage == "planned" and decision_data and decision_data.get("decision_state") in {"candidate", "considered"}:
        action_map["planned"] = ["confirm_decision", "cancel"]
    return {
        "cycle_id": cycle_id,
        "cycle_stage": stage,
        "recommendation": rec,
        "plan": plan_data,
        "decision": decision_data,
        "execution_events": events,
        "result": (decision_data or {}).get("result", "") if decision_data else "",
        "later_evaluation": (decision_data or {}).get("later_evaluation", "") if decision_data else "",
        "available_actions": action_map.get(stage, []),
    }


REPLAY_STAGE_LABELS = {
    "trigger": "きっかけ",
    "context": "検討した背景",
    "options": "選択肢",
    "recommendation": "相談で得た提案",
    "decision": "決めたこと",
    "rationale": "決めた理由",
    "execution": "実行",
    "result": "結果",
    "later_evaluation": "後日評価",
    "lesson": "次回に活かすこと",
}
REPLAY_SENSITIVE_DOMAINS = {"finance", "money", "relationship", "people", "health"}


def _replay_safe_text(domain: str, value: object, fallback: str, include_sensitive: bool) -> str:
    if canonical_domain(domain) in REPLAY_SENSITIVE_DOMAINS and not include_sensitive:
        return fallback
    return str(value or fallback).strip()[:4000]


def _replay_stage(stage: str, *, summary: str = "", items: list[str] | None = None,
                  occurred_at: str | None = None, status: str = "recorded",
                  basis: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "stage": stage,
        "label": REPLAY_STAGE_LABELS[stage],
        "status": status,
        "summary": summary,
        "items": items or [],
        "occurred_at": occurred_at or "",
        "basis": basis or [],
    }


def _replay_next_time(value: object) -> str:
    """Return an explicitly recorded lesson; never infer one from an LLM."""
    for line in str(value or "").splitlines():
        normalized = line.strip()
        if normalized.startswith(("次回:", "次回に活かすこと:", "次回に活かすこと：")):
            return normalized.split(":", 1)[-1].split("：", 1)[-1].strip()
    return ""


def decision_replay(decision_id: int, include_sensitive: bool = False) -> dict[str, object] | None:
    """Read-only lifecycle projection for a user-owned Decision.

    Recommendations remain a distinct assistant artifact; this function only
    shows them as context and never upgrades them into a user Decision.
    """
    with db() as connection:
        row = connection.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if not row:
            return None
        decision = dict(row)
        recommendation = None
        if decision.get("source_recommendation_id"):
            recommendation_row = connection.execute(
                "SELECT id,title,rationale,options_json,created_at,updated_at FROM recommendations WHERE id=?",
                (decision["source_recommendation_id"],),
            ).fetchone()
            recommendation = dict(recommendation_row) if recommendation_row else None
        plan = connection.execute(
            "SELECT id,title,steps_json,status,created_at,updated_at FROM plans WHERE decision_id=? ORDER BY id DESC LIMIT 1",
            (decision_id,),
        ).fetchone()
        events = [dict(item) for item in connection.execute(
            "SELECT id,event_type,summary,occurred_at,created_at FROM execution_events WHERE decision_id=? ORDER BY COALESCE(occurred_at,created_at),id",
            (decision_id,),
        )]
        related_ids = _json_value(decision.get("related_fact_ids_json"), [])
        related_ids = [int(value) for value in related_ids if str(value).isdigit()][:20]
        related_facts: list[dict[str, object]] = []
        if related_ids:
            marks = ",".join("?" for _ in related_ids)
            related_facts = [dict(item) for item in connection.execute(
                f"""SELECT f.id,f.category,f.summary,f.status,COUNT(e.id) AS evidence_count
                    FROM facts f LEFT JOIN fact_evidence e ON e.fact_id=f.id
                    WHERE f.id IN ({marks}) GROUP BY f.id ORDER BY f.id""",
                related_ids,
            )]

    domain = canonical_domain(str(decision.get("domain") or "other"))
    masked = domain in REPLAY_SENSITIVE_DOMAINS and not include_sensitive
    options = _json_value(decision.get("options_json"), [])
    options = [str(item)[:500] for item in options] if isinstance(options, list) else []
    decision_basis = [{"kind": "decision", "id": int(decision_id)}]
    fact_basis = [
        {"kind": "fact", "id": int(item["id"]), "summary": _replay_safe_text(domain, item.get("summary"), "確認済みの関連情報", include_sensitive),
         "evidence_count": int(item.get("evidence_count") or 0)}
        for item in related_facts
    ]
    stages: list[dict[str, object]] = []
    trigger = str(decision.get("question") or decision.get("title") or "").strip()
    stages.append(_replay_stage(
        "trigger", summary=_replay_safe_text(domain, trigger, "判断を記録しました", include_sensitive),
        occurred_at=str(decision.get("created_at") or ""), basis=decision_basis,
    ))
    context = str(decision.get("context") or "").strip()
    stages.append(_replay_stage(
        "context", summary=_replay_safe_text(domain, context, "検討した背景は未記録です", include_sensitive),
        occurred_at=str(decision.get("created_at") or ""),
        status="recorded" if context else "missing", basis=fact_basis,
    ))
    stages.append(_replay_stage(
        "options", items=[_replay_safe_text(domain, item, "機微情報の選択肢", include_sensitive) for item in options],
        occurred_at=str(decision.get("created_at") or ""), status="recorded" if options else "missing", basis=decision_basis,
    ))
    if recommendation:
        stages.append(_replay_stage(
            "recommendation", summary=_replay_safe_text(domain, recommendation.get("rationale") or recommendation.get("title"), "相談で得た提案があります", include_sensitive),
            occurred_at=str(recommendation.get("created_at") or ""), basis=[{"kind": "recommendation", "id": int(recommendation["id"])}],
        ))
    else:
        stages.append(_replay_stage("recommendation", summary="相談からの提案は記録されていません", status="not_applicable"))
    chosen = str(decision.get("selected_option") or decision.get("decision") or "").strip()
    state = str(decision.get("decision_state") or "candidate").lower()
    decision_recorded = state in {"decided", "executed", "result"} and bool(chosen)
    stages.append(_replay_stage(
        "decision", summary=_replay_safe_text(domain, chosen, "決めたことは未記録です", include_sensitive),
        occurred_at=str(decision.get("decided_on") or ""), status="recorded" if decision_recorded else "missing", basis=decision_basis,
    ))
    rationale = str(decision.get("rationale") or "").strip()
    stages.append(_replay_stage(
        "rationale", summary=_replay_safe_text(domain, rationale, "決めた理由は未記録です", include_sensitive),
        occurred_at=str(decision.get("decided_on") or ""), status="recorded" if rationale else "missing", basis=fact_basis or decision_basis,
    ))
    execution_summary = "\n".join(str(event.get("summary") or "").strip() for event in events if str(event.get("summary") or "").strip())
    execution_at = next((str(event.get("occurred_at") or event.get("created_at") or "") for event in events), "")
    executed = state in {"executed", "result"} or bool(events)
    stages.append(_replay_stage(
        "execution", summary=_replay_safe_text(domain, execution_summary, "実行はまだ記録されていません", include_sensitive),
        occurred_at=execution_at, status="recorded" if executed else "missing",
        basis=[{"kind": "execution_event", "id": int(event["id"])} for event in events] or decision_basis,
    ))
    result = str(decision.get("result") or "").strip()
    stages.append(_replay_stage(
        "result", summary=_replay_safe_text(domain, result, "結果はまだ記録されていません", include_sensitive),
        occurred_at=str(decision.get("outcome_recorded_at") or ""), status="recorded" if result else "missing", basis=decision_basis,
    ))
    evaluation = str(decision.get("later_evaluation") or "").strip()
    stages.append(_replay_stage(
        "later_evaluation", summary=_replay_safe_text(domain, evaluation, "後日評価はまだ記録されていません", include_sensitive),
        occurred_at=str(decision.get("evaluation_recorded_at") or ""), status="recorded" if evaluation else "missing", basis=decision_basis,
    ))
    lesson = _replay_next_time(evaluation) or _replay_next_time(result)
    stages.append(_replay_stage(
        "lesson", summary=_replay_safe_text(domain, lesson, "次回に活かすことは未記録です", include_sensitive),
        occurred_at=str(decision.get("evaluation_recorded_at") or decision.get("outcome_recorded_at") or ""),
        status="recorded" if lesson else ("missing" if result else "not_applicable"), basis=decision_basis,
    ))

    if state in {"candidate", "considered"}:
        next_action = {"type": "record_decision", "label": "決めたことを記録する"}
    elif state == "decided":
        next_action = {"type": "mark_executed", "label": "実行したと記録する"}
    elif state == "executed":
        next_action = {"type": "record_result", "label": "結果を記録する"}
    elif state == "result" and not evaluation:
        next_action = {"type": "record_evaluation", "label": "後日評価を記録する"}
    elif state == "result" and not lesson:
        next_action = {"type": "open_decision", "label": "次回に活かすことを追記する"}
    else:
        next_action = None
    public_decision = {
        "id": int(decision_id), "title": "機微情報の判断" if masked else str(decision.get("title") or "判断"),
        "domain": domain, "state": state, "created_at": str(decision.get("created_at") or ""),
        "decided_on": str(decision.get("decided_on") or ""), "masked": masked,
    }
    return {
        "decision": public_decision, "stages": stages,
        "missing_stages": [item["stage"] for item in stages if item["status"] == "missing"],
        "next_action": next_action, "has_sensitive_content": masked,
        "plan": {"id": int(plan["id"]), "title": "機微情報の計画" if masked else str(plan["title"]), "status": str(plan["status"])} if plan else None,
    }


def consultation_response_type(message: str) -> str:
    text = str(message or "").lower()
    if any(term in text for term in ("前の判断", "過去の判断", "正しかった", "結果どう", "振り返")):
        return "decision_review"
    if any(term in text for term in ("計画", "予定を組", "旅程", "段取り", "スケジュール")):
        return "planning"
    if any(term in text for term in ("どこ", "おすすめ", "候補", "した方が", "選ぶ", "どうする", "案を")):
        return "recommendation"
    return "answer_only"


def consultation_domain(message: str) -> str:
    text = str(message or "")
    if any(term in text for term in ("旅行", "旅", "ホテル", "温泉", "連休", "観光")):
        return "travel"
    if any(term in text for term in ("引っ越", "家賃", "住居", "部屋", "賃貸")):
        return "housing"
    if any(term in text for term in ("株", "投資", "資産", "積立", "売却", "家計")):
        return "money"
    if any(term in text for term in ("人間関係", "恋愛", "相手", "返信", "友人")):
        return "people"
    return "other"


_CYCLE_TRANSITIONS = {
    "candidate": {"candidate", "considered", "decided"},
    "considered": {"considered", "decided"},
    "decided": {"decided", "executed"},
    "executed": {"executed", "result"},
    "result": {"result"},
}


def valid_cycle_transition(current: str, requested: str) -> bool:
    return requested in _CYCLE_TRANSITIONS.get(str(current or "candidate"), {str(current or "candidate")})


def personal_inference_projection(domain: str | None = None) -> list[dict[str, object]]:
    domain = canonical_domain(domain) if domain else None
    with db() as connection:
        if domain:
            rows = connection.execute(
                "SELECT * FROM personal_inferences WHERE domain=? AND status='active' AND (expires_at IS NULL OR expires_at>=?) ORDER BY confidence DESC,last_evaluated_at DESC LIMIT 100",
                (domain, now()),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM personal_inferences WHERE status='active' AND (expires_at IS NULL OR expires_at>=?) ORDER BY confidence DESC,last_evaluated_at DESC LIMIT 100",
                (now(),),
            ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ("source_fact_ids_json", "source_decision_ids_json", "source_chunk_ids_json"):
            try:
                item[key[:-5]] = json.loads(item.pop(key) or "[]")
            except json.JSONDecodeError:
                item[key[:-5]] = []
        result.append(item)
    return result


def refresh_personal_inferences() -> dict[str, object]:
    """Derive short-lived patterns from eligible memory, never as Facts."""
    with db() as connection:
        fact_rows = connection.execute(
            """SELECT f.id,f.category,f.summary,f.source_chunk_id,c.speaker_role,c.source_type,c.text AS source_text
               FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
               LEFT JOIN chunks c ON c.id=f.source_chunk_id
               WHERE r.state='confirmed' AND f.personal_relevance='personal' AND f.retrieval_eligibility='eligible'
                 AND f.status IN ('current','superseded','historical')
                 AND COALESCE(c.speaker_role,'user') IN ('user','mixed','unknown')"""
        ).fetchall()
        decision_rows = connection.execute(
            """SELECT id,domain,title,rationale,result,later_evaluation,decision_state
               FROM decisions
               WHERE decision_state IN ('decided','executed','result')
               ORDER BY COALESCE(decided_on,created_at) DESC LIMIT 100"""
        ).fetchall()
        raw_rows = connection.execute(
            """SELECT c.id,c.text,c.speaker_role,c.source_type FROM chunks c WHERE c.is_active=1 AND
               (c.text LIKE '%Codex%' OR c.text LIKE '%Personal OS%' OR c.text LIKE '%自動化%' OR c.text LIKE '%AI%')
               AND c.speaker_role IN ('user','mixed','unknown')
               ORDER BY c.created_at DESC LIMIT 100"""
        ).fetchall()
    candidates: list[dict[str, object]] = []
    def usable_user_evidence(row: sqlite3.Row) -> str:
        role = str(row["speaker_role"] or "unknown")
        source_type = str(row["source_type"] or "unknown")
        if role in {"assistant", "system", "external"}:
            return ""
        if role == "unknown" and source_type not in {"manual", "screenshot", "ai-ingest"}:
            return ""
        text = str(row["source_text"] if "source_text" in row.keys() else row["text"] or "")
        return user_evidence_text(text)

    usable_facts = [row for row in fact_rows if usable_user_evidence(row)]
    usable_raw = [row for row in raw_rows if usable_user_evidence(row)]
    technology = []
    for row in usable_facts:
        if row["category"] not in {"technology", "learning"}:
            continue
        user_text = usable_user_evidence(row).lower()
        if row["speaker_role"] == "mixed":
            summary_text = str(row["summary"] or "").lower()
            tokens = [token.lower() for token in re.findall(r"[A-Za-z0-9_]{3,}|[^\W_]{3,}", summary_text)]
            needles = tokens + [term for term in ("codex", "ai", "自動化", "personal os") if term in summary_text]
            if not needles or not any(token in user_text for token in needles):
                continue
        technology.append(row)
    technology_chunks = [row["id"] for row in usable_raw if re.search(r"codex|personal os|自動化|ai", usable_user_evidence(row), re.I)]
    if technology or technology_chunks:
        candidates.append({
            "statement": "AI・自動化を使った個人開発に関心が強そう",
            "inference_type": "interest_pattern", "domain": "technology", "confidence": min(0.95, 0.55 + 0.08 * min(5, len(technology) + len(technology_chunks))),
            "source_fact_ids": [row["id"] for row in technology[:20]],
            "source_decision_ids": [], "source_chunk_ids": technology_chunks[:20],
        })
    category_counts: dict[str, list[sqlite3.Row]] = {}
    for row in fact_rows:
        category_counts.setdefault(str(row["category"]), []).append(row)
    for category, label in (("travel", "旅行"), ("finance", "資産"), ("housing", "住居"), ("relationship", "人間関係")):
        rows = category_counts.get(category, [])
        if len(rows) >= 3:
            candidates.append({
                "statement": f"{label}を継続的に記録・比較する傾向がある",
                "inference_type": "recurring_theme", "domain": category, "confidence": min(0.9, 0.5 + len(rows) * 0.05),
                "source_fact_ids": [row["id"] for row in rows[:20]],
                "source_decision_ids": [row["id"] for row in decision_rows if row["domain"] == category][:20],
                "source_chunk_ids": [row["source_chunk_id"] for row in rows if row["source_chunk_id"]][:20],
            })
    axis_terms = ("費用", "価格", "立地", "安全", "満足", "手間", "将来")
    for term in axis_terms:
        matched = [row for row in decision_rows if term in " ".join(str(row[key] or "") for key in ("rationale", "result", "later_evaluation"))]
        if len(matched) >= 2:
            candidates.append({
                "statement": f"判断で「{term}」を重視する傾向がありそう",
                "inference_type": "decision_axis", "domain": str(matched[0]["domain"] or "other"), "confidence": min(0.88, 0.55 + len(matched) * 0.08),
                "source_fact_ids": [], "source_decision_ids": [row["id"] for row in matched[:20]], "source_chunk_ids": [],
            })
    timestamp = now()
    with db() as connection:
        for candidate in candidates:
            existing = connection.execute(
                "SELECT id FROM personal_inferences WHERE statement=? AND status='active'",
                (candidate["statement"],),
            ).fetchone()
            if existing:
                connection.execute(
                    """UPDATE personal_inferences SET confidence=?,domain=?,source_fact_ids_json=?,source_decision_ids_json=?,source_chunk_ids_json=?,last_evaluated_at=?,expires_at=NULL WHERE id=?""",
                    (candidate["confidence"], candidate["domain"], json.dumps(candidate["source_fact_ids"], ensure_ascii=False),
                     json.dumps(candidate["source_decision_ids"], ensure_ascii=False), json.dumps(candidate["source_chunk_ids"], ensure_ascii=False), timestamp, existing["id"]),
                )
            else:
                connection.execute(
                    """INSERT INTO personal_inferences(statement,inference_type,domain,confidence,source_fact_ids_json,source_decision_ids_json,source_chunk_ids_json,created_at,last_evaluated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (candidate["statement"], candidate["inference_type"], candidate["domain"], candidate["confidence"],
                     json.dumps(candidate["source_fact_ids"], ensure_ascii=False), json.dumps(candidate["source_decision_ids"], ensure_ascii=False),
                     json.dumps(candidate["source_chunk_ids"], ensure_ascii=False), timestamp, timestamp),
                )
        active_statements = {str(candidate["statement"]) for candidate in candidates}
        if active_statements:
            marks = ",".join("?" for _ in active_statements)
            connection.execute(
                f"UPDATE personal_inferences SET status='expired',expires_at=? WHERE status='active' AND statement NOT IN ({marks})",
                (timestamp, *active_statements),
            )
        else:
            connection.execute(
                "UPDATE personal_inferences SET status='expired',expires_at=? WHERE status='active'",
                (timestamp,),
            )
    return {"generated": len(candidates), "inferences": personal_inference_projection()}


def personal_system_ideas() -> list[dict[str, object]]:
    """Generate context-backed system ideas without hard-coded recommendations.

    These are exploratory projections, not Facts or Decisions.  Every idea
    carries the Fact/chunk IDs that caused it so it can be inspected or
    regenerated when the underlying memory changes.
    """
    inferences = personal_inference_projection()
    with db() as connection:
        chunks = connection.execute(
            """SELECT id,text FROM chunks WHERE is_active=1 AND
               (text LIKE '%Codex%' OR text LIKE '%Personal OS%' OR text LIKE '%iPhone%' OR text LIKE '%スマホ%' OR text LIKE '%自動化%')
            ORDER BY created_at DESC LIMIT 60"""
        ).fetchall()
        decision_rows = connection.execute(
            """SELECT id,domain,title,result,later_evaluation,created_at
               FROM decisions
               WHERE result IS NOT NULL OR later_evaluation IS NOT NULL
               ORDER BY COALESCE(decided_on,created_at) DESC LIMIT 40"""
        ).fetchall()
    ideas: list[dict[str, object]] = []
    tech = [item for item in inferences if item.get("domain") == "technology"]
    mobile_chunks = [row for row in chunks if any(token in str(row["text"] or "").lower() for token in ("iphone", "スマホ", "mobile", "codex"))]
    if tech and mobile_chunks:
        ideas.append({
            "key": "mobile-personal-os-controller",
            "title": "スマホからPersonal OSを操作する開発コントローラー",
            "reason": "個人開発・AI活用とスマホ操作の記憶が繰り返し現れています。",
            "mvp": ["音声/テキスト入力", "作業依頼の下書き", "結果とテストの確認"],
            "usefulness": "high", "difficulty": "medium", "business_potential": "unknown",
            "source_fact_ids": sorted({fid for item in tech for fid in item.get("source_fact_ids", [])}),
            "source_chunk_ids": [int(row["id"]) for row in mobile_chunks[:20]],
            "generated_at": now(),
        })
    recurring = [item for item in inferences if item.get("inference_type") == "recurring_theme"]
    for item in recurring[:3]:
        domain = str(item.get("domain") or "other")
        ideas.append({
            "key": f"{domain}-decision-workbench",
            "title": f"{domain}の判断ワークベンチ",
            "reason": str(item.get("statement") or "繰り返し現れるテーマ"),
            "mvp": ["現在Factと過去履歴の比較", "候補ごとの根拠表示", "結果の記録"],
            "usefulness": "medium", "difficulty": "medium", "business_potential": "unknown",
            "source_fact_ids": item.get("source_fact_ids", []),
            "source_chunk_ids": item.get("source_chunk_ids", []),
            "generated_at": now(),
        })
    # A completed decision/result is a stronger signal than an assistant-only
    # suggestion: expose a follow-up system idea while keeping it separate
    # from Facts and Decisions themselves.
    for row in decision_rows[:3]:
        result_text = " ".join(str(row[key] or "") for key in ("result", "later_evaluation"))
        if not result_text.strip():
            continue
        ideas.append({
            "key": f"decision-followup-{row['id']}",
            "title": f"Decision follow-up: {row['title']}",
            "reason": f"A recorded {row['domain']} decision has an outcome/evaluation; capture the next reusable action.",
            "mvp": ["review the recorded result", "capture what to repeat or avoid", "link the next plan"],
            "usefulness": "medium", "difficulty": "low", "business_potential": "unknown",
            "source_decision_ids": [row["id"]], "source_fact_ids": [], "source_chunk_ids": [],
            "generated_at": now(),
        })
    # Every candidate is explicitly marked as a deterministic fallback.  A
    # future reasoning-engine adapter can replace this list while preserving
    # the same evidence lineage and output contract.
    evidence_ids = [str(row["id"]) for row in chunks] + [str(item.get("statement") or "") for item in inferences]
    signature = hashlib.sha256("|".join(evidence_ids).encode("utf-8")).hexdigest()[:8] if evidence_ids else "none"
    theme = next((str(row["text"] or "").strip().replace("\n", " ")[:36] for row in chunks if str(row["text"] or "").strip()), "current personal context")
    if len(ideas) < 3 and len(evidence_ids) >= 2:
        ideas.append({
            "key": f"evidence-review-{signature}",
            "title": f"Evidence review: {theme}",
            "reason": "Repeated user-owned evidence is available for a small review workflow.",
            "mvp": ["group the related evidence", "confirm the current constraint", "record the next action"],
            "usefulness": "medium", "difficulty": "low", "business_potential": "unknown",
            "source_fact_ids": [], "source_decision_ids": [], "source_chunk_ids": [int(row["id"]) for row in chunks[:20]],
            "generated_at": now(),
        })
    if len(ideas) < 3 and inferences:
        inference = inferences[0]
        ideas.append({
            "key": f"inference-check-{signature}",
            "title": f"Inference check: {inference.get('domain', 'other')}",
            "reason": "An active Personal Inference can be tested against newer Facts and Results.",
            "mvp": ["show supporting evidence", "check expiry and contradictions", "keep or reject the inference"],
            "usefulness": "medium", "difficulty": "low", "business_potential": "unknown",
            "source_fact_ids": inference.get("source_fact_ids", []), "source_decision_ids": inference.get("source_decision_ids", []),
            "source_chunk_ids": inference.get("source_chunk_ids", []), "generated_at": now(),
        })
    for item in ideas:
        item.setdefault("generation_mode", "fallback")
        item.setdefault("personal_evidence", item.get("source_fact_ids", []) or item.get("source_chunk_ids", []))
        item.setdefault("unknowns", [])
        item.setdefault("expected_personal_value", item.get("usefulness", "unknown"))
        item["title"] = f"{item['title']} [{signature}]"
    return ideas[:5]


def _format_yen(value: object) -> str:
    try:
        return f"{float(value):,.0f}円"
    except (TypeError, ValueError):
        return "不明"


def _decision_lesson(decision: dict) -> str:
    evaluation = str(decision.get("later_evaluation") or "").strip()
    result = str(decision.get("result") or "").strip()
    selected = str(decision.get("selected_option") or decision.get("decision") or "").strip()
    parts = [part for part in (selected, result, evaluation) if part]
    return " / ".join(parts)


def _recommendation_sources(question: str, domain: str) -> tuple[dict[str, object], list[int], list[int], list[int]]:
    domain = canonical_domain(domain)
    labels = {"money": "資産", "travel": "旅行", "housing": "住居", "people": "人間関係", "other": "生活"}
    context = retrieval_context(question or f"{labels.get(domain, domain)}について次にどうする")
    refresh_personal_inferences()
    context["inferences"] = personal_inference_projection(domain)
    fact_ids = [
        int(item["fact_id"]) for group in ("current", "history")
        for item in context[group] if item.get("fact_id")
    ]
    decision_ids = [int(item["decision_id"]) for item in context["decisions"] if item.get("decision_id")]
    evidence_ids: list[int] = []
    if fact_ids:
        with db() as connection:
            marks = ",".join("?" for _ in fact_ids)
            evidence_ids = [
                int(row["id"]) for row in connection.execute(
                    f"""SELECT id FROM fact_evidence
                        WHERE fact_id IN ({marks}) AND support IN ('supports','context')
                        ORDER BY reliability DESC,id LIMIT 40""",
                    fact_ids,
                )
            ]
    return context, list(dict.fromkeys(fact_ids)), list(dict.fromkeys(decision_ids)), evidence_ids


def build_local_recommendation(domain: str, question: str = "") -> dict[str, object]:
    """Create a data-dependent, explainable draft; never execute an action."""
    projection = domain_projection(domain)
    context, source_fact_ids, source_decision_ids, source_evidence_ids = _recommendation_sources(question, domain)
    inferences = list(context.get("inferences") or [])
    labels = {"money": "資産", "travel": "旅行", "housing": "住居", "people": "人間関係", "other": "生活"}
    title = question.strip()[:300] or f"{labels.get(domain, domain)}について次にどうするか"
    missing = missing_context_for_query(question or title)
    recent_decisions = [
        item for item in projection.get("decisions", [])
        if item.get("result") or item.get("later_evaluation")
    ][:3]
    lessons = [_decision_lesson(item) for item in recent_decisions if _decision_lesson(item)]
    summary = projection.get("summary", {})
    options: list[str] = []
    tradeoffs: list[dict[str, object]] = []
    plan_steps: list[dict[str, object]] = []
    rationale_parts: list[str] = []

    if domain == "travel":
        wanted = list(summary.get("wanted_places") or [])
        visited = list(summary.get("visited_places") or [])
        recorded_cost = summary.get("recorded_cost")
        miles = summary.get("miles")
        for place in wanted[:3]:
            options.append(f"{place}を次の候補として比較する")
        if visited:
            options.append(f"過去に訪問した候補を再評価する（例: {visited[0]}）")
        if not options:
            options = ["未訪問地域を2〜3件登録して比較する", "日程と上限予算を先に決める"]
        rationale_parts.append(f"訪問済み{len(visited)}件、行きたい候補{len(wanted)}件を参照しました。")
        if recorded_cost:
            rationale_parts.append(f"記録済み旅行費用は合計{_format_yen(recorded_cost)}です。")
        if miles:
            rationale_parts.append(f"記録済みマイルは{float(miles):,.0f}です。")
        for option in options:
            tradeoffs.append({
                "option": option,
                "benefit": "保存済みの訪問履歴・希望と比較できる",
                "cost": "日程と交通費の追加確認が必要" if missing else "保存済み情報で一次比較可能",
                "risk": "季節・混雑・最新価格は未反映",
            })
        plan_steps = [
            {"order": 1, "title": "条件を確定", "detail": "日程、予算上限、疲労度、同行者を確認する", "status": "pending"},
            {"order": 2, "title": "候補を比較", "detail": "移動負担、概算費用、過去訪問・評価を同じ軸で比べる", "status": "pending"},
            {"order": 3, "title": "旅程を下書き", "detail": "移動、宿泊、食事・温泉等の希望を時系列にする", "status": "pending"},
        ]
    elif domain == "housing":
        comparisons = list(summary.get("comparisons") or [])
        current_rent = summary.get("current_rent")
        if comparisons:
            for comparison in comparisons[:3]:
                options.append(str(comparison.get("summary") or "候補物件") + "を現住居と比較する")
        options.append("現住居に住み続ける")
        if not comparisons:
            options.append("候補物件の家賃・広さ・設備を登録して比較する")
        if current_rent is not None:
            rationale_parts.append(f"現在家賃として{_format_yen(current_rent)}を参照しました。")
        rationale_parts.append(f"比較可能な候補は{len(comparisons)}件です。")
        for option in list(dict.fromkeys(options)):
            tradeoffs.append({
                "option": option,
                "benefit": "生活満足と住居条件を比較できる",
                "cost": "家賃差・初期費用・年間差額を負担する可能性",
                "risk": "未登録の設備・立地条件は比較できない",
            })
        options = list(dict.fromkeys(options))
        plan_steps = [
            {"order": 1, "title": "比較条件を揃える", "detail": "家賃、初期費用、広さ、間取り、設備、駅距離を登録する", "status": "pending"},
            {"order": 2, "title": "年間差額を確認", "detail": "月額差だけでなく年間費用と資産形成への影響を算出する", "status": "pending"},
            {"order": 3, "title": "次の行動を決める", "detail": "継続、内見、更新期限まで保留のいずれかを本人が選ぶ", "status": "pending"},
        ]
    elif domain == "money":
        total = summary.get("total_assets")
        monthly = summary.get("monthly_investment")
        breakdown = summary.get("breakdown") or {}
        options = ["現状を維持して次回更新日に再確認する"]
        if breakdown:
            options.append("資産配分の偏りだけを確認する")
        else:
            options.append("現金・投信・株式等の内訳を追加してから配分を判断する")
        if monthly is not None:
            options.append("月間積立額と生活余力のバランスを再計算する")
        if total is not None:
            rationale_parts.append(f"確認済み総資産{_format_yen(total)}を参照しました。")
        if monthly is not None:
            rationale_parts.append(f"月間積立額{_format_yen(monthly)}を参照しました。")
        rationale_parts.append(f"集計対象は本人の適格取引{int(summary.get('transaction_count') or 0)}件だけです。")
        for option in options:
            tradeoffs.append({
                "option": option,
                "benefit": "確認済みFactと実取引だけで比較する",
                "cost": "変更する場合は生活余力と税・手数料の確認が必要",
                "risk": "市場予測や自動売買は行わない",
            })
        plan_steps = [
            {"order": 1, "title": "数値の基準日を確認", "detail": "総資産・内訳・積立額が同じ時点か確認する", "status": "pending"},
            {"order": 2, "title": "選択肢を比較", "detail": "維持・配分確認・積立見直しを費用と目的で比較する", "status": "pending"},
            {"order": 3, "title": "本人が判断", "detail": "売買は実行せず、採用する方針だけをDecisionとして記録する", "status": "pending"},
        ]
    elif domain == "people":
        people = list(summary.get("people") or [])
        options = [f"{name}との過去の会話と次に確認したいことを整理する" for name in people[:3]]
        if not options:
            options = ["対象人物と明示された事実を確認してから相談する", "判断を保留する"]
        rationale_parts.append(f"本人との関係が確認できる人物{len(people)}名だけを対象にしました。")
        for option in options:
            tradeoffs.append({
                "option": option,
                "benefit": "明示された会話・予定だけを根拠にできる",
                "cost": "相手の心理は確定できない",
                "risk": "性格・好感度・恋愛進展度を推測Factにしない",
            })
        plan_steps = [
            {"order": 1, "title": "事実を確認", "detail": "会った日、話題、次の予定など明示Evidenceだけを整理する", "status": "pending"},
            {"order": 2, "title": "候補を作る", "detail": "返信案・次に聞くこと・保留を比較する", "status": "pending"},
            {"order": 3, "title": "本人が選ぶ", "detail": "送信や連絡は自動実行せず、選択だけを記録する", "status": "pending"},
        ]
    else:
        options = ["関連する現在情報を確認する", "不足情報を一つ追加する", "判断を保留する"]
        tradeoffs = [{"option": option, "benefit": "根拠を整理できる", "cost": "追加確認が必要な場合がある", "risk": "根拠不足では個人最適化しない"} for option in options]
        plan_steps = [
            {"order": 1, "title": "目的を確認", "detail": "何を決めたいかを一文にする", "status": "pending"},
            {"order": 2, "title": "候補を比較", "detail": "利点・欠点・必要情報を整理する", "status": "pending"},
        ]

    if lessons:
        rationale_parts.append("過去の結果: " + " / ".join(lessons[:2]))
    if inferences:
        rationale_parts.append("保存済みFactから再生成した傾向: " + " / ".join(str(item["statement"]) for item in inferences[:2]))
    if not source_fact_ids:
        rationale_parts.append("質問に直接対応する確認済みFactが少ないため、一般的な整理案です。")
    rationale = " ".join(rationale_parts)
    personalization_level = "high" if len(source_fact_ids) + len(source_decision_ids) + len(inferences) >= 3 else ("medium" if context["current"] or context["decisions"] else "low")
    return {
        "domain": domain,
        "title": title,
        "recommendation": rationale,
        "rationale": rationale,
        "options": options[:5],
        "tradeoffs": tradeoffs[:5],
        "reason": rationale,
        "personal_context_used": [*source_fact_ids, *source_decision_ids, *source_evidence_ids],
        "assumptions": ["Unverified raw evidence is context only and cannot replace a confirmed current Fact."],
        "missing_information": missing,
        "plan": plan_steps,
        "personalization_level": personalization_level,
        "generation_mode": "fallback",
        "plan_steps": plan_steps,
        "missing_context": missing,
        "criteria": {
            "source": "query_relevant_memory",
            "question": question,
            "current_count": len(context["current"]),
            "history_count": len(context["history"]),
            "decision_count": len(context["decisions"]),
            "inference_count": len(inferences),
            "external_execution": False,
        },
        "context": context,
        "source_fact_ids": source_fact_ids,
        "source_decision_ids": source_decision_ids,
        "source_evidence_ids": source_evidence_ids,
    }


def today_snapshot() -> dict[str, object]:
    money = domain_projection("money")["current"]
    housing = domain_projection("housing")["current"]
    travel = domain_projection("travel")["current"]
    with db() as connection:
        pending_decision_rows = connection.execute(
            "SELECT id,title,question FROM decisions WHERE status='considering' OR decision_state IN ('candidate','considered') ORDER BY created_at DESC LIMIT 3"
        ).fetchall()
        pending_decisions = len(pending_decision_rows)
        changes = [dict(row) for row in connection.execute(
            "SELECT summary,created_at FROM memory_changes ORDER BY created_at DESC LIMIT 5"
        )]
        recent_themes = [dict(row) for row in connection.execute(
            """SELECT f.category, COUNT(*) AS count, MAX(f.created_at) AS latest
               FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
               WHERE r.state='confirmed' AND COALESCE(f.retrieval_eligibility,'pending')='eligible'
               GROUP BY f.category ORDER BY latest DESC LIMIT 5"""
        )]
        cycle_ids = [int(row["id"]) for row in connection.execute(
            "SELECT id FROM recommendations WHERE status NOT IN ('dismissed') ORDER BY updated_at DESC,id DESC LIMIT 10"
        )]
    cycles = [cycle_snapshot(cycle_id) for cycle_id in cycle_ids]
    cycles = [cycle for cycle in cycles if cycle]
    stage_order = {"decided": 0, "executed": 1, "planned": 2, "recommended": 3, "result": 4, "evaluated": 5}
    cycles.sort(key=lambda cycle: (stage_order.get(str(cycle["cycle_stage"]), 9), str(cycle["recommendation"].get("updated_at", ""))), reverse=False)
    next_candidates = [
        {"kind": "decision", "title": str(row["title"] or row["question"] or "unfinished decision"), "reason": "Decision is still open.", "source_decision_id": row["id"]}
        for row in pending_decision_rows
    ]
    if len(next_candidates) < 3:
        for idea in personal_system_ideas()[:3 - len(next_candidates)]:
            next_candidates.append({
                "kind": "system_idea", "title": idea.get("title"), "reason": idea.get("reason"),
                "source_fact_ids": idea.get("source_fact_ids", []), "source_chunk_ids": idea.get("source_chunk_ids", []),
            })
    return {
        "money": money[:3],
        "housing": housing[:3],
        "travel": travel[:3],
        "pending_decisions": pending_decisions,
        "changes": changes,
        "recent_themes": recent_themes,
        "next_candidates": next_candidates[:3],
        "cycles": cycles[:3],
    }


def _digest_domain_label(domain: object) -> str:
    return {
        "finance": "資産", "travel": "旅行", "housing": "住居", "relationship": "人間関係",
        "work": "仕事", "health": "健康", "life": "生活", "learning": "学習",
        "hobby": "趣味", "food": "食事", "shopping": "買い物",
    }.get(canonical_domain(str(domain or "other")), "その他")


def _digest_safe_text(domain: object, text: object, fallback: str) -> str:
    """Keep the daily overview useful without exposing sensitive summaries by default."""
    if canonical_domain(str(domain or "other")) in {"finance", "relationship", "health"}:
        return fallback
    return str(text or fallback)[:120]


def _parsed_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _review_snooze_is_future(value: object) -> bool:
    parsed = _parsed_iso(value)
    return bool(parsed and parsed > datetime.now(timezone.utc))


SENSITIVE_REVIEW_SUMMARY = "機微情報の確認が必要です"

_FACT_REVIEW_CTE = """
WITH review_rows AS (
  SELECT f.id,f.category,f.fact_type,f.fact_key,f.occurred_on,f.value_json,f.summary,
         f.confidence,f.truth_confidence,f.subject_scope,f.resolved_entity_type,
         f.retrieval_eligibility,f.validation_status,f.validation_reason,f.extractor,f.extractor_model,
         f.prompt_version,f.extracted_at,f.created_at,
         (SELECT COUNT(*) FROM fact_evidence fe WHERE fe.fact_id=f.id) AS evidence_count,
         r.state AS review_state,r.reason AS review_reason,r.review_note,q.snoozed_until,
         COALESCE(q.presentation_count,0) AS presentation_count,
         CASE WHEN EXISTS (
           SELECT 1 FROM facts cf JOIN fact_reviews cr ON cr.fact_id=cf.id
           WHERE cf.id<>f.id AND cf.fact_key=f.fact_key AND cf.status='current' AND cr.state='confirmed'
         ) THEN 1 ELSE 0 END AS is_current_replacement,
         CASE WHEN EXISTS (
           SELECT 1 FROM finance_transaction_candidates fc
           WHERE fc.fact_id=f.id AND fc.is_actual=1 AND fc.eligibility_state='pending'
         ) THEN 1 ELSE 0 END AS is_actual_transaction,
         CASE
           WHEN LOWER(COALESCE(f.retrieval_eligibility,''))='conflict'
             OR LOWER(COALESCE(f.validation_status,''))='conflict' THEN 0
           WHEN EXISTS (
             SELECT 1 FROM facts cf JOIN fact_reviews cr ON cr.fact_id=cf.id
             WHERE cf.id<>f.id AND cf.fact_key=f.fact_key AND cf.status='current' AND cr.state='confirmed'
           ) THEN 1
           WHEN EXISTS (
             SELECT 1 FROM decisions ad,
               json_each(CASE WHEN json_valid(ad.related_fact_ids_json) THEN ad.related_fact_ids_json ELSE '[]' END) jf
             WHERE ad.decision_state IN ('candidate','considered','decided','executed')
               AND CAST(jf.value AS INTEGER)=f.id
           ) THEN 2
           WHEN LOWER(COALESCE(f.validation_status,'')) IN ('anomaly','outlier')
             OR LOWER(COALESCE(f.validation_reason,'')) LIKE '%anomaly%'
             OR LOWER(COALESCE(f.validation_reason,'')) LIKE '%outlier%'
             OR COALESCE(f.validation_reason,'') LIKE '%外れ値%'
             OR COALESCE(r.reason,'') LIKE '%外れ値%' THEN 3
           WHEN EXISTS (
             SELECT 1 FROM finance_transaction_candidates fc
             WHERE fc.fact_id=f.id AND fc.is_actual=1 AND fc.eligibility_state='pending'
           ) THEN 4
           WHEN LOWER(COALESCE(f.fact_type,'')) IN ('schedule','plan')
             AND date(COALESCE(NULLIF(f.valid_from,''),NULLIF(f.occurred_on,''),
               json_extract(f.value_json,'$.target_date'),json_extract(f.value_json,'$.due_date'),
               json_extract(f.value_json,'$.date'))) BETWEEN date('now') AND date('now','+14 day') THEN 5
           ELSE 20
         END AS urgent_priority,
         COALESCE(NULLIF(f.created_at,''),'') AS sort_time
  FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
  LEFT JOIN fact_review_queue_state q ON q.fact_id=f.id
  WHERE r.state IN ('pending','deferred')
)
"""


def _fact_review_bucket_clause(bucket: str) -> tuple[str, int]:
    if bucket == "urgent":
        return "review_state='pending' AND (snoozed_until IS NULL OR snoozed_until<=?) AND urgent_priority<20", 0
    if bucket == "normal":
        return "review_state='pending' AND (snoozed_until IS NULL OR snoozed_until<=?) AND urgent_priority=20", 1
    return "(review_state='deferred' OR (review_state='pending' AND snoozed_until>?))", 2


def _review_after_clause(bucket_rank: int, item_kind: str, after_key: tuple[object, ...] | None) -> tuple[str, list[object]]:
    if after_key is None:
        return "", []
    return (
        " AND (?,urgent_priority,sort_time,?,id)>(?,?,?,?,?)",
        [bucket_rank, item_kind, *after_key],
    )


def _review_date_within_days(row: dict[str, object], days: int = 14) -> bool:
    candidates = [row.get("occurred_on"), row.get("valid_from")]
    try:
        value = json.loads(str(row.get("value_json") or "{}"))
    except json.JSONDecodeError:
        value = {}
    if isinstance(value, dict):
        candidates.extend(value.get(key) for key in ("date", "occurred_on", "target_date", "due_date"))
    today = datetime.now(timezone.utc).date()
    for candidate in candidates:
        parsed = _parsed_iso(candidate)
        if parsed and today <= parsed.date() <= today + timedelta(days=days):
            return True
        text = str(candidate or "")[:10]
        try:
            parsed_date = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= parsed_date <= today + timedelta(days=days):
            return True
    return False


def _review_bucket(row: dict[str, object], decision_fact_ids: set[int]) -> tuple[str, int, str]:
    """Classify a review deterministically without changing the Fact."""
    snoozed_until = row.get("snoozed_until")
    if str(row.get("review_state") or "") == "deferred":
        return "deferred", 0, "あとで確認するよう指定されています"
    elif _review_snooze_is_future(snoozed_until):
        return "deferred", 0, "あとで確認するよう指定されています"

    eligibility = str(row.get("retrieval_eligibility") or "").lower()
    validation = str(row.get("validation_status") or "").lower()
    if eligibility == "conflict" or validation == "conflict":
        return "urgent", 0, "現在の記憶と矛盾する可能性があります"
    if int(row.get("is_current_replacement") or 0):
        return "urgent", 1, "現在の情報を更新する可能性があります"
    if int(row.get("id") or 0) in decision_fact_ids:
        return "urgent", 2, "相談や判断の根拠として参照された情報です"
    reason_text = f"{row.get('validation_reason') or ''} {row.get('review_reason') or ''} {row.get('reason') or ''}".lower()
    if validation in {"anomaly", "outlier"} or any(token in reason_text for token in ("anomaly", "outlier", "外れ値")):
        return "urgent", 3, "現在値へ影響する数値の外れ値候補です"
    if int(row.get("is_actual_transaction") or 0):
        return "urgent", 4, "本人の実取引として抽出された未確認情報です"
    fact_type = str(row.get("fact_type") or "").lower()
    if fact_type in {"schedule", "plan"} and _review_date_within_days(row):
        return "urgent", 5, "14日以内に予定日または期限を迎える情報です"
    return "normal", 0, str(row.get("review_reason") or row.get("reason") or "根拠を確認して確定できます")


def _review_cursor_encode(key: tuple[object, ...]) -> str:
    raw = json.dumps(list(key), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _review_cursor_decode(cursor: str | None) -> tuple[object, ...] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid cursor") from error
    if not isinstance(value, list) or len(value) != 5:
        raise ValueError("Invalid cursor")
    return tuple(value)


def _proposal_projection(row: dict[str, object]) -> dict[str, object]:
    try:
        facts = json.loads(str(row.get("facts_json") or "[]"))
    except json.JSONDecodeError:
        facts = []
    facts = facts if isinstance(facts, list) else []
    domains = [canonical_domain(str(item.get("category") or "other")) for item in facts if isinstance(item, dict)]
    domain = domains[0] if domains else "other"
    sensitive = any(item in {"finance", "relationship", "health"} for item in domains)
    first = next((item for item in facts if isinstance(item, dict)), {})
    summary = SENSITIVE_REVIEW_SUMMARY if sensitive else str(first.get("summary") or row.get("entry_title") or "保存候補を確認してください")[:500]
    return {
        "id": int(row["id"]), "proposal_id": int(row["id"]), "entry_id": int(row.get("entry_id") or 0), "item_kind": "memory_proposal",
        "item_key": f"memory_proposal:{int(row['id'])}", "category": domain,
        "domain_label": _digest_domain_label(domain), "bucket": "deferred" if int(row.get("is_deferred") or 0) or _review_snooze_is_future(row.get("snoozed_until")) else "normal",
        "priority_reason": "AIが抽出した保存候補です", "sensitive": sensitive, "summary": summary,
        "evidence_count": len(facts), "created_at": str(row.get("created_at") or ""),
        "presentation_count": int(row.get("presentation_count") or 0),
        "_priority": 20, "_sort_time": str(row.get("created_at") or ""),
    }


def review_inbox_projection(
    bucket: str = "urgent", domain: str = "", limit: int = 10,
    cursor: str | None = None,
) -> dict[str, object]:
    """Project a bounded page from bucket-specific SQL queries.

    Classification happens before LIMIT so a recent conflict cannot be hidden
    behind thousands of older normal reviews.
    """
    if bucket not in {"urgent", "normal", "deferred", "all"}:
        raise ValueError("Invalid review bucket")
    limit = max(1, min(int(limit), 100))
    after_key = _review_cursor_decode(cursor)
    requested_domain = canonical_domain(domain) if domain else ""
    bucket_rank = {"urgent": 0, "normal": 1, "deferred": 2}
    requested_buckets = ["urgent", "normal", "deferred"] if bucket == "all" else [bucket]
    timestamp = now()
    with db() as connection:
        counts = {"urgent": 0, "normal": 0, "deferred": 0}
        projected: list[dict[str, object]] = []
        sensitive_domains = {"finance", "relationship", "health"}

        for name in ("urgent", "normal", "deferred"):
            clause, _rank = _fact_review_bucket_clause(name)
            domain_clause = " AND category=?" if requested_domain else ""
            params: list[object] = [timestamp]
            if requested_domain:
                params.append(requested_domain)
            counts[name] += int(connection.execute(
                _FACT_REVIEW_CTE + f"SELECT COUNT(*) FROM review_rows WHERE {clause}{domain_clause}", params,
            ).fetchone()[0])
            proposal_clause = (
                "(q.is_deferred=1 OR (q.snoozed_until IS NOT NULL AND q.snoozed_until>?))"
                if name == "deferred" else
                "COALESCE(q.is_deferred,0)=0 AND (q.snoozed_until IS NULL OR q.snoozed_until<=?)"
                if name == "normal" else "0"
            )
            if name != "urgent":
                proposal_params: list[object] = [timestamp]
                proposal_domain = ""
                if requested_domain:
                    proposal_domain = " AND json_extract(p.facts_json,'$[0].category')=?"
                    proposal_params.append(requested_domain)
                counts[name] += int(connection.execute(
                    f"""SELECT COUNT(*) FROM memory_proposals p
                         LEFT JOIN memory_proposal_queue_state q ON q.proposal_id=p.id
                         WHERE p.status='pending' AND {proposal_clause}{proposal_domain}""",
                    proposal_params,
                ).fetchone()[0])

        fetch_size = limit + 1
        for name in requested_buckets:
            clause, rank = _fact_review_bucket_clause(name)
            after_sql, after_params = _review_after_clause(rank, "fact", after_key)
            domain_clause = " AND category=?" if requested_domain else ""
            params = [timestamp]
            if requested_domain:
                params.append(requested_domain)
            params.extend(after_params)
            params.append(fetch_size)
            rows = [dict(row) for row in connection.execute(
                _FACT_REVIEW_CTE + f"""SELECT * FROM review_rows WHERE {clause}{domain_clause}{after_sql}
                    ORDER BY urgent_priority,sort_time,id LIMIT ?""", params,
            )]
            for row in rows:
                row_domain = canonical_domain(str(row.get("category") or "other"))
                sensitive = row_domain in sensitive_domains
                priority = int(row.get("urgent_priority") or 0)
                urgent_reasons = {
                    0: "現在の記憶と矛盾する可能性があります",
                    1: "現在の情報を更新する可能性があります",
                    2: "相談や判断の根拠として参照された情報です",
                    3: "現在値へ影響する数値の外れ値候補です",
                    4: "本人の実取引として抽出された未確認情報です",
                    5: "14日以内に予定日または期限を迎える情報です",
                }
                reason = urgent_reasons.get(priority, str(row.get("review_reason") or "根拠を確認して確定できます")) if name == "urgent" else str(row.get("review_reason") or "根拠を確認して確定できます")
                projected.append({
                    "id": int(row["id"]), "item_kind": "fact", "item_key": f"fact:{int(row['id'])}",
                    "category": row_domain, "domain_label": _digest_domain_label(row_domain), "bucket": name,
                    "priority_reason": reason, "sensitive": sensitive,
                    "summary": SENSITIVE_REVIEW_SUMMARY if sensitive else str(row.get("summary") or "確認が必要な記憶")[:500],
                    "evidence_count": int(row.get("evidence_count") or 0),
                    "created_at": str(row.get("created_at") or ""),
                    "presentation_count": int(row.get("presentation_count") or 0),
                    "_priority": priority, "_sort_time": str(row.get("sort_time") or ""),
                })

            if name == "urgent":
                continue
            proposal_condition = (
                "(q.is_deferred=1 OR (q.snoozed_until IS NOT NULL AND q.snoozed_until>?))"
                if name == "deferred" else
                "COALESCE(q.is_deferred,0)=0 AND (q.snoozed_until IS NULL OR q.snoozed_until<=?)"
            )
            proposal_domain = " AND json_extract(p.facts_json,'$[0].category')=?" if requested_domain else ""
            if after_key is None:
                after_sql, after_params = "", []
            else:
                after_sql = " AND (?,20,p.created_at,'memory_proposal',p.id)>(?,?,?,?,?)"
                after_params = [rank, *after_key]
            proposal_params = [timestamp]
            if requested_domain:
                proposal_params.append(requested_domain)
            proposal_params.extend(after_params)
            proposal_params.append(fetch_size)
            proposal_rows = [dict(row) for row in connection.execute(
                f"""SELECT p.id,p.entry_id,p.facts_json,p.created_at,p.created_at AS sort_time,
                           20 AS urgent_priority,e.title AS entry_title,q.snoozed_until,q.is_deferred,
                           COALESCE(q.presentation_count,0) AS presentation_count
                    FROM memory_proposals p JOIN entries e ON e.id=p.entry_id
                    LEFT JOIN memory_proposal_queue_state q ON q.proposal_id=p.id
                    WHERE p.status='pending' AND {proposal_condition}{proposal_domain}{after_sql}
                    ORDER BY p.created_at,p.id LIMIT ?""", proposal_params,
            )]
            for proposal_row in proposal_rows:
                item = _proposal_projection(proposal_row)
                item["_priority"] = 20
                item["_sort_time"] = str(proposal_row.get("sort_time") or "")
                projected.append(item)
        projected.sort(key=lambda item: (
            bucket_rank[str(item["bucket"])], int(item["_priority"]),
            str(item["_sort_time"]), str(item["item_kind"]), int(item["id"]),
        ))
        keyed = [(
            bucket_rank[str(item["bucket"])], int(item["_priority"]), str(item["_sort_time"]),
            str(item["item_kind"]), int(item["id"]), item,
        ) for item in projected]
        page_entries = keyed[:limit]
        page = [entry[5] for entry in page_entries]
        for item in page:
            item.pop("_priority", None)
            item.pop("_sort_time", None)
        next_cursor = _review_cursor_encode(page_entries[-1][:5]) if len(keyed) > limit and page_entries else None
    return {"items": page, "counts": counts, "counts_exact": True, "next_cursor": next_cursor, "bucket": bucket}


def review_inbox_detail(fact_id: int, include_sensitive: bool = False) -> dict[str, object]:
    with db() as connection:
        row = connection.execute(
            """SELECT f.id,f.category,f.fact_type,f.summary,f.value_json,f.occurred_on,f.valid_from,f.valid_to,
                      f.extractor,f.extractor_model,f.prompt_version,f.extracted_at,
                      f.validation_status,f.retrieval_eligibility,
                      COALESCE(c.text,sc.text,'') AS evidence,d.title AS document_title,d.source_created_at,
                      (SELECT COUNT(*) FROM fact_evidence fe WHERE fe.fact_id=f.id) AS evidence_count,
                      r.state AS review_state
               FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
               LEFT JOIN documents d ON d.id=f.document_id
               LEFT JOIN chunks c ON c.id=f.chunk_id LEFT JOIN chunks sc ON sc.id=f.source_chunk_id
               WHERE f.id=? AND r.state IN ('pending','deferred')""", (fact_id,),
        ).fetchone()
    if not row:
        raise LookupError("Review item not found")
    item = dict(row)
    domain = canonical_domain(str(item.get("category") or "other"))
    sensitive = domain in {"finance", "relationship", "health"}
    technical_detail = {
        "fact_type": item.pop("fact_type", ""),
        "extractor": item.pop("extractor", ""),
        "extractor_model": item.pop("extractor_model", ""),
        "prompt_version": item.pop("prompt_version", ""),
        "validation_status": item.pop("validation_status", ""),
        "retrieval_eligibility": item.pop("retrieval_eligibility", ""),
        "extracted_at": item.pop("extracted_at", ""),
        "internal_id": int(item["id"]),
    }
    item.update({
        "item_kind": "fact", "category": domain, "domain_label": _digest_domain_label(domain),
        "sensitive": sensitive, "technical_detail": technical_detail,
    })
    if sensitive and not include_sensitive:
        item.update({
            "summary": SENSITIVE_REVIEW_SUMMARY, "value_json": "{}", "evidence": "",
            "document_title": "非表示", "source_created_at": "", "technical_detail": {},
        })
    return item


def review_proposal_detail(proposal_id: int, include_sensitive: bool = False) -> dict[str, object]:
    with db() as connection:
        row = connection.execute(
            """SELECT p.id,p.facts_json,p.policy,p.created_at,e.title AS entry_title,e.body AS entry_body
               FROM memory_proposals p JOIN entries e ON e.id=p.entry_id
               WHERE p.id=? AND p.status='pending'""", (proposal_id,),
        ).fetchone()
    if not row:
        raise LookupError("Review item not found")
    projected = _proposal_projection(dict(row))
    if projected["sensitive"] and not include_sensitive:
        return projected
    projected.update({"facts": json.loads(row["facts_json"] or "[]"), "source_preview": str(row["entry_body"] or "")[:1200], "policy": row["policy"]})
    return projected


def update_fact_review_state(fact_id: int, state: str, defer_for: str = "", note: str = "") -> dict[str, object]:
    """Apply only valid review transitions; finite snoozes keep pending state."""
    if state not in {"pending", "confirmed", "rejected", "deferred"}:
        raise ValueError("Invalid review state")
    if state == "pending" and defer_for not in {"", "one_day", "one_week"}:
        raise ValueError("pending accepts only one_day or one_week snooze")
    if state == "deferred" and defer_for != "indefinite":
        raise ValueError("deferred requires indefinite")
    if state in {"confirmed", "rejected"} and defer_for:
        raise ValueError("confirmed and rejected do not accept defer_for")
    with db() as connection:
        current = connection.execute(
            "SELECT state,reason,review_note,reviewed_at FROM fact_reviews WHERE fact_id=?", (fact_id,),
        ).fetchone()
        if not current:
            raise LookupError("Review item not found")
        queue = connection.execute("SELECT snoozed_until FROM fact_review_queue_state WHERE fact_id=?", (fact_id,)).fetchone()
        current_state = str(current["state"])
        if current_state in {"confirmed", "rejected"}:
            if state == current_state and not defer_for:
                return {"message": "Already reviewed", "state": current_state, "defer_for": None, "quality": None}
            raise ValueError("Completed review cannot be reopened or changed through this endpoint")
        if state == "pending" and not defer_for and current["state"] != "deferred" and not queue:
            raise ValueError("pending is only valid when resuming a deferred or snoozed item")
        if current_state == "deferred" and defer_for in {"one_day", "one_week"}:
            raise ValueError("A deferred review must be resumed before a finite snooze")
        if state in {"confirmed", "rejected", "deferred"} and current_state not in {"pending", "deferred"}:
            raise ValueError(f"Cannot transition {current['state']} to {state}")
        stored_state = state
        if state in {"confirmed", "rejected"}:
            connection.execute(
                "UPDATE fact_reviews SET state=?,review_note=?,reviewed_at=? WHERE fact_id=?",
                (state, str(note or ("ユーザー確認" if state == "confirmed" else "ユーザー却下"))[:1000], now(), fact_id),
            )
        elif state == "deferred":
            connection.execute("UPDATE fact_reviews SET state='deferred' WHERE fact_id=?", (fact_id,))
        elif state == "pending" and not defer_for and current_state == "deferred":
            connection.execute("UPDATE fact_reviews SET state='pending' WHERE fact_id=?", (fact_id,))
        # A finite snooze is queue metadata only: do not touch fact_reviews.
        if state == "pending" and defer_for:
            deadline = None
            if defer_for == "one_day": deadline = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            elif defer_for == "one_week": deadline = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
            connection.execute(
                """INSERT INTO fact_review_queue_state(fact_id,snoozed_until,updated_at)
                   VALUES(?,?,?) ON CONFLICT(fact_id) DO UPDATE SET
                   snoozed_until=excluded.snoozed_until,updated_at=excluded.updated_at""", (fact_id, deadline, now()),
            )
        elif state == "deferred":
            connection.execute(
                """INSERT INTO fact_review_queue_state(fact_id,snoozed_until,updated_at)
                   VALUES(?,NULL,?) ON CONFLICT(fact_id) DO UPDATE SET
                   snoozed_until=NULL,updated_at=excluded.updated_at""", (fact_id, now()),
            )
        else:
            connection.execute("DELETE FROM fact_review_queue_state WHERE fact_id=?", (fact_id,))
        if stored_state == "confirmed":
            sync_finance_transaction(connection, fact_id, confirmed=True)
        elif stored_state == "rejected":
            connection.execute(
                "UPDATE finance_transactions SET eligibility_state='excluded',eligibility_reason='fact_rejected',validated_at=? WHERE fact_id=?",
                (now(), fact_id),
            )
        # Snooze/defer is display control only. Re-running quality analysis at
        # that point could change validation or eligibility, which would make
        # a UI scheduling action alter the meaning of the Fact.
        quality = apply_memory_quality_to_fact(connection, fact_id, source="manual_review") if stored_state in {"confirmed", "rejected"} else None
    return {"message": "Reviewed", "state": stored_state, "defer_for": defer_for or None, "quality": quality}


def mark_review_item_presented(item_kind: str, item_id: int) -> int:
    """Increment one item only after the client confirms it entered the DOM."""
    timestamp = now()
    with db() as connection:
        if item_kind == "fact":
            exists = connection.execute(
                "SELECT 1 FROM fact_reviews WHERE fact_id=? AND state IN ('pending','deferred')", (item_id,),
            ).fetchone()
            if not exists: raise LookupError("Review item not found")
            connection.execute(
                """INSERT INTO fact_review_queue_state(fact_id,last_presented_at,presentation_count,updated_at)
                   VALUES(?,?,1,?) ON CONFLICT(fact_id) DO UPDATE SET
                   last_presented_at=excluded.last_presented_at,
                   presentation_count=fact_review_queue_state.presentation_count+1,
                   updated_at=excluded.updated_at""", (item_id, timestamp, timestamp),
            )
            return int(connection.execute(
                "SELECT presentation_count FROM fact_review_queue_state WHERE fact_id=?", (item_id,),
            ).fetchone()[0])
        if item_kind == "memory-proposal":
            exists = connection.execute(
                "SELECT 1 FROM memory_proposals WHERE id=? AND status='pending'", (item_id,),
            ).fetchone()
            if not exists: raise LookupError("Review item not found")
            connection.execute(
                """INSERT INTO memory_proposal_queue_state(proposal_id,last_presented_at,presentation_count,updated_at)
                   VALUES(?,?,1,?) ON CONFLICT(proposal_id) DO UPDATE SET
                   last_presented_at=excluded.last_presented_at,
                   presentation_count=memory_proposal_queue_state.presentation_count+1,
                   updated_at=excluded.updated_at""", (item_id, timestamp, timestamp),
            )
            return int(connection.execute(
                "SELECT presentation_count FROM memory_proposal_queue_state WHERE proposal_id=?", (item_id,),
            ).fetchone()[0])
    raise ValueError("Invalid review item kind")


def _action_is_snoozed(row: dict[str, object] | None) -> bool:
    if not row:
        return False
    return bool(int(row.get("keep_in_inbox") or 0) or _review_snooze_is_future(row.get("snoozed_until")))


def action_center_projection() -> dict[str, object]:
    """Return exactly one server-side next action and compact status counts."""
    inbox = review_inbox_projection(bucket="urgent", limit=1)
    with db() as connection:
        decisions = [dict(row) for row in connection.execute(
            """SELECT id,domain,title,question,decision_state,result,later_evaluation,updated_at,created_at
               FROM decisions ORDER BY COALESCE(updated_at,created_at) ASC,id ASC"""
        )]
        snoozes = {str(row["action_key"]): dict(row) for row in connection.execute("SELECT * FROM action_center_snoozes")}
        recent_changes = int(connection.execute(
            "SELECT COUNT(*) FROM memory_changes WHERE created_at>=?", ((datetime.now(timezone.utc)-timedelta(days=7)).isoformat(),)
        ).fetchone()[0])
        confirmed_facts = int(connection.execute(
            """SELECT COUNT(*) FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
               WHERE r.state='confirmed' AND f.status IN ('current','historical')
                 AND COALESCE(f.retrieval_eligibility,'pending')='eligible'"""
        ).fetchone()[0])

    candidates: list[tuple[int, str, dict[str, object]]] = []
    result_waiting = evaluation_waiting = 0
    decision_priority = {"executed": 2, "result": 3, "decided": 7, "candidate": 8, "considered": 8}
    for row in decisions:
        state = str(row.get("decision_state") or "").lower()
        if state == "executed" and not str(row.get("result") or "").strip():
            result_waiting += 1
            action_type, label, reason = "decision_result", "結果を記録する", "実行済みの判断に結果がまだ記録されていません"
        elif state == "result" and not str(row.get("later_evaluation") or "").strip():
            evaluation_waiting += 1
            action_type, label, reason = "decision_evaluation", "後日評価を記録する", "結果を残した判断の振り返りがまだです"
        elif state == "decided":
            action_type, label, reason = "decision_execute", "判断を確認する", "決めた内容の実行状況を確認できます"
        elif state in {"candidate", "considered"}:
            action_type, label, reason = "decision_choose", "判断を進める", "選択待ちの判断があります"
        else:
            continue
        action_key = f"decision:{int(row['id'])}:{action_type}"
        if _action_is_snoozed(snoozes.get(action_key)):
            continue
        domain = canonical_domain(str(row.get("domain") or "other"))
        title = _digest_safe_text(domain, row.get("title") or row.get("question"), f"{_digest_domain_label(domain)}の判断")
        candidates.append((decision_priority[state], str(row.get("updated_at") or row.get("created_at") or ""), {
            "key": action_key, "kind": action_type, "id": int(row["id"]), "title": title,
            "reason": reason, "action_label": label, "route": "decisions", "domain": domain,
            "can_snooze": True,
        }))

    urgent = inbox["items"]
    for priority, collection in ((4, urgent[:1]),):
        for item in collection:
            candidates.append((priority, str(item.get("source_created_at") or item.get("created_at") or ""), {
                "key": f"fact:{int(item['id'])}:review", "kind": "fact_review", "id": int(item["id"]),
                "title": str(item["summary"]), "reason": str(item["priority_reason"]),
                "action_label": "確認する", "route": "verify", "domain": str(item["category"]),
                "bucket": str(item["bucket"]), "can_snooze": True,
            }))
    if candidates:
        candidates.sort(key=lambda value: (value[0], value[1], int(value[2].get("id") or 0)))
        top_action = candidates[0][2]
    elif confirmed_facts:
        top_action = {
            "key": "consultation:start", "kind": "consultation", "title": "今の記憶を使って相談する",
            "reason": "確認済みの記憶を次の判断に活かせます", "action_label": "相談する",
            "route": "chat", "domain": "other", "can_snooze": False,
        }
    else:
        top_action = {
            "key": "record:start", "kind": "record", "title": "最初の記録を残す",
            "reason": "記録が増えると相談や判断の根拠として使えます", "action_label": "記録する",
            "route": "home", "domain": "other", "can_snooze": False,
        }
    return {
        "top_action": top_action,
        "status_counts": {
            "urgent_reviews": int(inbox["counts"]["urgent"]),
            "normal_reviews": int(inbox["counts"]["normal"]),
            "deferred_reviews": int(inbox["counts"]["deferred"]),
            "result_waiting": result_waiting,
            "evaluation_waiting": evaluation_waiting,
            "recent_changes": recent_changes,
        },
    }


def today_digest() -> dict[str, object]:
    """Return a bounded, evidence-backed daily overview without creating memory.

    The response intentionally contains only confirmed Facts, explicit Decision
    state, and recorded changes. It never treats a recommendation or inferred
    mood as a current personal fact.
    """
    with db() as connection:
        decisions = [dict(row) for row in connection.execute(
            """SELECT id,domain,title,question,decision_state,status,result,later_evaluation,updated_at,created_at
               FROM decisions ORDER BY COALESCE(updated_at,created_at) ASC,id ASC"""
        )]
        changes = [dict(row) for row in connection.execute(
            """SELECT mc.id,mc.fact_id,mc.change_type,mc.summary,mc.created_at,f.category,f.summary AS fact_summary
               FROM memory_changes mc LEFT JOIN facts f ON f.id=mc.fact_id
               ORDER BY mc.created_at DESC,mc.id DESC LIMIT 18"""
        )]
        remembered = [dict(row) for row in connection.execute(
            """SELECT f.id,f.category,f.summary,f.status,f.valid_from,f.effective_at,f.observed_at,f.created_at,
                      (SELECT COUNT(*) FROM fact_evidence fe WHERE fe.fact_id=f.id) AS evidence_count
               FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
               WHERE r.state='confirmed' AND COALESCE(f.retrieval_eligibility,'pending')='eligible'
                 AND f.status IN ('current','historical')
                 AND ((SELECT COUNT(*) FROM fact_evidence fe WHERE fe.fact_id=f.id) > 0 OR f.source_chunk_id IS NOT NULL)
               ORDER BY CASE f.status WHEN 'current' THEN 0 ELSE 1 END,
                        COALESCE(f.effective_at,f.observed_at,f.valid_from,f.created_at) DESC,f.id DESC LIMIT 12"""
        )]

    action_priority = {"executed": 0, "decided": 1, "candidate": 2, "considered": 2, "result": 3}
    action_copy = {
        "executed": ("結果を記録する", "結果待ち"),
        "decided": ("実行する", "実行待ち"),
        "candidate": ("判断を進める", "判断待ち"),
        "considered": ("判断を進める", "判断待ち"),
        "result": ("振り返る", "後日評価待ち"),
    }
    next_actions: list[dict[str, object]] = []
    for row in decisions:
        state = str(row.get("decision_state") or "").lower()
        if state == "result" and str(row.get("later_evaluation") or "").strip():
            continue
        if state not in action_priority:
            continue
        action, state_label = action_copy[state]
        title = _digest_safe_text(row.get("domain"), row.get("title") or row.get("question"), f"{_digest_domain_label(row.get('domain'))}の判断")
        next_actions.append({
            "kind": "decision", "id": row["id"], "title": title, "action": action, "state_label": state_label,
            "domain": canonical_domain(str(row.get("domain") or "other")), "updated_at": row.get("updated_at") or row.get("created_at"),
            "basis": [{"kind": "decision", "id": row["id"]}], "_priority": action_priority[state],
        })
    next_actions.sort(key=lambda item: (int(item["_priority"]), str(item.get("updated_at") or ""), int(item["id"])))
    for item in next_actions:
        item.pop("_priority", None)

    recent_changes: list[dict[str, object]] = []
    seen_change_facts: set[int] = set()
    for row in changes:
        fact_id = row.get("fact_id")
        if fact_id and int(fact_id) in seen_change_facts:
            continue
        if fact_id:
            seen_change_facts.add(int(fact_id))
        domain = canonical_domain(str(row.get("category") or "other"))
        domain_label = _digest_domain_label(domain)
        summary = _digest_safe_text(domain, row.get("fact_summary") or row.get("summary"), f"{domain_label}の記録が更新されました")
        if domain in {"finance", "relationship", "health"}:
            summary = f"{domain_label}の記録が更新されました"
        recent_changes.append({
            "kind": "fact_change", "id": row["id"], "fact_id": fact_id, "domain": domain,
            "text": summary, "change_type": row.get("change_type") or "updated", "occurred_at": row.get("created_at"),
            "basis": [{"kind": "fact", "id": fact_id}] if fact_id else [],
        })
        if len(recent_changes) == 3:
            break
    if len(recent_changes) < 3:
        used_decisions = {item["id"] for item in next_actions}
        for row in reversed(decisions):
            if row["id"] in used_decisions:
                continue
            state = str(row.get("decision_state") or "")
            if not state:
                continue
            domain = canonical_domain(str(row.get("domain") or "other"))
            recent_changes.append({
                "kind": "decision_change", "id": row["id"], "domain": domain,
                "text": f"{_digest_safe_text(domain, row.get('title') or row.get('question'), f'{_digest_domain_label(domain)}の判断')} が更新されました",
                "change_type": state, "occurred_at": row.get("updated_at") or row.get("created_at"),
                "basis": [{"kind": "decision", "id": row["id"]}],
            })
            if len(recent_changes) == 3:
                break

    # Rotate confirmed, evidence-backed memories by date so a static record
    # does not occupy the same spot forever while avoiding a random UI.
    remember: list[dict[str, object]] = []
    if remembered:
        rotation = datetime.now().date().toordinal() % len(remembered)
        for row in (remembered[rotation:] + remembered[:rotation]):
            domain = canonical_domain(str(row.get("category") or "other"))
            label = _digest_safe_text(domain, row.get("summary"), f"{_digest_domain_label(domain)}に関する確認済みの記録")
            if domain in {"finance", "relationship", "health"}:
                label = f"{_digest_domain_label(domain)}に関する確認済みの記録があります"
            remember.append({
                "kind": "fact", "id": row["id"], "domain": domain, "text": label,
                "occurred_at": row.get("effective_at") or row.get("observed_at") or row.get("valid_from") or row.get("created_at"),
                "basis": [{"kind": "fact", "id": row["id"]}],
            })
            if len(remember) == 2:
                break

    domains = [item["domain"] for item in recent_changes if item.get("domain") not in {"other", ""}]
    result_waiting = sum(1 for item in next_actions if item["state_label"] == "結果待ち")
    if result_waiting:
        headline = f"今週は{result_waiting}件の判断が結果待ちです"
        headline_basis = [item["basis"][0] for item in next_actions if item["state_label"] == "結果待ち"]
    elif len(set(domains)) >= 2:
        labels = [_digest_domain_label(domain) for domain in dict.fromkeys(domains)]
        headline = f"最近は{labels[0]}と{labels[1]}に関する記録が更新されています"
        headline_basis = [item["basis"][0] for item in recent_changes[:2] if item["basis"]]
    elif recent_changes:
        headline = "最近の記録に更新があります"
        headline_basis = [item["basis"][0] for item in recent_changes if item["basis"]]
    else:
        headline = "最近の大きな変化はまだありません"
        headline_basis = []

    available_domains = {canonical_domain(str(row.get("category") or "other")) for row in remembered}
    available_domains.update(canonical_domain(str(row.get("domain") or "other")) for row in decisions)
    prompt_catalog = (
        ("travel", "次の旅行候補を整理する"),
        ("housing", "住居更新前に希望条件を見直す"),
        ("finance", "最近の資産変化を振り返る"),
    )
    consultation_prompts = [
        {"domain": domain, "text": text, "basis": []}
        for domain, text in prompt_catalog if domain in available_domains
    ][:3]
    action_center = action_center_projection()
    return {
        "headline": {"text": headline, "basis": headline_basis[:3], "period": "recent"},
        "next_actions": next_actions[:3], "recent_changes": recent_changes[:3], "remember": remember[:2],
        "consultation_prompts": consultation_prompts,
        **action_center,
    }


# The timeline is a read-only projection.  It deliberately does not add a
# second "history" database: facts, decisions and execution events remain the
# authoritative records and this layer only gives them a shared, semantic
# event contract for Explore.
TIMELINE_KIND_LABELS = {
    "fact_started": "新しく記録",
    "fact_changed": "情報を更新",
    "preference_changed": "好みが変化",
    "plan_created": "計画を作成",
    "plan_changed": "計画を更新",
    "decision_created": "判断候補を追加",
    "decision_made": "判断",
    "executed": "実行",
    "result_recorded": "結果を記録",
    "evaluation_recorded": "後日評価",
    "travel_event": "旅行",
    "housing_event": "住居",
    "finance_snapshot": "資産",
    "relationship_event": "人間関係",
    "inference_confirmed": "確認済みの傾向",
}
TIMELINE_SENSITIVE_DOMAINS = {"finance", "relationship", "health"}
TIMELINE_KIND_FILTERS = {
    "memory": {"fact_started", "fact_changed", "finance_snapshot", "travel_event", "housing_event", "relationship_event"},
    "preference": {"preference_changed"},
    "plan": {"plan_created", "plan_changed"},
    "decision": {"decision_created", "decision_made"},
    "executed": {"executed"},
    "result": {"result_recorded"},
    "evaluation": {"evaluation_recorded"},
}


def _timeline_domain(value: object) -> str:
    return canonical_domain(str(value or "other"))


def _timeline_timestamp(*values: object) -> tuple[str | None, str]:
    """Pick content time before database insertion time, without inventing time.

    ISO dates and date-only strings sort correctly together.  ``created_at``
    is intentionally the final fallback and is exposed as record time to the
    client so it is not misrepresented as when the underlying event happened.
    """
    names = ("occurred_at", "occurred_on", "effective_at", "valid_from", "decided_on",
             "executed_at", "result_at", "evaluated_at", "source_created_at", "created_at")
    for name, value in zip(names, values):
        if value is not None and str(value).strip():
            return str(value).strip(), name
    return None, "unknown"


def _timeline_safe_text(domain: str, text: object, fallback: str, include_sensitive: bool) -> str:
    if domain in TIMELINE_SENSITIVE_DOMAINS and not include_sensitive:
        return fallback
    return str(text or fallback).strip()[:240]


def _timeline_event(*, event_id: str, event_kind: str, domain: object, occurred_at: str | None,
                    temporal_source: str, title: object, summary: object, basis: list[dict[str, object]],
                    related: list[dict[str, object]] | None = None, action: dict[str, object] | None = None,
                    sensitive: bool = False, detail: dict[str, object] | None = None) -> dict[str, object]:
    normalized_domain = _timeline_domain(domain)
    return {
        "id": event_id, "event_kind": event_kind, "domain": normalized_domain,
        "occurred_at": occurred_at, "temporal_source": temporal_source,
        "title": str(title or "記録"), "summary": str(summary or ""), "status": "historical",
        "sensitive": bool(sensitive), "basis": basis, "related": related or [], "action": action,
        "detail": detail or {},
    }


def _timeline_fact_kind(row: dict[str, object]) -> str:
    domain, fact_type = _timeline_domain(row.get("category")), str(row.get("fact_type") or "")
    if row.get("supersedes_fact_id"):
        return "fact_changed"
    if fact_type == "preference":
        return "preference_changed"
    if domain == "finance" and fact_type in {"asset_balance", "holding", "income"}:
        return "finance_snapshot"
    if domain == "travel":
        return "travel_event"
    if domain == "housing":
        return "housing_event"
    if domain == "relationship":
        return "relationship_event"
    return "fact_started"


def _timeline_cursor_encode(event: dict[str, object]) -> str:
    payload = f"{event.get('occurred_at') or ''}|{event.get('id') or ''}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _timeline_cursor_decode(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        occurred_at, event_id = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8").split("|", 1)
        return occurred_at, event_id
    except (ValueError, UnicodeDecodeError):
        return None


def _timeline_fact_value(row: dict[str, object] | None, include_sensitive: bool) -> str:
    if not row:
        return ""
    domain = _timeline_domain(row.get("category"))
    fallback = f"{_digest_domain_label(domain)}の情報"
    return _timeline_safe_text(domain, row.get("summary"), fallback, include_sensitive)


def timeline_projection(domain: str | None = None, kind: str | None = None, from_date: str | None = None,
                        to_date: str | None = None, limit: int = 30, cursor: str | None = None,
                        include_sensitive: bool = False) -> dict[str, object]:
    """Project confirmed personal history as bounded, de-duplicated events.

    Recommendations and simulations are intentionally absent.  Inferences are
    eligible only when explicitly marked ``confirmed``/``confirmed_pattern``;
    generated active inferences therefore never become a user's timeline by
    accident.
    """
    requested_domain = _timeline_domain(domain) if domain else ""
    requested_kinds = TIMELINE_KIND_FILTERS.get(str(kind or "").strip().lower())
    limit = max(1, min(100, int(limit)))
    events: list[dict[str, object]] = []
    with db() as connection:
        fact_rows = [dict(row) for row in connection.execute(
            """SELECT f.*,d.source_created_at,e.canonical_name AS subject
               FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
               JOIN documents d ON d.id=f.document_id
               LEFT JOIN entities e ON e.id=f.subject_entity_id
               WHERE r.state='confirmed' AND f.personal_relevance='personal'
                 AND COALESCE(f.retrieval_eligibility,'pending')='eligible'
                 AND f.status IN ('current','historical','superseded')
                 AND f.category NOT IN ('reference','scenario','simulation','what_if')
                 AND f.fact_type NOT IN ('scenario','simulation','what_if')
               ORDER BY COALESCE(f.effective_at,f.observed_at,f.valid_from,f.occurred_on,d.source_created_at,f.created_at) DESC,f.id DESC
               LIMIT 600"""
        )]
        fact_by_id = {int(row["id"]): row for row in fact_rows}
        for row in fact_rows:
            event_kind = _timeline_fact_kind(row)
            normalized_domain = _timeline_domain(row.get("category"))
            occurred_at, temporal_source = _timeline_timestamp(
                None, row.get("occurred_on"), row.get("effective_at"), row.get("valid_from"),
                None, None, None, None, row.get("source_created_at"), row.get("created_at"),
            )
            previous = fact_by_id.get(int(row["supersedes_fact_id"])) if row.get("supersedes_fact_id") else None
            sensitive = normalized_domain in TIMELINE_SENSITIVE_DOMAINS
            label = _digest_domain_label(normalized_domain)
            if event_kind == "fact_changed":
                title = f"{label}の情報を更新"
                summary = "情報が更新されました" if sensitive and not include_sensitive else _timeline_fact_value(row, include_sensitive)
            elif event_kind == "preference_changed":
                title, summary = "好みを更新", _timeline_fact_value(row, include_sensitive)
            elif event_kind in {"finance_snapshot", "travel_event", "housing_event", "relationship_event"}:
                title = f"{label}の記録を更新"
                summary = f"{label}情報を更新しました" if sensitive and not include_sensitive else _timeline_fact_value(row, include_sensitive)
            else:
                title, summary = f"{label}の記録を追加", _timeline_fact_value(row, include_sensitive)
            events.append(_timeline_event(
                event_id=f"fact-{row['id']}", event_kind=event_kind, domain=normalized_domain,
                occurred_at=occurred_at, temporal_source=temporal_source, title=title, summary=summary,
                sensitive=sensitive, basis=[{"kind": "fact", "id": row["id"]}],
                action={"type": "open_fact", "id": row["id"]},
                detail={"currentness": row.get("status"), "previous_value": _timeline_fact_value(previous, include_sensitive) if previous else None,
                        "new_value": _timeline_fact_value(row, include_sensitive), "source_chunk_id": row.get("source_chunk_id")},
            ))

        decision_rows = [dict(row) for row in connection.execute(
            """SELECT * FROM decisions ORDER BY COALESCE(decided_on,created_at) DESC,id DESC LIMIT 400"""
        )]
        for row in decision_rows:
            normalized_domain = _timeline_domain(row.get("domain"))
            sensitive = normalized_domain in TIMELINE_SENSITIVE_DOMAINS
            title = _timeline_safe_text(normalized_domain, row.get("title") or row.get("question"), f"{_digest_domain_label(normalized_domain)}の判断", include_sensitive)
            common = {"domain": normalized_domain, "sensitive": sensitive, "basis": [{"kind": "decision", "id": row["id"]}],
                      "action": {"type": "open_decision", "id": row["id"]}}
            created_at, created_source = _timeline_timestamp(None, None, None, None, None, None, None, None, None, row.get("created_at"))
            events.append(_timeline_event(event_id=f"decision-{row['id']}-created", event_kind="decision_created", occurred_at=created_at,
                                          temporal_source=created_source, title="判断候補を追加", summary=title, **common))
            state = str(row.get("decision_state") or "").lower()
            if state in {"decided", "executed", "result"}:
                made_at, made_source = _timeline_timestamp(None, None, None, None, row.get("decided_on"), None, None, None, None, row.get("created_at"))
                events.append(_timeline_event(event_id=f"decision-{row['id']}-made", event_kind="decision_made", occurred_at=made_at,
                                              temporal_source=made_source, title="判断を決定", summary=title, **common))
            if str(row.get("result") or "").strip():
                result_at, result_source = _timeline_timestamp(None, None, None, None, None, None, row.get("outcome_recorded_at"), None, None, row.get("updated_at") or row.get("created_at"))
                events.append(_timeline_event(event_id=f"decision-{row['id']}-result", event_kind="result_recorded", occurred_at=result_at,
                                              temporal_source=result_source, title="結果を記録", summary=_timeline_safe_text(normalized_domain, row.get("result"), f"{_digest_domain_label(normalized_domain)}の結果を記録しました", include_sensitive), **common))
            if str(row.get("later_evaluation") or "").strip():
                evaluated_at, evaluated_source = _timeline_timestamp(None, None, None, None, None, None, None, row.get("evaluation_recorded_at"), None, row.get("updated_at") or row.get("created_at"))
                events.append(_timeline_event(event_id=f"decision-{row['id']}-evaluation", event_kind="evaluation_recorded", occurred_at=evaluated_at,
                                              temporal_source=evaluated_source, title="後日評価を記録", summary=_timeline_safe_text(normalized_domain, row.get("later_evaluation"), f"{_digest_domain_label(normalized_domain)}の振り返りを記録しました", include_sensitive), **common))

        plan_rows = [dict(row) for row in connection.execute("SELECT * FROM plans ORDER BY created_at DESC,id DESC LIMIT 400")]
        for row in plan_rows:
            normalized_domain = _timeline_domain(row.get("domain"))
            sensitive = normalized_domain in TIMELINE_SENSITIVE_DOMAINS
            basis = [{"kind": "plan", "id": row["id"]}]
            related = [{"kind": "decision", "id": row["decision_id"]}] if row.get("decision_id") else []
            common = {"domain": normalized_domain, "sensitive": sensitive, "basis": basis, "related": related,
                      "action": {"type": "open_decision", "id": row["decision_id"]} if row.get("decision_id") else None}
            created_at, created_source = _timeline_timestamp(None, None, None, None, None, None, None, None, None, row.get("created_at"))
            plan_title = _timeline_safe_text(normalized_domain, row.get("title"), f"{_digest_domain_label(normalized_domain)}の計画", include_sensitive)
            events.append(_timeline_event(event_id=f"plan-{row['id']}-created", event_kind="plan_created", occurred_at=created_at,
                                          temporal_source=created_source, title="計画を作成", summary=plan_title, **common))
            if str(row.get("updated_at") or "") != str(row.get("created_at") or ""):
                updated_at, updated_source = _timeline_timestamp(None, None, None, None, None, None, None, None, None, row.get("updated_at"))
                events.append(_timeline_event(event_id=f"plan-{row['id']}-changed", event_kind="plan_changed", occurred_at=updated_at,
                                              temporal_source=updated_source, title="計画を更新", summary=plan_title, **common))

        execution_rows = [dict(row) for row in connection.execute(
            """SELECT e.*,d.domain,d.title FROM execution_events e JOIN decisions d ON d.id=e.decision_id
               ORDER BY COALESCE(e.occurred_at,e.created_at) DESC,e.id DESC LIMIT 500"""
        )]
        for row in execution_rows:
            normalized_domain = _timeline_domain(row.get("domain"))
            occurred_at, temporal_source = _timeline_timestamp(row.get("occurred_at"), None, None, None, None, row.get("occurred_at"), None, None, None, row.get("created_at"))
            events.append(_timeline_event(
                event_id=f"execution-{row['id']}", event_kind="executed", domain=normalized_domain,
                occurred_at=occurred_at, temporal_source=temporal_source, title="実行を記録",
                summary=_timeline_safe_text(normalized_domain, row.get("summary") or row.get("title"), f"{_digest_domain_label(normalized_domain)}を実行しました", include_sensitive),
                sensitive=normalized_domain in TIMELINE_SENSITIVE_DOMAINS,
                basis=[{"kind": "execution", "id": row["id"]}, {"kind": "decision", "id": row["decision_id"]}],
                related=[{"kind": "decision", "id": row["decision_id"]}], action={"type": "open_decision", "id": row["decision_id"]},
            ))

        inference_rows = [dict(row) for row in connection.execute(
            """SELECT * FROM personal_inferences
               WHERE status='active' AND inference_type IN ('confirmed','confirmed_pattern')
               ORDER BY last_evaluated_at DESC,id DESC LIMIT 120"""
        )]
        for row in inference_rows:
            normalized_domain = _timeline_domain(row.get("domain"))
            basis = [{"kind": "inference", "id": row["id"]}]
            try:
                basis.extend({"kind": "fact", "id": int(item)} for item in json.loads(row.get("source_fact_ids_json") or "[]")[:8])
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            occurred_at, temporal_source = _timeline_timestamp(None, None, None, None, None, None, None, None, row.get("last_evaluated_at"), row.get("created_at"))
            events.append(_timeline_event(event_id=f"inference-{row['id']}", event_kind="inference_confirmed", domain=normalized_domain,
                                          occurred_at=occurred_at, temporal_source=temporal_source, title="確認済みの傾向を更新",
                                          summary=_timeline_safe_text(normalized_domain, row.get("statement"), f"{_digest_domain_label(normalized_domain)}の確認済み傾向", include_sensitive),
                                          sensitive=normalized_domain in TIMELINE_SENSITIVE_DOMAINS, basis=basis))

    # One source event can be projected only once.  Fact-currentness rebuilds
    # and import retries otherwise easily create visually identical entries.
    unique: dict[tuple[str, str, str], dict[str, object]] = {}
    for event in events:
        dedupe_key = (str(event["id"]), str(event["event_kind"]), str(event.get("occurred_at") or ""))
        unique.setdefault(dedupe_key, event)
    events = list(unique.values())
    if requested_domain:
        events = [event for event in events if event["domain"] == requested_domain]
    if requested_kinds is not None:
        events = [event for event in events if event["event_kind"] in requested_kinds]
    if from_date:
        events = [event for event in events if event.get("occurred_at") and str(event["occurred_at"])[:10] >= from_date]
    if to_date:
        events = [event for event in events if event.get("occurred_at") and str(event["occurred_at"])[:10] <= to_date]
    events.sort(key=lambda item: (str(item.get("occurred_at") or ""), str(item["id"])), reverse=True)
    cursor_value = _timeline_cursor_decode(cursor)
    if cursor_value:
        events = [event for event in events if (str(event.get("occurred_at") or ""), str(event["id"])) < cursor_value]
    page = events[:limit]
    next_cursor = _timeline_cursor_encode(page[-1]) if len(events) > limit and page else None
    return {
        "events": page, "next_cursor": next_cursor,
        "filters": {"domains": sorted({event["domain"] for event in events}), "kinds": sorted({event["event_kind"] for event in events})},
    }


def timeline_event_detail(event_id: str, include_sensitive: bool = False) -> dict[str, object] | None:
    """Return one projected event.  No source row is ever mutated."""
    for event in timeline_projection(limit=100, include_sensitive=include_sensitive)["events"]:
        if event["id"] == event_id:
            return event
    return None


def configured_access_password() -> str:
    return os.environ.get("PERSONAL_OS_ACCESS_PASSWORD", "")


def access_auth_required(remote_address: str) -> bool:
    """Require auth for LAN clients; loopback remains convenient for local use."""
    forced = os.environ.get("PERSONAL_OS_REQUIRE_AUTH", "").lower() == "true"
    is_loopback = remote_address in {"127.0.0.1", "::1", "localhost"}
    return forced or not is_loopback


class Handler(BaseHTTPRequestHandler):
    server_version = "PersonalOS/0.1"

    def _request_id(self) -> str:
        value = getattr(self, "request_id", "") or self.headers.get("X-Request-ID", "").strip()
        if not value:
            value = f"srv-{uuid.uuid4().hex[:16]}"
        self.request_id = value[:120]
        return self.request_id

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{now()}][{self._request_id()}] {format % args}")

    def _session_id(self) -> str:
        cookies = self.headers.get("Cookie", "")
        for item in cookies.split(";"):
            key, _, value = item.strip().partition("=")
            if key == "personal_os_session":
                return value
        return ""

    def _session(self) -> dict[str, object] | None:
        session_id = self._session_id()
        if not session_id:
            return None
        with AUTH_SESSIONS_LOCK:
            session = AUTH_SESSIONS.get(session_id)
            if not session or float(session.get("expires_at", 0)) < time.time():
                AUTH_SESSIONS.pop(session_id, None)
                return None
            return dict(session)

    def _auth_status(self) -> dict[str, object]:
        password_configured = bool(configured_access_password())
        required = access_auth_required(self.client_address[0])
        session = self._session()
        return {"required": required, "password_configured": password_configured, "authenticated": bool(session),
                "csrf_token": session.get("csrf_token") if session else None}

    def _authorize(self, state_changing: bool = False) -> bool:
        if not access_auth_required(self.client_address[0]):
            return True
        if not configured_access_password():
            self.send_json({"error": "LAN access requires PERSONAL_OS_ACCESS_PASSWORD", "error_type": "authentication_error"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return False
        session = self._session()
        if not session:
            self.send_json({"error": "Authentication required", "auth_required": True, "error_type": "authentication_error"}, HTTPStatus.UNAUTHORIZED)
            return False
        if state_changing and self.headers.get("X-CSRF-Token", "") != session.get("csrf_token"):
            self.send_json({"error": "CSRF token required", "error_type": "csrf"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def _login(self, payload: dict[str, object]) -> None:
        password = str(payload.get("password") or "")
        configured = configured_access_password()
        if not configured:
            return self.send_json({"error": "Set PERSONAL_OS_ACCESS_PASSWORD before LAN login"}, HTTPStatus.SERVICE_UNAVAILABLE)
        if not secrets.compare_digest(password, configured):
            return self.send_json({"error": "Invalid password"}, HTTPStatus.UNAUTHORIZED)
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        with AUTH_SESSIONS_LOCK:
            AUTH_SESSIONS[session_id] = {"csrf_token": csrf_token, "expires_at": time.time() + AUTH_SESSION_SECONDS}
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Request-ID", self._request_id())
        self.send_header("Set-Cookie", f"personal_os_session={session_id}; HttpOnly; SameSite=Lax; Path=/; Max-Age={AUTH_SESSION_SECONDS}")
        body = json_bytes({"authenticated": True, "csrf_token": csrf_token, "expires_in": AUTH_SESSION_SECONDS, "request_id": self._request_id()})
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def allowed_cors_origin(self) -> str | None:
        origin = self.headers.get("Origin", "").strip()
        if not origin:
            return None
        parsed = urlparse(origin)
        request_host = self.headers.get("Host", "").lower()
        if parsed.scheme in {"http", "https"} and parsed.netloc.lower() == request_host:
            return origin
        configured = {
            item.strip() for item in setting("allowed_origins", "").split(",") if item.strip()
        }
        return origin if origin in configured else None

    def send_json(self, payload: object, status: int = 200, *, no_store: bool = False) -> None:
        request_id = self._request_id()
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.setdefault("request_id", request_id)
            if status >= 400:
                if status == 403 and "csrf" in str(payload.get("error", "")).lower():
                    default_error_type = "csrf"
                else:
                    default_error_type = {401: "authentication_error", 408: "timeout", 504: "timeout"}.get(status, "http_error")
                payload.setdefault("error_type", default_error_type)
        content = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Request-ID", request_id)
        if no_store:
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Pragma", "no-cache")
        allowed_origin = self.allowed_cors_origin()
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(content)

    def send_html(self) -> None:
        content = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Request-ID", self._request_id())
        self.send_header("X-Personal-OS-Version", BACKEND_VERSION)
        self.end_headers()
        self.wfile.write(content)

    def send_static(self, relative_path: str, content_type: str) -> None:
        content = (ROOT / "static" / relative_path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Request-ID", self._request_id())
        self.send_header("X-Personal-OS-Version", BACKEND_VERSION)
        self.end_headers()
        self.wfile.write(content)

    def send_attachment_image(self, attachment_id: int) -> None:
        with db() as connection:
            attachment = connection.execute(
                "SELECT storage_path,mime_type FROM attachments WHERE id=?", (attachment_id,)
            ).fetchone()
        if not attachment:
            return self.send_json({"error": "Attachment not found"}, 404)
        try:
            target = (ROOT / attachment["storage_path"]).resolve()
            target.relative_to(ATTACHMENT_DIR.resolve())
        except (OSError, ValueError):
            return self.send_json({"error": "Invalid attachment path"}, 403)
        if not target.is_file():
            return self.send_json({"error": "Attachment file not found"}, 404)
        content = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", attachment["mime_type"])
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin", "").strip()
        allowed_origin = self.allowed_cors_origin()
        if origin and not allowed_origin:
            self.send_response(HTTPStatus.FORBIDDEN)
            self.end_headers()
            return
        self.send_response(204)
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-File-Name, X-Request-ID, Idempotency-Key, X-CSRF-Token")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/auth/status":
            return self.send_json(self._auth_status())
        if path.startswith("/api/") and not self._authorize(False):
            return
        if path == "/":
            return self.send_html()
        static_files = {
            "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
            "/service-worker.js": ("service-worker.js", "application/javascript; charset=utf-8"),
            "/icon.svg": ("icon.svg", "image/svg+xml"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/api-client.js": ("api-client.js", "application/javascript; charset=utf-8"),
            "/app.js": ("app.js", "application/javascript; charset=utf-8"),
            "/visualization.js": ("visualization.js", "application/javascript; charset=utf-8"),
            "/draft-store.js": ("draft-store.js", "application/javascript; charset=utf-8"),
            "/daily-ux.js": ("daily-ux.js", "application/javascript; charset=utf-8"),
            "/action-center.js": ("action-center.js", "application/javascript; charset=utf-8"),
        }
        if path in static_files:
            filename, content_type = static_files[path]
            return self.send_static(filename, content_type)
        if path == "/api/attachments":
            with db() as connection:
                attachments = [dict(row) for row in connection.execute(
                    """SELECT a.id,a.entry_id,a.original_name,a.mime_type,a.byte_size,a.created_at,e.title,e.body AS context
                       FROM attachments a JOIN entries e ON e.id=a.entry_id
                       ORDER BY a.created_at DESC,a.id DESC LIMIT 100"""
                )]
                for attachment in attachments:
                    attachment["facts"] = [dict(row) for row in connection.execute(
                        """SELECT id,category,fact_type,summary,confidence FROM facts
                           WHERE source_attachment_id=? ORDER BY id""", (attachment["id"],)
                    )]
                    proposal = connection.execute(
                        """SELECT facts_json,policy FROM memory_proposals
                           WHERE entry_id=? AND status='pending' ORDER BY id DESC LIMIT 1""", (attachment["entry_id"],)
                    ).fetchone()
                    attachment["proposal"] = {
                        "facts": json.loads(proposal["facts_json"]), "policy": proposal["policy"]
                    } if proposal else None
                    derivative = connection.execute(
                        """SELECT engine,content,confidence,metadata_json FROM attachment_derivatives
                           WHERE attachment_id=? AND derivative_kind='ocr'
                           ORDER BY id DESC LIMIT 1""",
                        (attachment["id"],),
                    ).fetchone()
                    attachment["ocr"] = dict(derivative) if derivative else None
            return self.send_json({"items": attachments})
        image_match = re.fullmatch(r"/api/attachments/(\d+)/image", path)
        if image_match:
            return self.send_attachment_image(int(image_match.group(1)))
        if path == "/api/entries":
            query = parse_qs(urlparse(self.path).query)
            search = query.get("q", [""])[0].strip()
            status = query.get("status", [""])[0].strip()
            sql = "SELECT * FROM entries WHERE 1=1"
            parameters: list[str] = []
            if search:
                sql += " AND (title LIKE ? OR body LIKE ? OR tags LIKE ?)"
                parameters.extend([f"%{search}%"] * 3)
            if status:
                sql += " AND status = ?"
                parameters.append(status)
            sql += " ORDER BY created_at DESC LIMIT 200"
            with db() as connection:
                rows = [dict(row) for row in connection.execute(sql, parameters)]
            return self.send_json(rows)
        if path == "/api/stats":
            with db() as connection:
                rows = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM entries GROUP BY status"
                ).fetchall()
            counts = {row["status"]: row["count"] for row in rows}
            return self.send_json({"inbox": counts.get("inbox", 0), "notes": counts.get("note", 0), "tasks": counts.get("task", 0)})
        if path == "/api/question-answers":
            with db() as connection:
                rows = [dict(row) for row in connection.execute("SELECT * FROM question_answers ORDER BY created_at DESC LIMIT 100")]
            return self.send_json(rows)
        if path == "/api/checkins":
            with db() as connection:
                rows = [dict(row) for row in connection.execute("SELECT * FROM checkins ORDER BY created_at DESC LIMIT 30")]
            return self.send_json(rows)
        if path == "/api/tasks":
            organize_tasks()
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    """SELECT entries.*, task_plans.area, task_plans.urgency, task_plans.next_action
                       FROM entries JOIN task_plans ON entries.id=task_plans.entry_id
                       WHERE entries.kind='task' AND entries.status != 'done'
                       ORDER BY CASE task_plans.urgency WHEN '高' THEN 1 WHEN '中' THEN 2 ELSE 3 END, entries.created_at DESC"""
                )]
            return self.send_json(rows)
        if path == "/api/review":
            since = (datetime.now(timezone.utc).astimezone() - timedelta(days=7)).isoformat(timespec="seconds")
            with db() as connection:
                created = connection.execute("SELECT COUNT(*) FROM entries WHERE created_at >= ?", (since,)).fetchone()[0]
                completed = connection.execute("SELECT COUNT(*) FROM entries WHERE kind='task' AND status='done' AND updated_at >= ?", (since,)).fetchone()[0]
                checkins = [dict(row) for row in connection.execute("SELECT * FROM checkins WHERE created_at >= ? ORDER BY created_at DESC", (since,))]
                open_tasks = [dict(row) for row in connection.execute("SELECT title, tags FROM entries WHERE kind='task' AND status != 'done' ORDER BY created_at DESC LIMIT 5")]
            moods: dict[str, int] = {}
            for item in checkins:
                moods[item["mood"]] = moods.get(item["mood"], 0) + 1
            return self.send_json({"created": created, "completed": completed, "checkins": len(checkins), "moods": moods, "open_tasks": open_tasks})
        if path == "/api/structured-memories":
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    """SELECT f.id,f.category,f.fact_type AS type,f.occurred_on,f.summary,f.value_json,
                              f.fact_key,f.status,f.confidence,f.extractor,f.extractor_model,f.prompt_version,f.extracted_at
                       FROM facts f LEFT JOIN fact_reviews r ON r.fact_id=f.id
                       WHERE COALESCE(r.state,'pending') != 'rejected'
                       ORDER BY COALESCE(f.occurred_on,f.created_at) DESC LIMIT 200"""
                )]
            return self.send_json(rows)
        if path == "/api/action-center":
            return self.send_json(action_center_projection())
        review_detail_match = re.fullmatch(r"/api/review-inbox/(\d+)/detail", path)
        if review_detail_match:
            query = parse_qs(urlparse(self.path).query)
            try:
                detail = review_inbox_detail(
                    int(review_detail_match.group(1)),
                    str(query.get("include_sensitive", ["false"])[0]).lower() == "true",
                )
                return self.send_json(detail, no_store=True)
            except LookupError as error:
                return self.send_json({"error": str(error)}, 404, no_store=True)
        proposal_detail_match = re.fullmatch(r"/api/review-inbox/proposals/(\d+)/detail", path)
        if proposal_detail_match:
            query = parse_qs(urlparse(self.path).query)
            try:
                detail = review_proposal_detail(
                    int(proposal_detail_match.group(1)),
                    str(query.get("include_sensitive", ["false"])[0]).lower() == "true",
                )
                return self.send_json(detail, no_store=True)
            except LookupError as error:
                return self.send_json({"error": str(error)}, 404, no_store=True)
        if path == "/api/review-inbox":
            query = parse_qs(urlparse(self.path).query)
            try:
                return self.send_json(review_inbox_projection(
                    bucket=str(query.get("bucket", ["urgent"])[0]),
                    domain=str(query.get("domain", [""])[0]),
                    limit=int(query.get("limit", [10])[0]),
                    cursor=query.get("cursor", [None])[0],
                ))
            except (TypeError, ValueError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/facts/review":
            # Legacy route kept for compatibility, but it now uses the same
            # deterministic and masked projection as Review Inbox.
            return self.send_json(review_inbox_projection("all", limit=10)["items"])
        if path == "/api/facts/review-summary":
            with db() as connection:
                counts = {row["state"]: row["count"] for row in connection.execute(
                    "SELECT state,COUNT(*) AS count FROM fact_reviews GROUP BY state"
                )}
                # Do not call every high-confidence Fact "low risk": evidence
                # and transaction validation decide whether it is actually
                # resolvable without a person.  This is intentionally the
                # same decision path used by POST /api/facts/auto-resolve.
                pending_rows = connection.execute(
                    """SELECT f.id,f.category,f.fact_type,f.confidence,f.summary,
                              f.value_json,c.text AS evidence
                       FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
                       LEFT JOIN chunks c ON c.id=f.source_chunk_id
                       WHERE r.state='pending'"""
                ).fetchall()
                auto_resolvable = 0
                for row in pending_rows:
                    try:
                        value = json.loads(row["value_json"] or "{}")
                    except json.JSONDecodeError:
                        value = {}
                    fact = {"category": row["category"], "type": row["fact_type"],
                            "summary": row["summary"], "asset": value.get("asset"),
                            "amount": value.get("amount"), "details": value.get("details", {})}
                    validation = None
                    if row["category"] == "finance" and row["fact_type"] == "transaction":
                        validation = validate_transaction_candidate({
                            "category": row["category"], "fact_type": row["fact_type"],
                            "summary": row["summary"], "value_json": row["value_json"],
                            "confidence": row["confidence"], "document_title": "",
                        }, row["evidence"] or "")
                    state, _ = fact_review_decision(
                        fact, float(row["confidence"] or 0),
                        evidence_text=row["evidence"] or "", transaction_validation=validation,
                    )
                    if state in {"confirmed", "rejected"}:
                        auto_resolvable += 1
            return self.send_json({"confirmed": counts.get("confirmed", 0), "pending": counts.get("pending", 0),
                                   "deferred": counts.get("deferred", 0), "rejected": counts.get("rejected", 0),
                                   "auto_resolvable_pending": auto_resolvable,
                                   # Backward-compatible alias for older UI clients.
                                   "low_risk_pending": auto_resolvable,
                                   "threshold": AUTO_CONFIRM_CONFIDENCE_THRESHOLD})
        if path == "/api/current-truth":
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    """SELECT f.id, f.category, f.fact_type, f.fact_key, f.occurred_on, f.effective_at, f.observed_at, f.temporal_source, f.valid_from, f.valid_to, f.confidence,
                              f.subject_scope, f.resolved_entity_type, f.retrieval_eligibility, f.truth_confidence,
                              f.value_json, f.summary, f.extractor, f.extractor_model, f.prompt_version, f.extracted_at,
                              (SELECT COUNT(*) FROM fact_evidence fe WHERE fe.fact_id=f.id) AS evidence_count,
                              e.canonical_name AS subject, COALESCE(e.entity_type,f.resolved_entity_type) AS entity_type, d.source_created_at
                       FROM facts f
                       LEFT JOIN entities e ON e.id=f.subject_entity_id
                       JOIN fact_reviews r ON r.fact_id=f.id
                       JOIN documents d ON d.id=f.document_id
                       WHERE f.status='current' AND r.state='confirmed'
                         AND COALESCE(f.retrieval_eligibility,'pending')='eligible'
                       ORDER BY f.category, COALESCE(f.valid_from, f.created_at) DESC LIMIT 100"""
                )]
            return self.send_json(rows)
        if path == "/api/timeline":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = max(1, min(100, int(query.get("limit", [30])[0])))
            except ValueError:
                limit = 30
            return self.send_json(timeline_projection(
                domain=query.get("domain", [None])[0] or None,
                kind=query.get("kind", [None])[0] or None,
                from_date=query.get("from", [None])[0] or None,
                to_date=query.get("to", [None])[0] or None,
                limit=limit, cursor=query.get("cursor", [None])[0] or None,
                include_sensitive=query.get("include_sensitive", ["false"])[0].lower() == "true",
            ))
        timeline_match = re.fullmatch(r"/api/timeline/([A-Za-z0-9_-]+)", path)
        if timeline_match:
            query = parse_qs(urlparse(self.path).query)
            detail = timeline_event_detail(
                timeline_match.group(1), query.get("include_sensitive", ["false"])[0].lower() == "true"
            )
            return self.send_json(detail or {"error": "Not found"}, HTTPStatus.OK if detail else HTTPStatus.NOT_FOUND)
        if path == "/api/benchmarks":
            query = parse_qs(urlparse(self.path).query)
            return self.send_json(benchmark_projection(query.get("metric_key", [None])[0]))
        if path == "/api/benchmarks/compatibility-audit":
            return self.send_json(benchmark_compatibility_audit())
        if path == "/api/personal-space":
            query = parse_qs(urlparse(self.path).query)
            include_sensitive = query.get("include_sensitive", ["false"])[0].lower() == "true"
            try:
                limit = max(20, min(300, int(query.get("limit", [180])[0])))
            except ValueError:
                limit = 180
            return self.send_json(personal_space_projection(include_sensitive, limit))
        if path.startswith("/api/personal-space/nodes/"):
            query = parse_qs(urlparse(self.path).query)
            parts = path.rstrip("/").split("/")
            try:
                detail = personal_space_node_detail(parts[-2], int(parts[-1]), query.get("include_sensitive", ["false"])[0].lower() == "true")
            except (ValueError, IndexError):
                detail = None
            return self.send_json(detail or {"error": "Not found"}, HTTPStatus.OK if detail else HTTPStatus.NOT_FOUND)
        if path == "/api/search":
            query = parse_qs(urlparse(self.path).query)
            message = query.get("q", [""])[0].strip()
            if not message:
                return self.send_json({"current": [], "decisions": [], "fts": [], "semantic": [], "raw": []})
            context = retrieval_context(message)
            ordered = (
                context["current"] + context["decisions"] + context["history"]
                + context["profile"] + context["raw"]
            )
            return self.send_json({"query": message, "ordered": ordered, **context})
        if path == "/api/facts/category-audit":
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    """SELECT f.id,f.category,COALESCE(mc.label,f.category) AS category_label,f.fact_type,f.status,
                              f.summary,f.confidence,r.state AS review_state,d.title AS document_title
                       FROM facts f JOIN fact_reviews r ON r.fact_id=f.id
                       JOIN documents d ON d.id=f.document_id
                       LEFT JOIN memory_categories mc ON mc.slug=f.category
                       WHERE r.state IN ('pending','deferred') AND f.category != 'reference'
                       ORDER BY CASE f.status WHEN 'current' THEN 0 ELSE 1 END,f.created_at DESC LIMIT 30"""
                )]
            return self.send_json(rows)
        if path == "/api/memory-quality":
            with db() as connection:
                counts = {row["retrieval_eligibility"]: row["count"] for row in connection.execute(
                    "SELECT COALESCE(retrieval_eligibility,'pending') AS retrieval_eligibility,COUNT(*) AS count FROM facts GROUP BY retrieval_eligibility"
                )}
                validations = {row["validation_status"]: row["count"] for row in connection.execute(
                    "SELECT COALESCE(validation_status,'pending') AS validation_status,COUNT(*) AS count FROM facts GROUP BY validation_status"
                )}
                entity_types = {row["entity_type"]: row["count"] for row in connection.execute(
                    "SELECT COALESCE(e.entity_type,f.resolved_entity_type,'unknown') AS entity_type,COUNT(*) AS count FROM facts f LEFT JOIN entities e ON e.id=f.subject_entity_id GROUP BY COALESCE(e.entity_type,f.resolved_entity_type,'unknown')"
                )}
                personal_relevance = {row["personal_relevance"]: row["count"] for row in connection.execute(
                    "SELECT COALESCE(personal_relevance,'unknown') AS personal_relevance,COUNT(*) AS count FROM facts GROUP BY COALESCE(personal_relevance,'unknown')"
                )}
                corrections = [dict(row) for row in connection.execute(
                    "SELECT * FROM memory_corrections ORDER BY created_at DESC,id DESC LIMIT 50"
                )]
                non_people = [dict(row) for row in connection.execute(
                    """SELECT f.id,f.category,f.summary,f.retrieval_eligibility,f.validation_status,
                              COALESCE(e.entity_type,f.resolved_entity_type,'unknown') AS entity_type,e.canonical_name,r.state AS review_state
                       FROM facts f LEFT JOIN entities e ON e.id=f.subject_entity_id
                       LEFT JOIN fact_reviews r ON r.fact_id=f.id
                       WHERE f.category='relationship' AND COALESCE(e.entity_type,f.resolved_entity_type,'unknown')!='person'
                       ORDER BY f.id DESC LIMIT 100"""
                )]
                last_repair = connection.execute(
                    "SELECT * FROM repair_jobs ORDER BY id DESC LIMIT 1"
                ).fetchone()
            return self.send_json({"counts": counts, "validations": validations, "entity_types": entity_types,
                                   "personal_relevance": personal_relevance,
                                   "corrections": corrections, "non_people_relationships": non_people,
                                   "last_repair": dict(last_repair) if last_repair else None,
                                   "quality_version": MEMORY_QUALITY_VERSION})
        if path == "/api/facts/anomalies":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = max(1, min(200, int(query.get("limit", [50])[0])))
            except ValueError:
                limit = 50
            return self.send_json(detect_fact_anomalies(limit))
        if path == "/api/memory-changes":
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    """SELECT c.*, f.category, f.fact_type, f.summary AS fact_summary
                       FROM memory_changes c LEFT JOIN facts f ON f.id=c.fact_id
                       ORDER BY c.created_at DESC, c.id DESC LIMIT 50"""
                )]
            return self.send_json(rows)
        evidence_match = re.fullmatch(r"/api/facts/(\d+)/evidence", path)
        if evidence_match:
            fact_id = int(evidence_match.group(1))
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    """SELECT e.*,d.title AS document_title,c.text AS source_text,a.original_name
                       FROM fact_evidence e
                       LEFT JOIN chunks c ON c.id=e.source_chunk_id
                       LEFT JOIN documents d ON d.id=c.document_id
                       LEFT JOIN attachments a ON a.id=e.source_attachment_id
                       WHERE e.fact_id=? ORDER BY e.created_at DESC,e.id DESC""", (fact_id,)
                )]
            return self.send_json(rows)
        correction_match = re.fullmatch(r"/api/facts/(\d+)/corrections", path)
        if correction_match:
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    "SELECT * FROM memory_corrections WHERE fact_id=? ORDER BY created_at DESC,id DESC",
                    (int(correction_match.group(1)),),
                )]
            return self.send_json(rows)
        if path == "/api/recommendations":
            query = parse_qs(urlparse(self.path).query)
            items = recommendation_projection(query.get("domain", [None])[0])
            for item in items:
                cycle = cycle_snapshot(int(item["id"]))
                item["cycle_stage"] = cycle["cycle_stage"] if cycle else "recommended"
                item["available_actions"] = cycle["available_actions"] if cycle else []
            return self.send_json(items)
        if path == "/api/plans":
            query = parse_qs(urlparse(self.path).query)
            items = plan_projection(query.get("domain", [None])[0])
            for item in items:
                if item.get("source_recommendation_id"):
                    cycle = cycle_snapshot(int(item["source_recommendation_id"]))
                    if cycle:
                        item["cycle_stage"] = cycle["cycle_stage"]
                        item["available_actions"] = cycle["available_actions"]
            return self.send_json(items)
        if path == "/api/memory-proposals":
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    """SELECT p.*, e.title AS entry_title FROM memory_proposals p
                       JOIN entries e ON e.id=p.entry_id WHERE p.status='pending'
                       ORDER BY p.created_at DESC LIMIT 20"""
                )]
            return self.send_json(rows)
        replay_match = re.fullmatch(r"/api/decisions/(\d+)/replay", path)
        if replay_match:
            query = parse_qs(urlparse(self.path).query)
            include_sensitive = query.get("include_sensitive", ["false"])[0].lower() == "true"
            replay = decision_replay(int(replay_match.group(1)), include_sensitive=include_sensitive)
            return self.send_json(replay or {"error": "Not found"}, HTTPStatus.OK if replay else HTTPStatus.NOT_FOUND)
        if path == "/api/ux-feedback":
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    "SELECT id,screen,feedback_type,body,expected_behavior,severity,status,created_at,resolved_at FROM ux_feedback ORDER BY created_at DESC,id DESC LIMIT 100"
                )]
            return self.send_json(rows)
        if path == "/api/decisions":
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    "SELECT * FROM decisions ORDER BY COALESCE(decided_on, created_at) DESC, id DESC LIMIT 100"
                )]
                for row in rows:
                    row["execution_events"] = [dict(event) for event in connection.execute(
                        "SELECT * FROM execution_events WHERE decision_id=? ORDER BY COALESCE(occurred_at,created_at) DESC,id DESC LIMIT 30",
                        (row["id"],),
                    )]
            return self.send_json(rows)
        if path == "/api/memory-categories":
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    "SELECT * FROM memory_categories WHERE active=1 ORDER BY sort_order, label"
                )]
            return self.send_json(rows)
        if path == "/api/analysis-jobs":
            return self.send_json(analysis_job_summary())
        if path == "/api/import-jobs":
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    "SELECT * FROM import_jobs ORDER BY updated_at DESC,id DESC LIMIT 50"
                )]
            return self.send_json(rows)
        if path == "/api/repair-jobs":
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    "SELECT * FROM repair_jobs ORDER BY id DESC LIMIT 50"
                )]
            return self.send_json(rows)
        if path == "/api/inferences":
            query = parse_qs(urlparse(self.path).query)
            return self.send_json(personal_inference_projection(query.get("domain", [None])[0]))
        if path == "/api/personal-system-ideas":
            return self.send_json(personal_system_ideas())
        if path == "/api/embeddings":
            with db() as connection:
                counts = {row["state"]: row["count"] for row in connection.execute("SELECT state,COUNT(*) AS count FROM embedding_jobs GROUP BY state")}
                total = connection.execute("SELECT COUNT(*) FROM embedding_jobs").fetchone()[0]
            return self.send_json({"model": EMBEDDING_MODEL, "dimensions": EMBEDDING_DIMENSIONS, "pending": counts.get("pending", 0), "running": counts.get("running", 0), "completed": counts.get("completed", 0), "failed": counts.get("failed", 0), "total": total})
        if path == "/api/privacy/delete/preview":
            try:
                query = parse_qs(urlparse(self.path).query)
                return self.send_json(privacy_delete_preview(
                    str(query.get("target_type", [""])[0]),
                    int(query.get("target_id", ["0"])[0]),
                    str(query.get("delete_raw", ["false"])[0]).lower() == "true",
                ))
            except (ValueError, TypeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/privacy/audit":
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    "SELECT * FROM privacy_audit_log ORDER BY created_at DESC,id DESC LIMIT 100"
                )]
            for row in rows:
                try:
                    row["deleted_counts"] = json.loads(row.pop("deleted_counts_json") or "{}")
                except json.JSONDecodeError:
                    row["deleted_counts"] = {}
            return self.send_json(rows)
        if path == "/api/health":
            return self.send_json(operational_health())
        if path == "/api/diagnostics":
            with LLM_TRACE_LOCK:
                traces = list(LLM_TRACE_EVENTS[-20:])
            auth = self._auth_status()
            return self.send_json({
                "backend_version": BACKEND_VERSION,
                "environment": APP_ENV,
                "api_reachable": True,
                "auth": {"required": auth["required"], "authenticated": auth["authenticated"]},
                "session": bool(self._session()),
                "csrf": bool(auth.get("csrf_token")),
                "runtime": {"pid": os.getpid(), "port": self.server.server_port},
                "analysis": analysis_job_summary(),
                "provider_status": provider_status(),
                "llm_trace": traces,
            })
        if path == "/api/llm-traces":
            with LLM_TRACE_LOCK:
                return self.send_json({"items": list(LLM_TRACE_EVENTS[-50:])})
        if path == "/api/runtime":
            return self.send_json({
                **runtime_status(),
                "current_pid": os.getpid(),
                "current_port": self.server.server_port,
                "environment": APP_ENV,
                "database": str(DB_PATH),
            })
        if path == "/api/today/digest":
            return self.send_json(today_digest())
        if path == "/api/today":
            return self.send_json(today_snapshot())
        if path == "/api/integrations/moneyforward":
            return self.send_json(moneyforward_projection(MONEYFORWARD_DB_PATH, snapshot_directory=ATTACHMENT_DIR / "integrations" / "moneyforward"))
        cycle_match = re.fullmatch(r"/api/cycles/(\d+)", path)
        if cycle_match:
            cycle = cycle_snapshot(int(cycle_match.group(1)))
            return self.send_json(cycle or {"error": "Cycle not found"}, HTTPStatus.OK if cycle else HTTPStatus.NOT_FOUND)
        if path.startswith("/api/domains/"):
            domain = path.rsplit("/", 1)[-1]
            if domain not in {"money", "travel", "housing", "people"}:
                return self.send_json({"error": "Unknown domain"}, 404)
            return self.send_json(domain_projection(domain))
        if path == "/api/providers":
            return self.send_json(provider_status())
        if path == "/api/backup/verify":
            query = parse_qs(urlparse(self.path).query)
            relative_path = query.get("path", [""])[0]
            try:
                return self.send_json(verify_backup(relative_path))
            except (ValueError, FileNotFoundError, sqlite3.Error) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/backup":
            return self.send_json(backup_status())
        if path == "/api/insights":
            with db() as connection:
                totals = {
                    "memories": connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
                    "conversations": connection.execute("SELECT COUNT(*) FROM entries WHERE kind='conversation'").fetchone()[0],
                    "facts": connection.execute("SELECT COUNT(*) FROM facts WHERE id IN (SELECT fact_id FROM fact_reviews WHERE state != 'rejected')").fetchone()[0],
                    "checkins": connection.execute("SELECT COUNT(*) FROM checkins").fetchone()[0],
                    "imported": connection.execute("SELECT COUNT(*) FROM entries WHERE source='chatgpt-export'").fetchone()[0],
                    "analyzed": 0,
                }
                by_month = [dict(row) for row in connection.execute(
                    """SELECT substr(created_at, 1, 7) AS label, COUNT(*) AS count FROM entries
                       GROUP BY label ORDER BY label DESC LIMIT 12"""
                )][::-1]
                by_kind = [dict(row) for row in connection.execute(
                    "SELECT kind AS label, COUNT(*) AS count FROM entries GROUP BY kind ORDER BY count DESC"
                )]
                by_category = [dict(row) for row in connection.execute(
                    """SELECT f.category AS label, COUNT(*) AS count FROM facts f
                       LEFT JOIN fact_reviews r ON r.fact_id=f.id
                       WHERE COALESCE(r.state, 'pending') != 'rejected'
                         AND COALESCE(f.retrieval_eligibility,'pending')='eligible'
                       GROUP BY f.category ORDER BY count DESC"""
                )]
                asset_totals: dict[tuple[str, str], dict[str, object]] = {}
                for transaction in eligible_finance_transactions(connection):
                    key = (str(transaction.get("asset") or "対象なし"), str(transaction.get("currency") or "JPY"))
                    item = asset_totals.setdefault(key, {"asset": key[0], "currency": key[1], "amount": 0.0, "count": 0})
                    item["amount"] = float(item["amount"]) + float(transaction.get("normalized_amount") or 0)
                    item["count"] = int(item["count"]) + 1
                assets = sorted(asset_totals.values(), key=lambda item: float(item["amount"]), reverse=True)[:10]
            jobs = analysis_job_summary()
            totals["analyzed"] = jobs["completed"]
            return self.send_json({
                "totals": totals,
                "by_month": by_month,
                "by_kind": by_kind,
                "by_category": by_category,
                "assets": assets,
                "analysis": jobs,
            })
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/auth/login":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                return self._login(payload)
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "Invalid login payload"}, HTTPStatus.BAD_REQUEST)
        if path == "/api/auth/logout":
            session_id = self._session_id()
            with AUTH_SESSIONS_LOCK:
                AUTH_SESSIONS.pop(session_id, None)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", "personal_os_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
            body = json_bytes({"authenticated": False})
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if not self._authorize(True):
            return
        if path == "/api/integrations/moneyforward/import":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MONEYFORWARD_MAX_BYTES:
                    raise ValueError("更新JSONは8MB以内にしてください。")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                result = import_moneyforward_snapshot(ATTACHMENT_DIR / "integrations" / "moneyforward", payload)
                return self.send_json(result, HTTPStatus.CREATED)
            except (ValueError, TypeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except OSError:
                return self.send_json({"error": "更新データを保存できませんでした。空き容量と保存先を確認してください。"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        presented_match = re.fullmatch(r"/api/review-inbox/(fact|memory-proposal)/(\d+)/presented", path)
        if presented_match:
            item_kind, raw_id = presented_match.groups()
            item_id = int(raw_id)
            try:
                count = mark_review_item_presented(item_kind, item_id)
            except LookupError:
                return self.send_json({"error": "Review item not found"}, 404)
            return self.send_json({"item_kind": item_kind, "id": item_id, "presentation_count": int(count)})
        proposal_snooze_match = re.fullmatch(r"/api/review-inbox/memory-proposal/(\d+)/snooze", path)
        if proposal_snooze_match:
            try:
                proposal_id = int(proposal_snooze_match.group(1))
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                defer_for = str(payload.get("defer_for") or "").strip()
                if defer_for not in {"resume", "one_day", "one_week", "indefinite"}:
                    raise ValueError("Invalid proposal defer period")
                deadline = None
                if defer_for == "one_day":
                    deadline = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                elif defer_for == "one_week":
                    deadline = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
                with db() as connection:
                    exists = connection.execute(
                        "SELECT 1 FROM memory_proposals WHERE id=? AND status='pending'", (proposal_id,),
                    ).fetchone()
                    if not exists:
                        return self.send_json({"error": "Proposal not found"}, 404)
                    connection.execute(
                        """INSERT INTO memory_proposal_queue_state(proposal_id,snoozed_until,is_deferred,updated_at)
                           VALUES(?,?,?,?) ON CONFLICT(proposal_id) DO UPDATE SET
                           snoozed_until=excluded.snoozed_until,is_deferred=excluded.is_deferred,
                           updated_at=excluded.updated_at""",
                        (proposal_id, deadline, 1 if defer_for == "indefinite" else 0, now()),
                    )
                return self.send_json({"proposal_id": proposal_id, "defer_for": defer_for})
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/action-center/snooze":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                action_key = str(payload.get("action_key") or "").strip()[:200]
                defer_for = str(payload.get("defer_for") or "").strip()
                if not action_key or defer_for not in {"one_day", "one_week", "indefinite"}:
                    raise ValueError("Invalid action snooze")
                deadline = None
                if defer_for == "one_day":
                    deadline = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                elif defer_for == "one_week":
                    deadline = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
                with db() as connection:
                    connection.execute(
                        """INSERT INTO action_center_snoozes(action_key,snoozed_until,keep_in_inbox,updated_at)
                           VALUES(?,?,?,?) ON CONFLICT(action_key) DO UPDATE SET
                           snoozed_until=excluded.snoozed_until,keep_in_inbox=excluded.keep_in_inbox,
                           updated_at=excluded.updated_at""",
                        (action_key, deadline, 1 if defer_for == "indefinite" else 0, now()),
                    )
                return self.send_json({"message": "Deferred", "action_key": action_key, "snoozed_until": deadline})
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/benchmarks/preview":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                return self.send_json(validate_benchmark_bundle(payload.get("raw_json", payload)))
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/benchmarks/demo":
            try:
                return self.send_json(import_benchmark_bundle(demo_benchmark_bundle(), channel="demo"), HTTPStatus.CREATED)
            except (ValueError, TypeError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/benchmarks/import":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                return self.send_json(import_benchmark_bundle(payload.get("raw_json", payload), channel="chatgpt_copy" if payload.get("raw_json") else "manual"), HTTPStatus.CREATED)
            except (ValueError, TypeError, sqlite3.Error, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/facts/auto-resolve":
            changed = auto_confirm_low_risk_facts()
            quality = audit_memory_quality()
            return self.send_json({"resolved": changed, "quality": quality, "summary": "Evidence-based automatic review and memory quality audit completed"})
        if path == "/api/memory-quality/recheck":
            return self.send_json({"quality": audit_memory_quality(), "quality_version": MEMORY_QUALITY_VERSION})
        if path == "/api/memory-quality/repair":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                result = repair_memory_state(str(payload.get("reason", "manual")))
                return self.send_json({"result": result, "quality_version": MEMORY_QUALITY_VERSION})
            except (sqlite3.Error, OSError, ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/inferences/refresh":
            try:
                return self.send_json(refresh_personal_inferences())
            except (sqlite3.Error, OSError, ValueError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/memory-quality/resegment":
            try:
                result = prepare_conversation_reanalysis()
                return self.send_json({"result": result, "quality": audit_memory_quality(), "quality_version": MEMORY_QUALITY_VERSION})
            except (sqlite3.Error, OSError, ValueError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return self.send_json({"resolved": changed, "summary": "Evidenceに基づく自動判定を実行しました"})
        if path == "/api/ingest":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                text = str(payload.get("text", "")).strip()
                if not text:
                    raise ValueError("text is required")
                title = str(payload.get("title", "")).strip() or text[:60]
                timestamp = now()
                with db() as connection:
                    cursor = connection.execute(
                        """INSERT INTO entries(kind,title,body,source,tags,status,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        ("note", title, text, "ai-ingest", "unprocessed", "inbox", timestamp, timestamp),
                    )
                entry_id = cursor.lastrowid
                ensure_document_for_entry(entry_id)
                # Raw-first contract: return immediately and let the durable Job worker analyze it.
                queued = queue_analysis_jobs()
                return self.send_json(
                    {"entry_id": entry_id, "facts": [], "proposal": None, "queued": queued,
                     "message": "原文を保存しました。解析はバックグラウンドで実行します。"},
                    HTTPStatus.ACCEPTED,
                )
                try:
                    facts = extract_with_model(text)
                except ValueError as error:
                    return self.send_json({"entry_id": entry_id, "facts": [], "warning": str(error)}, HTTPStatus.ACCEPTED)
                if facts is None:
                    return self.send_json({"entry_id": entry_id, "facts": [], "warning": "抽出用LLMが未設定のため、原文のみ保存しました。"}, HTTPStatus.ACCEPTED)
                auto_facts = [fact for fact in facts if fact_policy(fact) != "exclude"]
                saved = save_structured_facts(entry_id, auto_facts)
                proposal = create_memory_proposal(entry_id, auto_facts)
                message = "自動保存しました" if not proposal else "重要な記憶は保存前に確認してください"
                return self.send_json({"entry_id": entry_id, "facts": saved, "proposal": proposal, "message": message}, HTTPStatus.CREATED)
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/analyze-imports":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                result = analyze_imported_conversations(int(payload.get("limit", analysis_batch_size())))
                return self.send_json(result)
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/analysis-control":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                action = str(payload.get("action", "")).lower()
                if action == "pause":
                    save_setting("analysis_paused", "true")
                    message = "Analysis will pause after the active request finishes."
                elif action == "resume":
                    save_setting("analysis_paused", "false")
                    message = "Analysis resumed."
                elif action == "unload":
                    save_setting("analysis_paused", "true")
                    released = unload_local_model()
                    message = "Local model unload requested; analysis is paused." if released else "Analysis is paused. Ollama was not reachable to unload."
                else:
                    raise ValueError("action must be pause, resume, or unload")
                return self.send_json({"message": message, "unloaded": action == "unload", "jobs": analysis_job_summary()})
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/import/chatgpt":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 2 * 1024 * 1024 * 1024:
                    raise ValueError("ZIPファイルは2GB以下にしてください")
                content_type = self.headers.get("Content-Type", "").lower()
                if content_type.startswith(("application/zip", "application/octet-stream")):
                    upload_path, digest = stream_request_file(self.rfile, length)
                    try:
                        created, skipped = import_chatgpt_export_path(
                            upload_path, digest, Path(self.headers.get("X-File-Name", "chatgpt-export.zip")).name
                        )
                    finally:
                        upload_path.unlink(missing_ok=True)
                else:
                    if length > 250 * 1024 * 1024:
                        raise ValueError("旧multipart取込は250MB以下です。画面を再読み込みして再試行してください")
                    content = multipart_file(self.rfile.read(length), self.headers.get("Content-Type", ""))
                    created, skipped = import_chatgpt_export(content)
                return self.send_json({"created": created, "skipped": skipped}, HTTPStatus.CREATED)
            except (ValueError, OSError, json.JSONDecodeError, zipfile.BadZipFile) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/import/screenshot":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 13 * 1024 * 1024:
                    raise ValueError("Screenshot upload must be 12 MB or smaller")
                content, filename, mime_type, fields = multipart_form_file(
                    self.rfile.read(length), self.headers.get("Content-Type", "")
                )
                context = fields.get("context", "").strip()[:1000]
                entry_id, attachment_id = store_screenshot(content, filename, mime_type, context)
                # Keep the original image and return before OCR/vision. A local-only
                # attachment Job can be retried without asking the user to upload again.
                with db() as connection:
                    document = connection.execute("SELECT id FROM documents WHERE legacy_entry_id=?", (entry_id,)).fetchone()
                    digest = hashlib.sha256(content).hexdigest()
                    connection.execute(
                        """INSERT OR IGNORE INTO analysis_jobs(
                             document_id,provider,model,prompt_version,content_hash,status,job_kind,
                             source_attachment_id,priority,priority_reason,requested_at,created_at,updated_at
                           ) VALUES(?,?,?,?,?,'pending','attachment',?,10,'新規画像',?,?,?)""",
                        (document["id"], "local", provider_model("local", "verification"),
                         "screenshot-vision-jp-v1", digest, attachment_id, now(), now(), now()),
                    )
                if analysis_paused():
                    return self.send_json(
                        {"entry_id": entry_id, "attachment_id": attachment_id, "facts": [],
                         "warning": "画像を保存し、ローカル解析待ちにしました。解析を再開すると処理します。"},
                        HTTPStatus.ACCEPTED,
                    )
                if not local_base_url():
                    return self.send_json(
                        {"entry_id": entry_id, "attachment_id": attachment_id, "facts": [],
                         "warning": "画像は保存しましたが、ローカルLLM URLが未設定のため解析待ちです。"}, HTTPStatus.ACCEPTED
                    )
                return self.send_json(
                    {"entry_id": entry_id, "attachment_id": attachment_id, "facts": [],
                     "message": "画像を保存しました。ローカル解析Jobを開始します。"}, HTTPStatus.ACCEPTED
                )
                auto_facts = [fact for fact in facts if fact_policy(fact) != "exclude"]
                saved = save_structured_facts(
                    entry_id, auto_facts, extractor="local", model=provider_model("local", "verification"),
                    prompt_version="screenshot-vision-jp-v1", source_attachment_id=attachment_id,
                )
                proposal = create_memory_proposal(entry_id, auto_facts)
                return self.send_json(
                    {"entry_id": entry_id, "attachment_id": attachment_id, "facts": saved, "proposal": proposal,
                     "message": "Screenshot analyzed locally. Important facts may need confirmation."},
                    HTTPStatus.CREATED,
                )
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/tasks/organize":
            return self.send_json({"count": organize_tasks(), "message": "タスクを整理しました"})
        if path == "/api/chat":
            try:
                request_id = self._request_id()
                record_llm_trace("api_received", request_id=request_id)
                length = int(self.headers.get("Content-Length", "0"))
                message = str(json.loads(self.rfile.read(length).decode("utf-8")).get("message", "")).strip()
                if not message:
                    raise ValueError("message is required")
                prioritized_jobs = prioritize_analysis_for_query(message)
                memory_groups = retrieval_context(message)
                memories = (
                    memory_groups["current"] + memory_groups["decisions"] + memory_groups["history"]
                    + memory_groups["profile"] + memory_groups["raw"]
                )
                record_llm_trace("context_built", provider=selected_provider("chat"),
                                 model=provider_model(selected_provider("chat"), "chat"), request_id=request_id)
                answer = ask_model(message, memories, request_id)
                if answer is None:
                    answer = "関連する記憶を見つけました。APIキーを設定すると、この記憶を根拠に会話形式で回答します。"
                response_type = consultation_response_type(message)
                candidate = None
                if response_type in {"recommendation", "planning"}:
                    draft = build_local_recommendation(consultation_domain(message), message)
                    candidate = {
                        "consultation_id": request_id,
                        "candidate_id": f"candidate-{uuid.uuid4().hex[:12]}",
                        "original_question": message,
                        "context_reference": {"fact_ids": draft["source_fact_ids"], "decision_ids": draft["source_decision_ids"], "evidence_ids": draft["source_evidence_ids"]},
                        "title": draft["title"],
                        "summary": draft["rationale"],
                        "options": draft["options"],
                        "tradeoffs": draft["tradeoffs"],
                        "plan_steps": draft["plan_steps"],
                        "personal_context_used": draft["personal_context_used"],
                        "source_fact_ids": draft["source_fact_ids"],
                        "source_decision_ids": draft["source_decision_ids"],
                        "source_evidence_ids": draft["source_evidence_ids"],
                        "domain": draft["domain"],
                    }
                record_llm_trace("response_parsed", provider=selected_provider("chat"),
                                 model=provider_model(selected_provider("chat"), "chat"), request_id=request_id)
                if E2E_CHAT_DELAY_SECONDS:
                    time.sleep(E2E_CHAT_DELAY_SECONDS)
                return self.send_json({
                    "answer": answer,
                    "memories": memories,
                    "memory_groups": memory_groups,
                    "missing_context": missing_context_for_query(message),
                    "model_enabled": provider_configured("chat"),
                    "provider": selected_provider("chat"),
                    "model": provider_model(selected_provider("chat")),
                    "analysis_prioritized": prioritized_jobs,
                    "response_type": response_type,
                    "recommendation_candidate": candidate,
                })
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/question-answers":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                fields = [str(payload[key]).strip() for key in ("domain", "question_id", "question", "answer")]
                if not all(fields):
                    raise ValueError("All question fields are required")
                with db() as connection:
                    cursor = connection.execute(
                        "INSERT INTO question_answers(domain,question_id,question,answer,created_at) VALUES(?,?,?,?,?)",
                        (*fields, now()),
                    )
                return self.send_json({"id": cursor.lastrowid, "message": "Saved"}, HTTPStatus.CREATED)
            except (KeyError, ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/checkins":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                fields = [str(payload[key]).strip() for key in ("mood", "energy", "focus")]
                note = str(payload.get("note", "")).strip()
                if not all(fields):
                    raise ValueError("mood, energy and focus are required")
                with db() as connection:
                    cursor = connection.execute(
                        "INSERT INTO checkins(mood,energy,focus,note,created_at) VALUES(?,?,?,?,?)",
                        (*fields, note, now()),
                    )
                return self.send_json({"id": cursor.lastrowid, "message": "Saved"}, HTTPStatus.CREATED)
            except (KeyError, ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/recommendations/generate":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                supplied_candidate = payload.get("candidate")
                domain = str((supplied_candidate or {}).get("domain", payload.get("domain", "other"))).strip()[:40]
                if domain not in {"money", "travel", "housing", "people", "other"}:
                    raise ValueError("Invalid domain")
                if isinstance(supplied_candidate, dict):
                    # Persist the exact candidate shown to the user.  Never
                    # regenerate a recommendation from its title during save.
                    draft = {
                        "domain": domain,
                        "title": str(supplied_candidate.get("title", "相談候補"))[:300],
                        "rationale": str(supplied_candidate.get("summary", supplied_candidate.get("rationale", "")))[:4000],
                        "options": supplied_candidate.get("options", []) if isinstance(supplied_candidate.get("options", []), list) else [],
                        "criteria": supplied_candidate.get("criteria", []) if isinstance(supplied_candidate.get("criteria", []), list) else [],
                        "tradeoffs": supplied_candidate.get("tradeoffs", []) if isinstance(supplied_candidate.get("tradeoffs", []), list) else [],
                        "plan_steps": supplied_candidate.get("plan_steps", []) if isinstance(supplied_candidate.get("plan_steps", []), list) else [],
                        "personal_context_used": supplied_candidate.get("personal_context_used", []),
                        "source_fact_ids": supplied_candidate.get("source_fact_ids", []) if isinstance(supplied_candidate.get("source_fact_ids", []), list) else [],
                        "source_decision_ids": supplied_candidate.get("source_decision_ids", []) if isinstance(supplied_candidate.get("source_decision_ids", []), list) else [],
                        "source_evidence_ids": supplied_candidate.get("source_evidence_ids", []) if isinstance(supplied_candidate.get("source_evidence_ids", []), list) else [],
                        "missing_context": supplied_candidate.get("missing_context", []) if isinstance(supplied_candidate.get("missing_context", []), list) else [],
                        "context": {"consultation_id": supplied_candidate.get("consultation_id"), "candidate_id": supplied_candidate.get("candidate_id"), "original_question": supplied_candidate.get("original_question"), "context_reference": supplied_candidate.get("context_reference", {})},
                    }
                else:
                    draft = build_local_recommendation(domain, str(payload.get("question", "")))
                timestamp = now()
                recommendation_context = dict(draft["context"] or {})
                recommendation_context["plan_steps"] = draft["plan_steps"]
                with db() as connection:
                    cursor = connection.execute(
                        """INSERT INTO recommendations(
                             domain,title,rationale,options_json,criteria_json,source_fact_ids_json,
                             source_decision_ids_json,source_evidence_ids_json,context_json,tradeoffs_json,
                             missing_context_json,status,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)""",
                        (draft["domain"], draft["title"], draft["rationale"], json.dumps(draft["options"], ensure_ascii=False),
                         json.dumps(draft["criteria"], ensure_ascii=False), json.dumps(draft["source_fact_ids"], ensure_ascii=False),
                         json.dumps(draft["source_decision_ids"], ensure_ascii=False),
                         json.dumps(draft["source_evidence_ids"], ensure_ascii=False),
                         json.dumps(recommendation_context, ensure_ascii=False),
                         json.dumps(draft["tradeoffs"], ensure_ascii=False),
                         json.dumps(draft["missing_context"], ensure_ascii=False), timestamp, timestamp),
                    )
                return self.send_json({"id": cursor.lastrowid, "plan_id": None, "cycle_stage": "recommended",
                                       "available_actions": ["create_plan", "dismiss"], **draft}, HTTPStatus.CREATED)
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        recommendation_plan_match = re.fullmatch(r"/api/recommendations/(\d+)/plan", path)
        if recommendation_plan_match:
            try:
                recommendation_id = int(recommendation_plan_match.group(1))
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                with db() as connection:
                    recommendation = connection.execute("SELECT * FROM recommendations WHERE id=?", (recommendation_id,)).fetchone()
                    if not recommendation or recommendation["status"] == "dismissed":
                        return self.send_json({"error": "Recommendation is not available"}, HTTPStatus.CONFLICT)
                    existing = connection.execute(
                        "SELECT id FROM plans WHERE source_recommendation_id=? ORDER BY id DESC LIMIT 1", (recommendation_id,)
                    ).fetchone()
                    if existing:
                        cycle = cycle_snapshot(recommendation_id)
                        return self.send_json(cycle, HTTPStatus.OK)
                    steps = payload.get("steps")
                    if not isinstance(steps, list):
                        steps = _json_value(recommendation["context_json"], {}).get("plan_steps", []) if isinstance(_json_value(recommendation["context_json"], {}), dict) else []
                    if not steps:
                        steps = [{"order": 1, "title": "次の一歩を決める", "detail": "提案に沿った具体的な行動を一つ選ぶ", "status": "pending"}]
                    timestamp = now()
                    connection.execute(
                        """INSERT INTO plans(domain,title,steps_json,budget,target_date,source_recommendation_id,decision_id,status,result,checkpoints_json,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (recommendation["domain"], recommendation["title"], json.dumps(steps[:50], ensure_ascii=False), payload.get("budget"),
                         str(payload.get("target_date", ""))[:32] or None, recommendation_id, None, "draft", "", "[]", timestamp, timestamp),
                    )
                    connection.execute("UPDATE recommendations SET status='accepted',updated_at=? WHERE id=?", (timestamp, recommendation_id))
                return self.send_json(cycle_snapshot(recommendation_id), HTTPStatus.CREATED)
            except (ValueError, json.JSONDecodeError, sqlite3.IntegrityError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        recommendation_decision_match = re.fullmatch(r"/api/recommendations/(\d+)/decision", path)
        if recommendation_decision_match:
            try:
                recommendation_id = int(recommendation_decision_match.group(1))
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                with db() as connection:
                    recommendation = connection.execute(
                        "SELECT * FROM recommendations WHERE id=?", (recommendation_id,)
                    ).fetchone()
                    if not recommendation:
                        return self.send_json({"error": "Recommendation not found"}, 404)
                    linked_plan = connection.execute(
                        "SELECT id FROM plans WHERE source_recommendation_id=? ORDER BY id DESC LIMIT 1", (recommendation_id,)
                    ).fetchone()
                    if not linked_plan and not payload.get("create_plan") and not payload.get("allow_legacy"):
                        return self.send_json({"error": "Create a plan before recording a decision"}, HTTPStatus.CONFLICT)
                    try:
                        options = json.loads(recommendation["options_json"] or "[]")
                    except json.JSONDecodeError:
                        options = []
                    if not isinstance(options, list):
                        options = []
                    selected = str(payload.get("selected_option", "")).strip()[:1000]
                    if selected and options and selected not in [str(option) for option in options]:
                        raise ValueError("selected_option must be one of the recommendation options")
                    status = "decided" if selected else "considering"
                    timestamp = now()
                    title = str(recommendation["title"] or "").strip()[:300]
                    question = str(payload.get("question", title))[:2000]
                    rationale = str(payload.get("rationale", recommendation["rationale"] or ""))[:4000]
                    source_fact_ids = json.loads(recommendation["source_fact_ids_json"] or "[]")
                    if not isinstance(source_fact_ids, list):
                        source_fact_ids = []
                    cursor = connection.execute(
                        """INSERT INTO decisions(domain,title,context,question,options_json,decision,selected_option,rationale,status,decided_on,
                           related_fact_ids_json,related_entity_ids_json,result,later_evaluation,source_recommendation_id,
                           outcome_recorded_at,evaluation_recorded_at,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (recommendation["domain"], title, "recommendation", question,
                         json.dumps(options, ensure_ascii=False), selected or "未選択", selected,
                         rationale, status, timestamp[:10] if selected else None,
                         json.dumps(source_fact_ids, ensure_ascii=False), "[]", "", "", recommendation_id,
                         None, None, timestamp, timestamp),
                    )
                    decision_id = cursor.lastrowid
                    connection.execute(
                        "UPDATE decisions SET decision_state=? WHERE id=?",
                        ("decided" if selected else "candidate", decision_id),
                    )
                    connection.execute(
                        "UPDATE recommendations SET status='converted',updated_at=? WHERE id=?",
                        (timestamp, recommendation_id),
                    )
                    plan_id = None
                    if bool(payload.get("create_plan")):
                        raw_steps = payload.get("steps", [])
                        if not isinstance(raw_steps, list):
                            raw_steps = [line.strip() for line in str(raw_steps).splitlines() if line.strip()]
                        existing_plan = connection.execute(
                            "SELECT id,steps_json FROM plans WHERE source_recommendation_id=? ORDER BY id DESC LIMIT 1",
                            (recommendation_id,),
                        ).fetchone()
                        if not raw_steps and existing_plan:
                            try:
                                raw_steps = json.loads(existing_plan["steps_json"] or "[]")
                            except json.JSONDecodeError:
                                raw_steps = []
                        if not raw_steps:
                            raw_steps = [{"order": 1, "title": selected or "選択肢を決める", "detail": "", "status": "pending"}]
                        if existing_plan:
                            connection.execute(
                                """UPDATE plans SET steps_json=?,budget=?,target_date=?,decision_id=?,updated_at=? WHERE id=?""",
                                (json.dumps(raw_steps[:50], ensure_ascii=False), payload.get("budget"),
                                 str(payload.get("target_date", ""))[:32] or None, decision_id, timestamp, existing_plan["id"]),
                            )
                            plan_id = existing_plan["id"]
                        else:
                            plan_cursor = connection.execute(
                                """INSERT INTO plans(domain,title,steps_json,budget,target_date,source_recommendation_id,decision_id,status,result,checkpoints_json,created_at,updated_at)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (recommendation["domain"], title, json.dumps(raw_steps[:50], ensure_ascii=False), payload.get("budget"),
                                 str(payload.get("target_date", ""))[:32] or None, recommendation_id, decision_id, "draft", "", "[]", timestamp, timestamp),
                            )
                            plan_id = plan_cursor.lastrowid
                return self.send_json({"decision_id": decision_id, "plan_id": plan_id, "status": status}, HTTPStatus.CREATED)
            except (ValueError, json.JSONDecodeError, sqlite3.IntegrityError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        plan_decision_match = re.fullmatch(r"/api/plans/(\d+)/decision", path)
        if plan_decision_match:
            try:
                plan_id = int(plan_decision_match.group(1))
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                with db() as connection:
                    plan = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
                    if not plan or not plan["source_recommendation_id"]:
                        return self.send_json({"error": "Plan must belong to a recommendation"}, HTTPStatus.CONFLICT)
                    if plan["status"] in {"cancelled", "completed"}:
                        return self.send_json({"error": "Plan cannot create a decision in its current state"}, HTTPStatus.CONFLICT)
                    existing = connection.execute(
                        "SELECT id FROM decisions WHERE source_recommendation_id=? ORDER BY id DESC LIMIT 1",
                        (plan["source_recommendation_id"],),
                    ).fetchone()
                    if existing:
                        return self.send_json(cycle_snapshot(int(plan["source_recommendation_id"])), HTTPStatus.OK)
                    recommendation = connection.execute("SELECT options_json FROM recommendations WHERE id=?", (plan["source_recommendation_id"],)).fetchone()
                    options = _json_value(recommendation["options_json"] if recommendation else "[]", [])
                    if not isinstance(options, list):
                        options = []
                    timestamp = now()
                    cursor = connection.execute(
                        """INSERT INTO decisions(domain,title,context,question,options_json,decision,selected_option,rationale,status,decided_on,
                           related_fact_ids_json,related_entity_ids_json,result,later_evaluation,source_recommendation_id,
                           outcome_recorded_at,evaluation_recorded_at,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (plan["domain"], plan["title"], "plan", "この計画で進めるか", json.dumps(options, ensure_ascii=False),
                         "未確定", "", "計画を確認して本人が判断する", "considering", None, "[]", "[]", "", "",
                         plan["source_recommendation_id"], None, None, timestamp, timestamp),
                    )
                    decision_id = cursor.lastrowid
                    connection.execute("UPDATE decisions SET decision_state='candidate' WHERE id=?", (decision_id,))
                    connection.execute("UPDATE plans SET status='active',decision_id=?,updated_at=? WHERE id=?", (decision_id, timestamp, plan_id))
                return self.send_json(cycle_snapshot(int(plan["source_recommendation_id"])), HTTPStatus.CREATED)
            except (ValueError, json.JSONDecodeError, sqlite3.IntegrityError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/plans":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                title = str(payload.get("title", "")).strip()[:300]
                domain = str(payload.get("domain", "other")).strip()[:40]
                if not title:
                    raise ValueError("title is required")
                steps = payload.get("steps", [])
                if not isinstance(steps, list):
                    steps = [line.strip() for line in str(steps).splitlines() if line.strip()]
                timestamp = now()
                with db() as connection:
                    cursor = connection.execute(
                        """INSERT INTO plans(domain,title,steps_json,budget,target_date,source_recommendation_id,decision_id,status,result,checkpoints_json,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (domain, title, json.dumps(steps[:50], ensure_ascii=False), payload.get("budget"), str(payload.get("target_date", ""))[:32] or None,
                         payload.get("source_recommendation_id"), payload.get("decision_id"), str(payload.get("status", "draft")),
                         str(payload.get("result", ""))[:4000],
                         json.dumps(payload.get("checkpoints", []) if isinstance(payload.get("checkpoints", []), list) else [], ensure_ascii=False),
                         timestamp, timestamp),
                    )
                return self.send_json({"id": cursor.lastrowid, "message": "計画を保存しました"}, HTTPStatus.CREATED)
            except (ValueError, json.JSONDecodeError, sqlite3.IntegrityError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path.startswith("/api/memory-proposals/") and path.endswith("/apply"):
            try:
                proposal_id = int(path.split("/")[3])
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                with db() as connection:
                    proposal = connection.execute(
                        "SELECT * FROM memory_proposals WHERE id=? AND status='pending'", (proposal_id,)
                    ).fetchone()
                if not proposal:
                    return self.send_json({"error": "Proposal not found"}, 404)
                facts = payload.get("facts", json.loads(proposal["facts_json"]))
                if not isinstance(facts, list):
                    raise ValueError("facts must be an array")
                saved = save_structured_facts(proposal["entry_id"], facts, user_confirmed=True)
                with db() as connection:
                    connection.execute("UPDATE memory_proposals SET status='applied', resolved_at=? WHERE id=?", (now(), proposal_id))
                return self.send_json({"facts": saved, "message": "確認して保存しました"}, HTTPStatus.CREATED)
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path.startswith("/api/memory-proposals/") and path.endswith("/discard"):
            try:
                proposal_id = int(path.split("/")[3])
                with db() as connection:
                    cursor = connection.execute(
                        "UPDATE memory_proposals SET status='discarded', resolved_at=? WHERE id=? AND status='pending'",
                        (now(), proposal_id),
                    )
                if not cursor.rowcount:
                    return self.send_json({"error": "Proposal not found"}, 404)
                return self.send_json({"message": "保存しませんでした"})
            except ValueError as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/decisions":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                title = str(payload.get("title", "")).strip()
                decision = str(payload.get("decision", "")).strip()
                if not title or not decision:
                    raise ValueError("title and decision are required")
                raw_options = payload.get("options", [])
                options = raw_options if isinstance(raw_options, list) else [line.strip() for line in str(raw_options).splitlines() if line.strip()]
                status = str(payload.get("status", "decided"))
                if status not in {"considering", "decided", "revisited"}:
                    raise ValueError("Invalid decision status")
                decision_state = str(payload.get("decision_state", payload.get("state", ""))).strip().lower()
                if not decision_state:
                    decision_state = "result" if str(payload.get("result", "")).strip() else ("considered" if status == "considering" else "decided")
                if decision_state not in DECISION_STATES:
                    raise ValueError("Invalid decision state")
                domain = str(payload.get("domain", "other"))[:40]
                related_facts = payload.get("related_fact_ids", [])
                related_entities = payload.get("related_entity_ids", [])
                timestamp = now()
                with db() as connection:
                    cursor = connection.execute(
                        """INSERT INTO decisions(domain,title,context,question,options_json,decision,selected_option,rationale,status,decided_on,
                           related_fact_ids_json,related_entity_ids_json,result,later_evaluation,source_recommendation_id,
                           outcome_recorded_at,evaluation_recorded_at,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (domain, title[:300], str(payload.get("context", ""))[:4000], str(payload.get("question", ""))[:2000],
                         json.dumps(options, ensure_ascii=False), decision[:1000], str(payload.get("selected_option", decision))[:1000],
                         str(payload.get("rationale", ""))[:4000], status, str(payload.get("decided_on", ""))[:32] or timestamp[:10],
                         json.dumps(related_facts if isinstance(related_facts, list) else [], ensure_ascii=False),
                         json.dumps(related_entities if isinstance(related_entities, list) else [], ensure_ascii=False),
                         str(payload.get("result", ""))[:4000], str(payload.get("later_evaluation", ""))[:4000],
                         payload.get("source_recommendation_id"),
                         timestamp if payload.get("result") else None,
                         timestamp if payload.get("later_evaluation") else None,
                         timestamp, timestamp),
                    )
                    connection.execute("UPDATE decisions SET decision_state=? WHERE id=?", (decision_state, cursor.lastrowid))
                return self.send_json({"id": cursor.lastrowid, "message": "判断を記録しました"}, HTTPStatus.CREATED)
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        decision_execute_match = re.fullmatch(r"/api/decisions/(\d+)/execute", path)
        if decision_execute_match:
            try:
                decision_id = int(decision_execute_match.group(1))
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                with db() as connection:
                    decision = connection.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
                    if not decision:
                        return self.send_json({"error": "Decision not found"}, HTTPStatus.NOT_FOUND)
                    if decision["decision_state"] != "decided":
                        return self.send_json({"error": "Decision must be confirmed before execution"}, HTTPStatus.CONFLICT)
                    timestamp = str(payload.get("executed_at", ""))[:32] or now()
                    summary = str(payload.get("note", "ユーザーが実行した"))[:4000]
                    cursor = connection.execute(
                        "INSERT INTO execution_events(decision_id,plan_id,event_type,summary,source_entry_id,source_chunk_id,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (decision_id, None, "executed", summary, payload.get("source_entry_id"), payload.get("source_chunk_id"), timestamp, now()),
                    )
                    connection.execute("UPDATE decisions SET decision_state='executed',updated_at=? WHERE id=?", (now(), decision_id))
                    rec = decision["source_recommendation_id"]
                return self.send_json({"event_id": cursor.lastrowid, "decision_id": decision_id,
                                       "cycle": cycle_snapshot(int(rec)) if rec else None}, HTTPStatus.CREATED)
            except (ValueError, TypeError, json.JSONDecodeError, sqlite3.IntegrityError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        decision_result_match = re.fullmatch(r"/api/decisions/(\d+)/result", path)
        if decision_result_match:
            try:
                decision_id = int(decision_result_match.group(1))
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                comment = str(payload.get("comment", payload.get("result", ""))).strip()[:4000]
                if not comment:
                    raise ValueError("comment is required")
                with db() as connection:
                    decision = connection.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
                    if not decision:
                        return self.send_json({"error": "Decision not found"}, HTTPStatus.NOT_FOUND)
                    if decision["decision_state"] != "executed":
                        return self.send_json({"error": "Decision must be executed before recording a result"}, HTTPStatus.CONFLICT)
                    completed_at = str(payload.get("completed_at", ""))[:32] or now()
                    rating = str(payload.get("rating", "")).strip()[:20]
                    result_text = f"評価: {rating}\n{comment}" if rating else comment
                    connection.execute("UPDATE decisions SET result=?,outcome_recorded_at=?,decision_state='result',updated_at=? WHERE id=?",
                                       (result_text, completed_at, now(), decision_id))
                    rec = decision["source_recommendation_id"]
                return self.send_json({"decision_id": decision_id, "result": result_text,
                                       "cycle": cycle_snapshot(int(rec)) if rec else None}, HTTPStatus.CREATED)
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        decision_evaluate_match = re.fullmatch(r"/api/decisions/(\d+)/evaluate", path)
        if decision_evaluate_match:
            try:
                decision_id = int(decision_evaluate_match.group(1))
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                evaluation = str(payload.get("later_evaluation", payload.get("comment", ""))).strip()[:4000]
                if not evaluation:
                    raise ValueError("later_evaluation is required")
                with db() as connection:
                    decision = connection.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
                    if not decision:
                        return self.send_json({"error": "Decision not found"}, HTTPStatus.NOT_FOUND)
                    if decision["decision_state"] != "result":
                        return self.send_json({"error": "A result is required before later evaluation"}, HTTPStatus.CONFLICT)
                    connection.execute("UPDATE decisions SET later_evaluation=?,evaluation_recorded_at=?,updated_at=? WHERE id=?",
                                       (evaluation, now(), now(), decision_id))
                    rec = decision["source_recommendation_id"]
                return self.send_json({"decision_id": decision_id, "later_evaluation": evaluation,
                                       "cycle": cycle_snapshot(int(rec)) if rec else None}, HTTPStatus.CREATED)
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/analysis-jobs/requeue":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                changed = requeue_analysis_jobs(str(payload.get("mode", "failed")))
                return self.send_json({"requeued": changed, "jobs": analysis_job_summary()})
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/memory-categories":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                slug = normalize_category_slug(payload.get("slug", ""))
                if slug == "other" and str(payload.get("slug", "")).strip().lower() not in {"other", ""}:
                    raise ValueError("Category slug must use lowercase letters, numbers, - or _")
                label = str(payload.get("label", slug)).strip()[:80]
                if not label:
                    raise ValueError("Category label is required")
                with db() as connection:
                    connection.execute(
                        """INSERT INTO memory_categories(slug,label,icon,description,sort_order,active,created_at,updated_at)
                           VALUES(?,?,?,?,?,1,?,?)
                           ON CONFLICT(slug) DO UPDATE SET label=excluded.label,icon=excluded.icon,
                             description=excluded.description,updated_at=excluded.updated_at""",
                        (slug, label, str(payload.get("icon", "🏷️"))[:16], str(payload.get("description", ""))[:300],
                         int(payload.get("sort_order", 999)), now(), now()),
                    )
                return self.send_json({"slug": slug, "message": "Category saved"}, HTTPStatus.CREATED)
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/shutdown":
            self.send_json({"message": "Personal OS を安全に終了します。"})
            request_server_shutdown()
            return
        if path == "/api/backup":
            try:
                created = backup_database(force=True)
                return self.send_json({"message": "バックアップを作成しました", "path": _backup_display_path(created) if created else None, **backup_status()}, HTTPStatus.CREATED)
            except sqlite3.Error as error:
                return self.send_json({"error": f"バックアップに失敗しました: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        if path == "/api/backup/restore":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                if payload.get("confirm") is not True:
                    raise ValueError("復元する場合は confirm=true が必要です")
                result = restore_database(str(payload.get("path", "")))
                initialize()
                return self.send_json(result)
            except (ValueError, FileNotFoundError, sqlite3.Error, json.JSONDecodeError, OSError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/privacy/delete":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                if payload.get("confirm_phrase") != "DELETE":
                    raise ValueError("confirm_phrase=DELETE is required")
                target_type = str(payload.get("target_type", ""))
                target_id = int(payload.get("target_id"))
                backup = backup_database(force=True)
                result = delete_private_data(target_type, target_id, bool(payload.get("delete_raw", False)))
                result["backup"] = _backup_display_path(backup) if backup else None
                with db() as connection:
                    connection.execute(
                        """INSERT INTO privacy_audit_log(target_type,target_id,delete_raw,backup_path,deleted_counts_json,created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (target_type, target_id, int(result["raw_deleted"]), result["backup"] or "",
                         json.dumps({key: value for key, value in result.items() if key not in {"backup", "target_type", "target_id"}}, ensure_ascii=False), now()),
                    )
                return self.send_json(result)
            except (ValueError, TypeError, sqlite3.Error, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/ux-feedback":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                screen = str(payload.get("screen", "other")).strip()[:80] or "other"
                feedback_type = str(payload.get("feedback_type", "improvement")).strip().lower()
                severity = str(payload.get("severity", "medium")).strip().lower()
                body = str(payload.get("body", "")).strip()[:4000]
                expected = str(payload.get("expected_behavior", "")).strip()[:2000]
                if feedback_type not in {"improvement", "bug", "confusing", "praise"}:
                    raise ValueError("Invalid feedback_type")
                if severity not in {"low", "medium", "high"}:
                    raise ValueError("Invalid severity")
                if not body:
                    raise ValueError("body is required")
                with db() as connection:
                    cursor = connection.execute(
                        """INSERT INTO ux_feedback(screen,feedback_type,body,expected_behavior,severity,status,created_at)
                           VALUES(?,?,?,?,?,'open',?)""",
                        (screen, feedback_type, body, expected, severity, now()),
                    )
                return self.send_json({"id": cursor.lastrowid, "message": "Feedback saved locally"}, HTTPStatus.CREATED)
            except (ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path not in {"/api/entries", "/api/capture"}:
            return self.send_json({"error": "Not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            title = str(payload.get("title", "")).strip() or "無題の記録"
            body = str(payload.get("body", "")).strip()
            kind = str(payload.get("kind", "note"))
            status = str(payload.get("status", "inbox"))
            source = str(payload.get("source", "manual"))
            raw_tags = payload.get("tags", "")
            tags = ", ".join(raw_tags) if isinstance(raw_tags, list) else str(raw_tags)
            if kind not in {"note", "task", "conversation"} or status not in {"inbox", "note", "done"}:
                raise ValueError("Invalid kind or status")
            timestamp = now()
            with db() as connection:
                cursor = connection.execute(
                    """INSERT INTO entries(kind,title,body,source,tags,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (kind, title, body, source, tags, status, timestamp, timestamp),
                )
            ensure_document_for_entry(cursor.lastrowid)
            if kind == "task":
                organize_tasks()
            return self.send_json({"id": cursor.lastrowid, "message": "Saved"}, HTTPStatus.CREATED)
        except (ValueError, json.JSONDecodeError) as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        if not self._authorize(True):
            return
        if path == "/api/providers":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                for kind in ("chat", "extract"):
                    key = f"{kind}_provider"
                    if key in payload:
                        value = str(payload[key]).lower()
                        if value not in {"auto", "openai", "gemini", "local"}:
                            raise ValueError(f"Invalid {key}")
                        save_setting(key, value)
                for key in ("openai_model", "gemini_model", "local_llm_model"):
                    if key in payload:
                        save_setting(key, str(payload[key]).strip()[:120])
                for field, environment_name in (("openai_api_key", "OPENAI_API_KEY"), ("gemini_api_key", "GEMINI_API_KEY")):
                    if field in payload:
                        value = str(payload[field] or "").strip()
                        if value:
                            os.environ[environment_name] = value
                if "extract_parallel_providers" in payload:
                    raw_parallel = payload["extract_parallel_providers"]
                    if isinstance(raw_parallel, list):
                        names = [str(item).strip().lower() for item in raw_parallel]
                    else:
                        names = [item.strip().lower() for item in str(raw_parallel or "").split(",") if item.strip()]
                    if any(name not in {"local", "openai", "gemini"} for name in names):
                        raise ValueError("extract_parallel_providers must contain only local, openai, gemini")
                    save_setting("extract_parallel_providers", ",".join(dict.fromkeys(names)))
                if "analysis_batch_size" in payload:
                    try:
                        batch_size = int(payload["analysis_batch_size"])
                    except (TypeError, ValueError) as error:
                        raise ValueError("analysis_batch_size must be an integer") from error
                    if not 1 <= batch_size <= 200:
                        raise ValueError("analysis_batch_size must be between 1 and 200")
                    save_setting("analysis_batch_size", str(batch_size))
                if "local_llm_base_url" in payload:
                    url = str(payload["local_llm_base_url"]).strip().rstrip("/")
                    if url and not (url.startswith("http://") or url.startswith("https://")):
                        raise ValueError("local_llm_base_url must start with http:// or https://")
                    save_setting("local_llm_base_url", url)
                if "auto_start_local_llm" in payload:
                    enabled = payload["auto_start_local_llm"] is True or str(payload["auto_start_local_llm"]).strip().lower() == "true"
                    save_setting("auto_start_local_llm", "true" if enabled else "false")
                for key in ("allow_cloud_fallback_chat", "allow_cloud_fallback_note", "allow_cloud_fallback_import", "allow_sensitive_cloud"):
                    if key in payload:
                        value = payload[key]
                        enabled = value is True or (isinstance(value, str) and value.strip().lower() == "true")
                        save_setting(key, "true" if enabled else "false")
                return self.send_json(provider_status())
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path.startswith("/api/finance-transactions/"):
            try:
                fact_id = int(path.rsplit("/", 1)[-1])
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                state = str(payload.get("state", "")).strip()
                if state not in {"confirmed", "excluded", "pending"}:
                    raise ValueError("Invalid transaction state")
                with db() as connection:
                    if state == "confirmed":
                        connection.execute(
                            "UPDATE fact_reviews SET state='confirmed',reason='ユーザー確認済み',review_note=?,reviewed_at=? WHERE fact_id=?",
                            ("取引として確認", now(), fact_id),
                        )
                        result = sync_finance_transaction(connection, fact_id, confirmed=True)
                    elif state == "excluded":
                        connection.execute("UPDATE finance_transactions SET eligibility_state='excluded',eligibility_reason='manual_excluded',validated_at=? WHERE fact_id=?", (now(), fact_id))
                        connection.execute("UPDATE finance_transaction_candidates SET eligibility_state='excluded',eligibility_reason='manual_excluded',validated_at=? WHERE fact_id=?", (now(), fact_id))
                        result = {"state": "excluded", "reason": "manual_excluded"}
                    else:
                        connection.execute("UPDATE finance_transactions SET eligibility_state='pending',eligibility_reason='manual_deferred',validated_at=? WHERE fact_id=?", (now(), fact_id))
                        connection.execute("UPDATE finance_transaction_candidates SET eligibility_state='pending',eligibility_reason='manual_deferred',validated_at=? WHERE fact_id=?", (now(), fact_id))
                        result = {"state": "pending", "reason": "manual_deferred"}
                    quality = apply_memory_quality_to_fact(connection, fact_id, source="manual_transaction_review")
                return self.send_json({"fact_id": fact_id, "quality": quality, **result})
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path.startswith("/api/facts/") and path.endswith("/review"):
            try:
                fact_id = int(path.split("/")[3])
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                return self.send_json(update_fact_review_state(
                    fact_id, str(payload["state"]), str(payload.get("defer_for") or "").strip(), str(payload.get("note") or ""),
                ))
            except LookupError as error:
                return self.send_json({"error": str(error)}, 404)
            except (KeyError, ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path.startswith("/api/facts/") and path.endswith("/category"):
            try:
                fact_id = int(path.split("/")[3])
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                requested = payload.get("category", "other")
                with db() as connection:
                    fact = connection.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
                    if not fact:
                        return self.send_json({"error": "Not found"}, 404)
                    category = ensure_memory_category(connection, requested)
                    value = json.loads(fact["value_json"] or "{}")
                    fact_key = canonical_fact_key(
                        category, fact["fact_type"], value.get("asset"), value.get("details"), fact["summary"]
                    )
                    connection.execute("UPDATE facts SET category=?,fact_key=? WHERE id=?", (category, fact_key, fact_id))
                    apply_fact_timeline(connection, fact_id)
                return self.send_json({"message": "Category updated", "category": category})
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        fact_correction_match = re.fullmatch(r"/api/facts/(\d+)", path)
        if fact_correction_match:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                return self.send_json(correct_fact(int(fact_correction_match.group(1)), payload))
            except (ValueError, TypeError, json.JSONDecodeError, sqlite3.IntegrityError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        decision_event_match = re.fullmatch(r"/api/decisions/(\d+)/events", path)
        if decision_event_match:
            try:
                decision_id = int(decision_event_match.group(1))
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                event_type = str(payload.get("event_type", "note")).strip()[:80]
                summary = str(payload.get("summary", "")).strip()[:4000]
                if not event_type or not summary:
                    raise ValueError("event_type and summary are required")
                state = str(payload.get("decision_state", payload.get("state", ""))).strip().lower()
                with db() as connection:
                    decision = connection.execute("SELECT id FROM decisions WHERE id=?", (decision_id,)).fetchone()
                    if not decision:
                        return self.send_json({"error": "Not found"}, 404)
                    cursor = connection.execute(
                        "INSERT INTO execution_events(decision_id,plan_id,event_type,summary,source_entry_id,source_chunk_id,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (decision_id, payload.get("plan_id"), event_type, summary, payload.get("source_entry_id"),
                         payload.get("source_chunk_id"), str(payload.get("occurred_at", ""))[:32] or now(), now()),
                    )
                    if state:
                        if state not in DECISION_STATES:
                            raise ValueError("Invalid decision state")
                        current_state = connection.execute("SELECT decision_state FROM decisions WHERE id=?", (decision_id,)).fetchone()[0]
                        if not valid_cycle_transition(current_state, state):
                            raise ValueError(f"Invalid cycle transition: {current_state} -> {state}")
                        connection.execute("UPDATE decisions SET decision_state=?,updated_at=? WHERE id=?", (state, now(), decision_id))
                    elif event_type.lower() in {"executed", "execution", "completed"}:
                        current_state = connection.execute("SELECT decision_state FROM decisions WHERE id=?", (decision_id,)).fetchone()[0]
                        if not valid_cycle_transition(current_state, "executed"):
                            raise ValueError(f"Invalid cycle transition: {current_state} -> executed")
                        connection.execute("UPDATE decisions SET decision_state='executed',updated_at=? WHERE id=?", (now(), decision_id))
                return self.send_json({"id": cursor.lastrowid, "decision_id": decision_id}, HTTPStatus.CREATED)
            except (ValueError, TypeError, json.JSONDecodeError, sqlite3.IntegrityError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path.startswith("/api/decisions/"):
            try:
                decision_id = int(path.rsplit("/", 1)[-1])
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                fields: list[str] = []
                values: list[object] = []
                for key in ("result", "later_evaluation", "selected_option"):
                    if key in payload:
                        fields.append(f"{key}=?")
                        values.append(str(payload[key])[:4000])
                        if key == "result":
                            fields.append("outcome_recorded_at=?")
                            values.append(now())
                        elif key == "later_evaluation":
                            fields.append("evaluation_recorded_at=?")
                            values.append(now())
                if "result" in payload and not ("decision_state" in payload or "state" in payload):
                    fields.append("decision_state=?")
                    values.append("result" if str(payload.get("result", "")).strip() else "decided")
                if "status" in payload:
                    status = str(payload["status"])
                    if status not in {"considering", "decided", "revisited"}:
                        raise ValueError("Invalid decision status")
                    fields.append("status=?")
                    values.append(status)
                if "decision_state" in payload or "state" in payload:
                    decision_state = str(payload.get("decision_state", payload.get("state", ""))).strip().lower()
                    if decision_state not in DECISION_STATES:
                        raise ValueError("Invalid decision state")
                    fields.append("decision_state=?")
                    values.append(decision_state)
                if not fields:
                    raise ValueError("No decision fields supplied")
                values.extend([now(), decision_id])
                with db() as connection:
                    current = connection.execute("SELECT decision_state,source_recommendation_id FROM decisions WHERE id=?", (decision_id,)).fetchone()
                    if not current:
                        return self.send_json({"error": "Not found"}, 404)
                    requested_state = None
                    for field, value in zip(fields, values):
                        if field.startswith("decision_state="):
                            requested_state = str(value)
                    if "selected_option" in payload and not requested_state:
                        requested_state = "decided"
                    if "result" in payload and current["decision_state"] != "executed":
                        raise ValueError("Decision must be executed before result")
                    if "later_evaluation" in payload and not str(payload.get("later_evaluation", "")).strip():
                        raise ValueError("later_evaluation is required")
                    if requested_state == "decided" and current["decision_state"] not in {"candidate", "considered"}:
                        raise ValueError("Only a candidate decision can be confirmed")
                    if requested_state and not valid_cycle_transition(current["decision_state"], requested_state):
                        raise ValueError(f"Invalid cycle transition: {current['decision_state']} -> {requested_state}")
                    cursor = connection.execute(
                        f"UPDATE decisions SET {', '.join(fields)}, updated_at=? WHERE id=?", values
                    )
                if not cursor.rowcount:
                    return self.send_json({"error": "Not found"}, 404)
                return self.send_json({"message": "Decision updated"})
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path.startswith("/api/recommendations/"):
            try:
                recommendation_id = int(path.rsplit("/", 1)[-1])
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                status = str(payload.get("status", "draft"))
                if status not in {"draft", "accepted", "dismissed", "converted"}:
                    raise ValueError("Invalid recommendation status")
                with db() as connection:
                    cursor = connection.execute("UPDATE recommendations SET status=?,updated_at=? WHERE id=?", (status, now(), recommendation_id))
                if not cursor.rowcount:
                    return self.send_json({"error": "Not found"}, 404)
                return self.send_json({"message": "提案を更新しました"})
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path.startswith("/api/plans/"):
            try:
                plan_id = int(path.rsplit("/", 1)[-1])
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                fields = []
                values = []
                if "status" in payload:
                    status = str(payload["status"])
                    if status not in {"draft", "active", "completed", "cancelled"}:
                        raise ValueError("Invalid plan status")
                    fields.append("status=?"); values.append(status)
                for key in ("result", "target_date"):
                    if key in payload:
                        fields.append(f"{key}=?"); values.append(str(payload[key])[:4000])
                if "steps" in payload:
                    steps = payload["steps"] if isinstance(payload["steps"], list) else []
                    fields.append("steps_json=?"); values.append(json.dumps(steps[:50], ensure_ascii=False))
                if not fields:
                    raise ValueError("No plan fields supplied")
                values.extend([now(), plan_id])
                with db() as connection:
                    cursor = connection.execute(f"UPDATE plans SET {', '.join(fields)},updated_at=? WHERE id=?", values)
                    if cursor.rowcount and payload.get("result"):
                        plan = connection.execute("SELECT decision_id FROM plans WHERE id=?", (plan_id,)).fetchone()
                        if plan and plan["decision_id"]:
                            connection.execute(
                                "UPDATE decisions SET result=?,outcome_recorded_at=?,updated_at=? WHERE id=?",
                                (str(payload["result"])[:4000], now(), now(), plan["decision_id"]),
                            )
                            connection.execute(
                                "UPDATE decisions SET decision_state='result' WHERE id=?",
                                (plan["decision_id"],),
                            )
                if not cursor.rowcount:
                    return self.send_json({"error": "Not found"}, 404)
                return self.send_json({"message": "計画を更新しました"})
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if not path.startswith("/api/entries/"):
            return self.send_json({"error": "Not found"}, 404)
        try:
            entry_id = int(path.rsplit("/", 1)[-1])
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            status = str(payload["status"])
            if status not in {"inbox", "note", "done"}:
                raise ValueError("Invalid status")
            with db() as connection:
                cursor = connection.execute("UPDATE entries SET status=?, updated_at=? WHERE id=?", (status, now(), entry_id))
            if not cursor.rowcount:
                return self.send_json({"error": "Not found"}, 404)
            self.send_json({"message": "Updated"})
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


def lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


if __name__ == "__main__":
    requested_port = int(os.environ.get("PERSONAL_OS_PORT", str(APP_PORT)))
    if requested_port != APP_PORT:
        print(
            f"{APP_ENV} environment uses fixed port {APP_PORT}. "
            f"Remove PERSONAL_OS_PORT={requested_port} or set it to {APP_PORT}."
        )
        raise SystemExit(2)
    port = APP_PORT
    migration_backup = backup_before_migration("015_action_center_review_inbox_stabilization")
    initialize()
    acquired, existing = acquire_runtime_lease(port)
    if not acquired:
        print(
            "Personal OS is already running "
            f"(PID {existing.get('pid')}, port {existing.get('port')}). "
            "Open that server or use its 終了 button before starting another instance."
        )
        raise SystemExit(1)
    recovered = recover_interrupted_analysis()
    environment_label = "verification" if APP_ENV == "verification" else "production"
    print(f"Personal OS ({environment_label}): http://localhost:{port}")
    if migration_backup:
        print(f"Pre-migration backup: {migration_backup}")
    if recovered:
        print(f"Recovered {recovered} interrupted analysis job(s).")
    print(f"iPhone (same Wi-Fi): http://{lan_ip()}:{port}")
    print("Stop with Ctrl+C")
    threading.Thread(target=analysis_loop, name="gemini-import-analysis", daemon=True).start()
    threading.Thread(target=embedding_loop, name="local-embedding-worker", daemon=True).start()
    threading.Thread(target=backup_loop, name="local-backup", daemon=True).start()
    threading.Thread(target=runtime_heartbeat_loop, name="runtime-heartbeat", daemon=True).start()
    bind_host = os.environ.get("PERSONAL_OS_HOST", "0.0.0.0").strip() or "0.0.0.0"
    SERVER = ThreadingHTTPServer((bind_host, port), Handler)
    try:
        SERVER.serve_forever()
    finally:
        release_runtime_lease()
