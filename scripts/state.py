#!/usr/bin/env python3
"""Durable, atomic invocation state for Auto Dispatch."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import string
import sqlite3
import sys
import tempfile
import threading


ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SNAPSHOT_DIR_RE = re.compile(r"^auto-dispatch\.[A-Za-z0-9]{6}$")
PACKET_DIR_RE = re.compile(r"^auto-dispatch-create\.[A-Za-z0-9]{6}$")
APPLICATION_ID = 0x41554450  # "AUDP"
SCHEMA_VERSION = 2
ROUTES = (
    "luna-low",
    "luna-medium",
    "terra-low",
    "terra-medium",
    "terra-high",
    "terra-xhigh",
    "sol-medium",
    "sol-high",
    "sol-xhigh",
    "sol-max",
)
STAGES = {"assessment_reserved", "assessed", "create_reserved", "created"}
ROW_COLUMNS = {
    "invocation_key",
    "source_thread_id",
    "user_message_id",
    "snapshot_sha256",
    "snapshot_path",
    "stage",
    "route",
    "create_packet_sha256",
    "create_packet_path",
    "destination_kind",
    "destination_id",
    "created_at",
    "updated_at",
}
CREATE_TABLE_SQL = """
CREATE TABLE invocations (
    invocation_key TEXT PRIMARY KEY NOT NULL,
    source_thread_id TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    snapshot_path TEXT NOT NULL,
    stage TEXT NOT NULL,
    route TEXT,
    create_packet_sha256 TEXT,
    create_packet_path TEXT,
    destination_kind TEXT,
    destination_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_thread_id, user_message_id)
)
"""


class StateError(ValueError):
    pass


def default_db_path() -> Path:
    configured_root = os.environ.get("CODEX_HOME")
    if not configured_root:
        codex_root = Path.home() / ".codex"
    else:
        codex_root = Path(configured_root).expanduser()
        if not codex_root.is_absolute():
            raise StateError("CODEX_HOME must be absolute when set")
    return codex_root / "state" / "auto-dispatch.sqlite3"


def require_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise StateError(f"{name} must match {ID_RE.pattern}")
    return value


def require_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise StateError(f"{name} must be a lowercase SHA-256 digest")
    return value


def validate_temp_file_path(
    value: object,
    directory_pattern: re.Pattern[str],
    filename: str,
    name: str,
) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise StateError(f"{name} must be an absolute temporary file path")
    path = Path(value)
    if (
        not path.is_absolute()
        or path.name != filename
        or path.parent.parent.resolve() != Path("/tmp").resolve()
        or directory_pattern.fullmatch(path.parent.name) is None
    ):
        raise StateError(f"invalid {name}")
    return path


def file_sha256(
    value: object,
    directory_pattern: re.Pattern[str],
    filename: str,
    name: str,
) -> tuple[Path, str]:
    path = validate_temp_file_path(value, directory_pattern, filename, name)
    try:
        directory_stat = path.parent.lstat()
        file_stat = path.lstat()
    except OSError as exc:
        raise StateError(f"{name} is unavailable: {exc}") from exc
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or stat.S_ISLNK(directory_stat.st_mode)
        or directory_stat.st_uid != os.getuid()
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
    ):
        raise StateError(f"{name} directory must be owned, private, and not a symlink")
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or stat.S_ISLNK(file_stat.st_mode)
        or file_stat.st_uid != os.getuid()
    ):
        raise StateError(f"{name} must be an owned regular file")

    if not hasattr(os, "O_NOFOLLOW"):
        raise StateError("no-follow file reads are unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened_mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(opened_mode):
            raise StateError(f"{name} must remain a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            digest = hashlib.sha256(stream.read()).hexdigest()
    finally:
        os.close(descriptor)
    return path, digest


def validate_packet_preparation_path(value: object) -> Path:
    path = validate_temp_file_path(
        value,
        PACKET_DIR_RE,
        "create-packet.json",
        "create_packet_path",
    )
    try:
        directory_stat = path.parent.lstat()
    except OSError as exc:
        raise StateError(f"create packet directory is unavailable: {exc}") from exc
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or stat.S_ISLNK(directory_stat.st_mode)
        or directory_stat.st_uid != os.getuid()
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
    ):
        raise StateError(
            "create packet directory must be owned, private, and not a symlink"
        )
    if path.exists() or path.is_symlink() or any(path.parent.iterdir()):
        raise StateError("create packet directory must be empty before registration")
    return path


@contextmanager
def assessor_lease(snapshot_path: Path):
    snapshot_path = validate_temp_file_path(
        snapshot_path, SNAPSHOT_DIR_RE, "route-brief.md", "snapshot_path"
    )
    directory_stat = snapshot_path.parent.lstat()
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or stat.S_ISLNK(directory_stat.st_mode)
        or directory_stat.st_uid != os.getuid()
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
    ):
        raise StateError("snapshot directory must be owned, private, and not a symlink")
    if not hasattr(os, "O_NOFOLLOW"):
        raise StateError("no-follow lock files are unavailable")

    lock_path = snapshot_path.parent / ".assessor.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.getuid():
            raise StateError("assessment lease must be an owned regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateError(
                f"assessment owner is still active for {snapshot_path}"
            ) from exc
        try:
            yield descriptor
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def canonical_identity(source_thread_id: str, user_message_id: str) -> str:
    identity = [
        "auto-dispatch/v1",
        require_identifier(source_thread_id, "source_thread_id"),
        require_identifier(user_message_id, "user_message_id"),
    ]
    return json.dumps(identity, separators=(",", ":"), ensure_ascii=True)


def invocation_key(source_thread_id: str, user_message_id: str) -> str:
    identity = canonical_identity(source_thread_id, user_message_id)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized_sql(value: str) -> str:
    return " ".join(value.split())


def require_safe_cleanup() -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise StateError("safe temporary-directory cleanup is unavailable")


def validate_schema(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        raise StateError(f"state integrity check failed: {integrity}")

    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    if application_id != APPLICATION_ID:
        raise StateError(f"unexpected state application id: {application_id}")
    schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if schema_version != SCHEMA_VERSION:
        raise StateError(f"unsupported state schema version: {schema_version}")

    objects = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "ORDER BY type, name"
    ).fetchall()
    expected_objects = {
        ("index", "sqlite_autoindex_invocations_1", "invocations", None),
        ("index", "sqlite_autoindex_invocations_2", "invocations", None),
    }
    actual_indexes = {
        (row["type"], row["name"], row["tbl_name"], row["sql"])
        for row in objects
        if row["type"] == "index"
    }
    tables = [row for row in objects if row["type"] == "table"]
    if (
        len(objects) != 3
        or len(tables) != 1
        or actual_indexes != expected_objects
    ):
        raise StateError("state database has unexpected schema objects")
    table = tables[0]
    if table["name"] != "invocations" or table["tbl_name"] != "invocations":
        raise StateError("state database has an unexpected table")
    table_sql = table["sql"]
    if (
        not isinstance(table_sql, str)
        or normalized_sql(table_sql) != normalized_sql(CREATE_TABLE_SQL)
    ):
        raise StateError("invocations table schema does not match")

    indexes = connection.execute("PRAGMA index_list(invocations)").fetchall()
    index_shapes = {
        (index["unique"], index["origin"], index["partial"])
        for index in indexes
    }
    if len(indexes) != 2 or index_shapes != {(1, "pk", 0), (1, "u", 0)}:
        raise StateError("invocations table indexes do not match")


def connect(db_path: Path) -> sqlite3.Connection:
    require_safe_cleanup()
    db_path = db_path.expanduser()
    if not db_path.is_absolute():
        raise StateError("state database path must be absolute")
    db_path = db_path.parent.resolve() / db_path.name
    if db_path.is_symlink():
        raise StateError("state database must not be a symlink")
    if db_path.exists():
        database_stat = db_path.lstat()
        if (
            not stat.S_ISREG(database_stat.st_mode)
            or database_stat.st_uid != os.getuid()
        ):
            raise StateError("state database must be an owned regular file")
    parent_existed = db_path.parent.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    existed = db_path.exists()
    if not parent_existed:
        os.chmod(db_path.parent, 0o700)
    connection = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    if not existed:
        os.chmod(db_path, 0o600)

    try:
        connection.execute("BEGIN EXCLUSIVE")
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        objects = connection.execute("SELECT name FROM sqlite_schema").fetchall()
        is_empty = application_id == 0 and schema_version == 0 and not objects
        if is_empty:
            if existed:
                raise StateError("refusing to initialize a pre-existing database")
            connection.execute(CREATE_TABLE_SQL)
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        validate_schema(connection)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()
        raise
    os.chmod(db_path, 0o600)
    return connection


def begin(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")


def validate_row(row: sqlite3.Row) -> None:
    result = dict(row)
    if set(result) != ROW_COLUMNS:
        raise StateError("stored invocation has an unexpected schema")

    key = require_sha256(result["invocation_key"], "stored invocation_key")
    source_thread_id = require_identifier(
        result["source_thread_id"], "stored source_thread_id"
    )
    user_message_id = require_identifier(
        result["user_message_id"], "stored user_message_id"
    )
    if invocation_key(source_thread_id, user_message_id) != key:
        raise StateError("stored invocation identity does not match its key")
    require_sha256(result["snapshot_sha256"], "stored snapshot_sha256")
    validate_temp_file_path(
        result["snapshot_path"],
        SNAPSHOT_DIR_RE,
        "route-brief.md",
        "stored snapshot_path",
    )

    stage = result["stage"]
    if stage not in STAGES:
        raise StateError(f"invalid stored stage: {stage!r}")
    for name in ("created_at", "updated_at"):
        if not isinstance(result[name], str) or not result[name]:
            raise StateError(f"invalid stored {name}")

    if stage == "assessment_reserved":
        if any(
            result[name] is not None
            for name in (
                "route",
                "create_packet_sha256",
                "create_packet_path",
                "destination_kind",
                "destination_id",
            )
        ):
            raise StateError("assessment reservation contains later-stage state")
        return

    if result["route"] not in ROUTES:
        raise StateError(f"invalid stored route: {result['route']!r}")
    if stage == "assessed":
        if result["create_packet_sha256"] is not None or any(
            result[name] is not None
            for name in ("destination_kind", "destination_id")
        ):
            raise StateError("assessment contains later-stage state")
        validate_temp_file_path(
            result["create_packet_path"],
            PACKET_DIR_RE,
            "create-packet.json",
            "stored create_packet_path",
        )
        return

    require_sha256(
        result["create_packet_sha256"], "stored create_packet_sha256"
    )
    validate_temp_file_path(
        result["create_packet_path"],
        PACKET_DIR_RE,
        "create-packet.json",
        "stored create_packet_path",
    )
    if stage == "create_reserved":
        if (
            result["destination_kind"] is not None
            or result["destination_id"] is not None
        ):
            raise StateError("create reservation contains a destination")
        return

    if result["destination_kind"] not in {"threadId", "clientThreadId"}:
        raise StateError("invalid stored destination kind")
    require_identifier(result["destination_id"], "stored destination_id")


def row_dict(row: sqlite3.Row | None) -> dict[str, object]:
    if row is None:
        return {"stage": "absent"}
    validate_row(row)
    return dict(row)


def read_row(
    connection: sqlite3.Connection,
    key: str,
    source_thread_id: str | None = None,
    user_message_id: str | None = None,
) -> sqlite3.Row | None:
    row = connection.execute(
        "SELECT * FROM invocations WHERE invocation_key = ?", (key,)
    ).fetchone()
    if row is not None:
        validate_row(row)
    if source_thread_id is not None and user_message_id is not None:
        identity_row = connection.execute(
            "SELECT * FROM invocations "
            "WHERE source_thread_id = ? AND user_message_id = ?",
            (source_thread_id, user_message_id),
        ).fetchone()
        if identity_row is not None:
            validate_row(identity_row)
        if (row is None) != (identity_row is None) or (
            row is not None
            and identity_row is not None
            and row["invocation_key"] != identity_row["invocation_key"]
        ):
            raise StateError("invocation key and canonical identity disagree")
    return row


def status(
    db_path: Path, source_thread_id: str, user_message_id: str
) -> dict[str, object]:
    key = invocation_key(source_thread_id, user_message_id)
    with connect(db_path) as connection:
        row = read_row(
            connection, key, source_thread_id, user_message_id
        )
        if row is not None and (
            row["source_thread_id"] != source_thread_id
            or row["user_message_id"] != user_message_id
        ):
            raise StateError("invocation key collision")
        result = row_dict(row)
    result["invocation_key"] = key
    result["canonical_identity"] = canonical_identity(
        source_thread_id, user_message_id
    )
    return result


def reserve_assessment(
    db_path: Path,
    source_thread_id: str,
    user_message_id: str,
    snapshot_path: Path,
) -> dict[str, object]:
    snapshot_path, snapshot_sha256 = file_sha256(
        snapshot_path, SNAPSHOT_DIR_RE, "route-brief.md", "snapshot_path"
    )
    key = invocation_key(source_thread_id, user_message_id)
    connection = connect(db_path)
    try:
        begin(connection)
        row = read_row(
            connection, key, source_thread_id, user_message_id
        )
        reserved = row is None
        if reserved:
            timestamp = now()
            connection.execute(
                """
                INSERT INTO invocations (
                    invocation_key, source_thread_id, user_message_id,
                    snapshot_sha256, snapshot_path, stage, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'assessment_reserved', ?, ?)
                """,
                (
                    key,
                    source_thread_id,
                    user_message_id,
                    snapshot_sha256,
                    str(snapshot_path),
                    timestamp,
                    timestamp,
                ),
            )
            row = read_row(connection, key)
        elif (
            row["source_thread_id"] != source_thread_id
            or row["user_message_id"] != user_message_id
            or row["snapshot_sha256"] != snapshot_sha256
            or row["snapshot_path"] != str(snapshot_path)
        ):
            raise StateError("existing invocation does not match this snapshot")
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    result = row_dict(row)
    result["reserved"] = reserved
    return result


def record_assessment(
    db_path: Path,
    key: str,
    snapshot_path: Path,
    create_packet_path: Path,
    route: str,
) -> dict[str, object]:
    key = require_sha256(key, "invocation_key")
    snapshot_path, snapshot_sha256 = file_sha256(
        snapshot_path, SNAPSHOT_DIR_RE, "route-brief.md", "snapshot_path"
    )
    create_packet_path = validate_temp_file_path(
        create_packet_path,
        PACKET_DIR_RE,
        "create-packet.json",
        "create_packet_path",
    )
    if route not in ROUTES:
        raise StateError(f"unsupported route: {route!r}")
    connection = connect(db_path)
    try:
        begin(connection)
        row = read_row(connection, key)
        if (
            row is None
            or row["snapshot_sha256"] != snapshot_sha256
            or row["snapshot_path"] != str(snapshot_path)
        ):
            raise StateError("assessment reservation not found for this snapshot")
        if row["stage"] == "assessment_reserved":
            validate_packet_preparation_path(create_packet_path)
            connection.execute(
                """
                UPDATE invocations
                SET stage = 'assessed', route = ?, create_packet_path = ?,
                    updated_at = ?
                WHERE invocation_key = ?
                """,
                (route, str(create_packet_path), now(), key),
            )
        elif (
            row["route"] != route
            or row["create_packet_path"] != str(create_packet_path)
            or row["stage"] not in {"assessed", "create_reserved", "created"}
        ):
            raise StateError("assessment result conflicts with stored state")
        row = read_row(connection, key)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return row_dict(row)


def reserve_create(
    db_path: Path, key: str, snapshot_path: Path, create_packet_path: Path
) -> dict[str, object]:
    key = require_sha256(key, "invocation_key")
    snapshot_path, snapshot_sha256 = file_sha256(
        snapshot_path, SNAPSHOT_DIR_RE, "route-brief.md", "snapshot_path"
    )
    create_packet_path, create_packet_sha256 = file_sha256(
        create_packet_path,
        PACKET_DIR_RE,
        "create-packet.json",
        "create_packet_path",
    )
    connection = connect(db_path)
    try:
        begin(connection)
        row = read_row(connection, key)
        if (
            row is None
            or row["snapshot_sha256"] != snapshot_sha256
            or row["snapshot_path"] != str(snapshot_path)
            or row["create_packet_path"] != str(create_packet_path)
        ):
            raise StateError("assessed invocation not found for this snapshot")
        reserved = row["stage"] == "assessed"
        if reserved:
            connection.execute(
                """
                UPDATE invocations SET
                    stage = 'create_reserved',
                    create_packet_sha256 = ?,
                    updated_at = ?
                WHERE invocation_key = ?
                """,
                (create_packet_sha256, now(), key),
            )
        elif (
            row["stage"] not in {"create_reserved", "created"}
            or row["create_packet_sha256"] != create_packet_sha256
            or row["create_packet_path"] != str(create_packet_path)
        ):
            raise StateError("create reservation conflicts with stored state")
        row = read_row(connection, key)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    result = row_dict(row)
    result["reserved"] = reserved
    return result


def record_created(
    db_path: Path,
    key: str,
    create_packet_sha256: str,
    destination_kind: str,
    destination_id: str,
) -> dict[str, object]:
    key = require_sha256(key, "invocation_key")
    create_packet_sha256 = require_sha256(
        create_packet_sha256, "create_packet_sha256"
    )
    destination_id = require_identifier(destination_id, "destination_id")
    if destination_kind not in {"threadId", "clientThreadId"}:
        raise StateError("unsupported destination kind")
    connection = connect(db_path)
    try:
        begin(connection)
        row = read_row(connection, key)
        if (
            row is None
            or row["create_packet_sha256"] != create_packet_sha256
        ):
            raise StateError("create reservation not found for this packet")
        if row["stage"] == "create_reserved":
            connection.execute(
                """
                UPDATE invocations
                SET stage = 'created', destination_kind = ?, destination_id = ?,
                    updated_at = ?
                WHERE invocation_key = ?
                """,
                (destination_kind, destination_id, now(), key),
            )
        elif (
            row["stage"] != "created"
            or row["destination_kind"] != destination_kind
            or row["destination_id"] != destination_id
        ):
            raise StateError("created result conflicts with stored state")
        row = read_row(connection, key)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return row_dict(row)


def clean_temp_directory(
    directory: Path, pattern: re.Pattern[str], name: str
) -> bool:
    directory = Path(directory)
    if (
        not directory.is_absolute()
        or directory.parent.resolve() != Path("/tmp").resolve()
        or pattern.fullmatch(directory.name) is None
    ):
        raise StateError(f"invalid {name}")
    try:
        directory_stat = directory.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StateError(f"{name} is unavailable: {exc}") from exc
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or stat.S_ISLNK(directory_stat.st_mode)
        or directory_stat.st_uid != os.getuid()
    ):
        raise StateError(f"{name} must be an owned directory, not a symlink")
    require_safe_cleanup()
    shutil.rmtree(directory)
    return True


def clean_snapshot(snapshot_path: Path) -> bool:
    snapshot_path = validate_temp_file_path(
        snapshot_path, SNAPSHOT_DIR_RE, "route-brief.md", "snapshot_path"
    )
    return clean_temp_directory(
        snapshot_path.parent, SNAPSHOT_DIR_RE, "snapshot directory"
    )


def clean_snapshot_if_idle(snapshot_path: Path) -> bool:
    if not snapshot_path.parent.exists():
        return False
    with assessor_lease(snapshot_path):
        return clean_snapshot(snapshot_path)


def clean_packet(packet_dir: Path) -> bool:
    return clean_temp_directory(packet_dir, PACKET_DIR_RE, "packet directory")


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="auto-dispatch-state-test.") as temp_dir:
        db_path = Path(temp_dir) / "state.sqlite3"
        source_thread_id = "thread-123"
        user_message_id = "item-456"
        temporary_directories: list[Path] = []

        def allocate_directory(prefix: str) -> Path:
            alphabet = string.ascii_letters + string.digits
            for _ in range(100):
                directory = Path("/tmp") / (
                    prefix + "".join(secrets.choice(alphabet) for _ in range(6))
                )
                try:
                    directory.mkdir(mode=0o700)
                except FileExistsError:
                    continue
                temporary_directories.append(directory)
                return directory
            raise StateError("could not allocate self-test directory")

        def expect_state_error(action) -> None:
            try:
                action()
            except StateError:
                return
            raise AssertionError("expected state operation to fail closed")

        def race(action):
            barrier = threading.Barrier(8)
            results: list[bool] = []
            errors: list[Exception] = []

            def contender() -> None:
                try:
                    barrier.wait()
                    results.append(bool(action()["reserved"]))
                except Exception as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            threads = [threading.Thread(target=contender) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            return results, errors

        snapshot_dir = allocate_directory("auto-dispatch.")
        snapshot_path = snapshot_dir / "route-brief.md"
        snapshot_path.write_text("assessment snapshot\n", encoding="utf-8")
        packet_dir = allocate_directory("auto-dispatch-create.")
        packet_path = packet_dir / "create-packet.json"

        try:
            connection = connect(db_path)
            connection.close()
            assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

            shared_parent = Path(temp_dir) / "shared-state"
            shared_parent.mkdir(mode=0o755)
            os.chmod(shared_parent, 0o755)
            shared_db = shared_parent / "auto-dispatch.sqlite3"
            connect(shared_db).close()
            assert stat.S_IMODE(shared_parent.stat().st_mode) == 0o755
            assert stat.S_IMODE(shared_db.stat().st_mode) == 0o600

            public_snapshot_dir = allocate_directory("auto-dispatch.")
            public_snapshot_path = public_snapshot_dir / "route-brief.md"
            public_snapshot_path.write_text("not private\n", encoding="utf-8")
            os.chmod(public_snapshot_dir, 0o755)
            expect_state_error(
                lambda: file_sha256(
                    public_snapshot_path,
                    SNAPSHOT_DIR_RE,
                    "route-brief.md",
                    "snapshot_path",
                )
            )
            expect_state_error(
                lambda: clean_snapshot_if_idle(public_snapshot_path)
            )
            assert not (public_snapshot_dir / ".assessor.lock").exists()
            os.chmod(public_snapshot_dir, 0o700)

            public_packet_dir = allocate_directory("auto-dispatch-create.")
            public_packet_path = public_packet_dir / "create-packet.json"
            os.chmod(public_packet_dir, 0o755)
            expect_state_error(
                lambda: validate_packet_preparation_path(public_packet_path)
            )
            os.chmod(public_packet_dir, 0o700)

            results, errors = race(
                lambda: reserve_assessment(
                    db_path, source_thread_id, user_message_id, snapshot_path
                )
            )
            assert not errors, errors
            assert sum(results) == 1, results

            state = status(db_path, source_thread_id, user_message_id)
            key = str(state["invocation_key"])
            assert state["stage"] == "assessment_reserved"
            assert state["snapshot_path"] == str(snapshot_path)
            assert record_assessment(
                db_path,
                key,
                snapshot_path,
                packet_path,
                "terra-medium",
            )["stage"] == "assessed"
            packet_path.write_text("{}\n", encoding="utf-8")
            assert record_assessment(
                db_path,
                key,
                snapshot_path,
                packet_path,
                "terra-medium",
            )["stage"] == "assessed"
            snapshot_bytes = snapshot_path.read_bytes()
            snapshot_path.write_bytes(snapshot_bytes + b"changed\n")
            expect_state_error(
                lambda: reserve_create(
                    db_path, key, snapshot_path, packet_path
                )
            )
            snapshot_path.write_bytes(snapshot_bytes)
            results, errors = race(
                lambda: reserve_create(db_path, key, snapshot_path, packet_path)
            )
            assert not errors, errors
            assert sum(results) == 1, results
            repeated = reserve_create(
                db_path, key, snapshot_path, packet_path
            )
            assert repeated["reserved"] is False
            packet_sha256 = str(repeated["create_packet_sha256"])
            created = record_created(
                db_path,
                key,
                packet_sha256,
                "threadId",
                "thread-destination",
            )
            assert created["stage"] == "created"
            assert created["destination_id"] == "thread-destination"
            assert record_created(
                db_path,
                key,
                packet_sha256,
                "threadId",
                "thread-destination",
            )["stage"] == "created"

            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "UPDATE invocations SET invocation_key = ? "
                    "WHERE invocation_key = ?",
                    ("c" * 64, key),
                )
            expect_state_error(
                lambda: status(db_path, source_thread_id, user_message_id)
            )
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "UPDATE invocations SET invocation_key = ? "
                    "WHERE source_thread_id = ? AND user_message_id = ?",
                    (key, source_thread_id, user_message_id),
                )

            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "UPDATE invocations SET destination_kind = 'invalid' "
                    "WHERE invocation_key = ?",
                    (key,),
                )
            expect_state_error(
                lambda: status(db_path, source_thread_id, user_message_id)
            )

            dropped_db = Path(temp_dir) / "dropped.sqlite3"
            connect(dropped_db).close()
            with sqlite3.connect(dropped_db) as connection:
                connection.execute("DROP TABLE invocations")
            expect_state_error(
                lambda: status(dropped_db, source_thread_id, user_message_id)
            )
            with sqlite3.connect(dropped_db) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM sqlite_schema "
                    "WHERE type = 'table' AND name = 'invocations'"
                ).fetchone()[0] == 0

            foreign_db = Path(temp_dir) / "foreign.sqlite3"
            with sqlite3.connect(foreign_db) as connection:
                connection.execute("CREATE TABLE foreign_data (value TEXT)")
            foreign_before = foreign_db.read_bytes()
            expect_state_error(lambda: connect(foreign_db))
            assert foreign_db.read_bytes() == foreign_before

            empty_db = Path(temp_dir) / "preexisting-empty.sqlite3"
            with sqlite3.connect(empty_db) as connection:
                connection.execute("CREATE TABLE scratch (value TEXT)")
                connection.execute("DROP TABLE scratch")
            empty_before = empty_db.read_bytes()
            expect_state_error(lambda: connect(empty_db))
            assert empty_db.read_bytes() == empty_before

            wrong_id_db = Path(temp_dir) / "wrong-application-id.sqlite3"
            with sqlite3.connect(wrong_id_db) as connection:
                connection.execute(CREATE_TABLE_SQL)
                connection.execute(
                    f"PRAGMA application_id = {APPLICATION_ID + 1}"
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            wrong_id_before = wrong_id_db.read_bytes()
            expect_state_error(lambda: connect(wrong_id_db))
            assert wrong_id_db.read_bytes() == wrong_id_before

            trigger_db = Path(temp_dir) / "trigger.sqlite3"
            connect(trigger_db).close()
            with sqlite3.connect(trigger_db) as connection:
                connection.execute(
                    "CREATE TRIGGER sqliteXtrigger AFTER INSERT ON invocations "
                    "BEGIN UPDATE invocations SET route = 'sol-max'; END"
                )
            trigger_before = trigger_db.read_bytes()
            expect_state_error(lambda: connect(trigger_db))
            assert trigger_db.read_bytes() == trigger_before

            view_db = Path(temp_dir) / "view.sqlite3"
            connect(view_db).close()
            with sqlite3.connect(view_db) as connection:
                connection.execute(
                    "CREATE VIEW sqliteXview AS "
                    "SELECT invocation_key FROM invocations"
                )
            view_before = view_db.read_bytes()
            expect_state_error(lambda: connect(view_db))
            assert view_db.read_bytes() == view_before

            table_db = Path(temp_dir) / "table.sqlite3"
            connect(table_db).close()
            with sqlite3.connect(table_db) as connection:
                connection.execute("CREATE TABLE sqliteXtable (value TEXT)")
            table_before = table_db.read_bytes()
            expect_state_error(lambda: connect(table_db))
            assert table_db.read_bytes() == table_before

            previous_codex_home = os.environ.get("CODEX_HOME")
            had_codex_home = "CODEX_HOME" in os.environ
            try:
                os.environ["CODEX_HOME"] = ""
                assert default_db_path() == (
                    Path.home() / ".codex/state/auto-dispatch.sqlite3"
                )
                os.environ["CODEX_HOME"] = "relative-profile"
                expect_state_error(default_db_path)
            finally:
                if had_codex_home:
                    os.environ["CODEX_HOME"] = previous_codex_home or ""
                else:
                    os.environ.pop("CODEX_HOME", None)

            symlink_target = allocate_directory("auto-dispatch-create.")
            (symlink_target / "sentinel").write_text("keep\n", encoding="utf-8")
            symlink_path = allocate_directory("auto-dispatch-create.")
            symlink_path.rmdir()
            symlink_path.symlink_to(symlink_target, target_is_directory=True)
            expect_state_error(lambda: clean_packet(symlink_path))
            assert (symlink_target / "sentinel").read_text() == "keep\n"
            symlink_path.unlink()
            clean_packet(symlink_target)

            reserved_snapshot_dir = allocate_directory("auto-dispatch.")
            reserved_snapshot_path = reserved_snapshot_dir / "route-brief.md"
            reserved_snapshot_path.write_text("reserved\n", encoding="utf-8")
            reserved_db = Path(temp_dir) / "reserved.sqlite3"
            reserve_assessment(
                reserved_db,
                "thread-reserved",
                "message-reserved",
                reserved_snapshot_path,
            )
            assert clean_snapshot_if_idle(reserved_snapshot_path) is True
            assert not reserved_snapshot_dir.exists()

            leased_snapshot_dir = allocate_directory("auto-dispatch.")
            leased_snapshot_path = leased_snapshot_dir / "route-brief.md"
            leased_snapshot_path.write_text("leased\n", encoding="utf-8")
            with assessor_lease(leased_snapshot_path):
                expect_state_error(
                    lambda: clean_snapshot_if_idle(leased_snapshot_path)
                )
            assert clean_snapshot_if_idle(leased_snapshot_path) is True

            assert clean_snapshot(snapshot_path) is True
            assert clean_snapshot(snapshot_path) is False
            assert clean_packet(packet_dir) is True
            assert clean_packet(packet_dir) is False
            assert not snapshot_dir.exists()
            assert not packet_dir.exists()
        finally:
            for directory in temporary_directories:
                if directory.is_symlink():
                    directory.unlink()
                elif directory.exists():
                    shutil.rmtree(directory)
    print("state self-test: OK")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--db", type=Path)
    subparsers = result.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--source-thread-id", required=True)
    status_parser.add_argument("--user-message-id", required=True)

    reserve_assessment_parser = subparsers.add_parser("reserve-assessment")
    reserve_assessment_parser.add_argument("--source-thread-id", required=True)
    reserve_assessment_parser.add_argument("--user-message-id", required=True)
    reserve_assessment_parser.add_argument(
        "--snapshot-path", required=True, type=Path
    )

    record_assessment_parser = subparsers.add_parser("record-assessment")
    record_assessment_parser.add_argument("--invocation-key", required=True)
    record_assessment_parser.add_argument("--snapshot-path", required=True, type=Path)
    record_assessment_parser.add_argument(
        "--create-packet-path", required=True, type=Path
    )
    record_assessment_parser.add_argument("--route", required=True)

    reserve_create_parser = subparsers.add_parser("reserve-create")
    reserve_create_parser.add_argument("--invocation-key", required=True)
    reserve_create_parser.add_argument("--snapshot-path", required=True, type=Path)
    reserve_create_parser.add_argument(
        "--create-packet-path", required=True, type=Path
    )

    record_created_parser = subparsers.add_parser("record-created")
    record_created_parser.add_argument("--invocation-key", required=True)
    record_created_parser.add_argument(
        "--create-packet-sha256", required=True
    )
    destination_group = record_created_parser.add_mutually_exclusive_group(
        required=True
    )
    destination_group.add_argument("--thread-id")
    destination_group.add_argument("--client-thread-id")

    clean_parser = subparsers.add_parser("clean-packet")
    clean_parser.add_argument("--packet-dir", required=True, type=Path)
    clean_snapshot_parser = subparsers.add_parser("clean-snapshot")
    clean_snapshot_parser.add_argument("--snapshot-path", required=True, type=Path)

    subparsers.add_parser("self-test")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "clean-packet":
            packet_dir = args.packet_dir.absolute()
            result = {
                "cleaned": clean_packet(packet_dir),
                "path": str(packet_dir),
            }
        elif args.command == "clean-snapshot":
            snapshot_path = args.snapshot_path.absolute()
            result = {
                "cleaned": clean_snapshot_if_idle(snapshot_path),
                "path": str(snapshot_path.parent),
            }
        elif args.command == "self-test":
            self_test()
            return 0
        else:
            db_path = args.db if args.db is not None else default_db_path()
            if args.command == "status":
                result = status(
                    db_path, args.source_thread_id, args.user_message_id
                )
            elif args.command == "reserve-assessment":
                result = reserve_assessment(
                    db_path,
                    args.source_thread_id,
                    args.user_message_id,
                    args.snapshot_path,
                )
            elif args.command == "record-assessment":
                result = record_assessment(
                    db_path,
                    args.invocation_key,
                    args.snapshot_path,
                    args.create_packet_path,
                    args.route,
                )
            elif args.command == "reserve-create":
                result = reserve_create(
                    db_path,
                    args.invocation_key,
                    args.snapshot_path,
                    args.create_packet_path,
                )
            else:
                if args.thread_id is not None:
                    destination_kind = "threadId"
                    destination_id = args.thread_id
                else:
                    destination_kind = "clientThreadId"
                    destination_id = args.client_thread_id
                result = record_created(
                    db_path,
                    args.invocation_key,
                    args.create_packet_sha256,
                    destination_kind,
                    destination_id,
                )
    except (OSError, sqlite3.Error, StateError) as exc:
        print(f"auto-dispatch state: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
