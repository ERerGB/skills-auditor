"""Durable local records, optimistic revisions and append-only event batches.

SQLite FULL synchronous rollback-journal transactions are the local atomicity
boundary. Filesystem effects belong to a separate write-ahead domain protocol;
committing this database cannot make arbitrary external effects atomic.
Checksums detect accidental record corruption, not malicious local rewriting.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Dict, Optional, Union

from .common import LifecycleError, canonical_entry, canonical_json, digest, durable_mkdir, fsync_directory, utc_now
from .locking import locked_paths


_COLUMNS = {
    "metadata": [("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)],
    "records": [("kind", "TEXT", 1, 1), ("id", "TEXT", 1, 2), ("revision", "INTEGER", 1, 0), ("data", "TEXT", 1, 0), ("checksum", "TEXT", 1, 0)],
    "events": [("sequence", "INTEGER", 0, 1), ("stream", "TEXT", 1, 0), ("event_type", "TEXT", 1, 0), ("payload", "TEXT", 1, 0), ("actor", "TEXT", 1, 0), ("tool", "TEXT", 1, 0), ("created_at", "TEXT", 1, 0), ("checksum", "TEXT", 1, 0)],
}
_TRIGGERS = {
    "events_no_update": "CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END",
    "events_no_delete": "CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END",
}


def _label(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 512 and not any(ord(char) < 32 for char in value)


def _database_error(exc: sqlite3.Error) -> LifecycleError:
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        code = "repository_busy"
    elif isinstance(exc, sqlite3.DatabaseError) and not any(word in message for word in ("disk", "readonly", "read-only", "unable to open")):
        code = "repository_corrupt"
    else:
        code = "repository_io_error"
    return LifecycleError(code, "lifecycle repository operation failed", details={"error": str(exc)})


class Repository:
    """Repository rooted at an explicit directory, never a guessed project root.

    ``create=False`` never initializes missing state, so status readers can
    distinguish unknown/uninitialized state from a valid empty repository.
    Instances are confined to one thread (SQLite's default safety policy).

    First creation commits and synchronizes a private staging database before
    no-overwrite publication. A persistent initialization lock is held only
    during creation, before domain registry/target locks, with a bounded wait.
    A killed initializer can leave ``.state-init-*.sqlite3`` artifacts; later
    attempts preserve those unknown-owner files and stage a fresh database.
    An existing final database is always validated, never reset or repaired.
    """

    def __init__(self, root: Union[str, Path], *, create: bool = True):
        self.root = canonical_entry(root)
        self.path = self.root / "state.sqlite3"
        self._connection = None
        self._depth = 0
        if self.root.is_symlink() or self.path.is_symlink():
            raise LifecycleError("unsafe_path", "repository root and database must not be symlinks")
        existed = self.path.exists()
        if not existed and not create:
            raise LifecycleError("repository_missing", "no managed lifecycle repository exists", details={"path": str(self.root)})
        try:
            if create:
                durable_mkdir(self.root)
            if not existed:
                self._initialize_new()
            self._open_database(self.path)
            self._check_schema()
        except sqlite3.Error as exc:
            self.close()
            raise _database_error(exc) from exc
        except OSError as exc:
            self.close()
            raise LifecycleError("repository_io_error", "could not open durable lifecycle storage", details={"error": str(exc)}) from exc
        except BaseException:
            self.close()
            raise

    def _open_database(self, path):
        if path.is_symlink():
            raise LifecycleError("unsafe_path", "repository database must not be a symlink")
        self._connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")

    def _initialize_new(self):
        # A single creator may publish. Readers never see an empty final file;
        # competing creators wait boundedly and then open the published schema.
        with locked_paths([self.root / ".initialize"], timeout=5.0):
            if self.path.exists() or self.path.is_symlink():
                return
            descriptor, name = tempfile.mkstemp(prefix=".state-init-", suffix=".sqlite3", dir=str(self.root))
            staging = Path(name)
            identity = os.fstat(descriptor)
            os.close(descriptor)
            try:
                self._open_database(staging)
                self._connection.execute("PRAGMA journal_mode=DELETE")
                self._initialize()
                self._check_schema()
                self.close()
                with staging.open("rb") as handle:
                    os.fsync(handle.fileno())
                try:
                    # POSIX link is atomic and refuses any existing destination.
                    # SQLite is closed before linking; no live database handle
                    # can still write through the staging filename afterward.
                    os.link(str(staging), str(self.path))
                except FileExistsError:
                    # A noncooperating writer occupied the name: validate it in
                    # the caller, preserving its bytes even if they are corrupt.
                    pass
                fsync_directory(self.root)
            finally:
                self.close()
                try:
                    current = staging.lstat()
                    if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                        staging.unlink()
                        fsync_directory(self.root)
                except OSError:
                    # Cleanup is best effort and never obscures an initialization
                    # or publication error. Never delete a different attempt's
                    # staging file or infer that a final publication was undone.
                    pass

    def _initialize(self):
        with self.atomic():
            self._connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._connection.execute("INSERT INTO metadata VALUES ('schema_version', '1')")
            self._connection.execute("CREATE TABLE records (kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL, checksum TEXT NOT NULL, PRIMARY KEY (kind,id))")
            self._connection.execute("CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, actor TEXT NOT NULL, tool TEXT NOT NULL, created_at TEXT NOT NULL, checksum TEXT NOT NULL)")
            self._connection.execute("CREATE INDEX events_stream ON events(stream,sequence)")
            for statement in _TRIGGERS.values():
                self._connection.execute(statement)

    def _check_schema(self):
        if self._connection.execute("PRAGMA quick_check").fetchall()[0][0] != "ok":
            raise LifecycleError("repository_corrupt", "repository integrity check failed")
        for table, expected in _COLUMNS.items():
            actual = [(row[1], row[2], row[3], row[5]) for row in self._connection.execute("PRAGMA table_info(" + table + ")")]
            if actual != expected:
                raise LifecycleError("repository_corrupt", "repository schema is missing or incompatible", details={"table": table})
        version = self._connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        triggers = {row[0]: row[1] for row in self._connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
        if version is None or version[0] != "1" or any(triggers.get(name) != sql for name, sql in _TRIGGERS.items()):
            raise LifecycleError("repository_corrupt", "repository schema version or append-only guards are invalid")

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    @contextmanager
    def atomic(self):
        """Commit all grouped records/events, or roll back; nesting uses savepoints."""
        depth = self._depth
        savepoint = "lifecycle_" + str(depth)
        try:
            self._connection.execute("BEGIN IMMEDIATE" if depth == 0 else "SAVEPOINT " + savepoint)
        except sqlite3.Error as exc:
            raise _database_error(exc) from exc
        self._depth += 1
        try:
            yield self
            self._connection.execute("COMMIT" if depth == 0 else "RELEASE SAVEPOINT " + savepoint)
        except BaseException as exc:
            try:
                self._connection.execute("ROLLBACK" if depth == 0 else "ROLLBACK TO SAVEPOINT " + savepoint)
                if depth:
                    self._connection.execute("RELEASE SAVEPOINT " + savepoint)
            except sqlite3.Error:
                # A failed commit can have uncertain outcome; never obscure its
                # primary error or manufacture a completed domain transaction.
                pass
            if isinstance(exc, sqlite3.Error):
                raise _database_error(exc) from exc
            raise
        finally:
            self._depth -= 1

    def _query(self, sql, parameters=()):
        try:
            self._check_schema()
            return self._connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise _database_error(exc) from exc

    @staticmethod
    def _record(row) -> Dict[str, Any]:
        try:
            data = json.loads(row["data"])
            record = {"kind": row["kind"], "id": row["id"], "revision": row["revision"], "data": data}
            if not _label(record["kind"]) or not _label(record["id"]) or not isinstance(data, dict) or type(record["revision"]) is not int or record["revision"] < 1 or digest(record) != row["checksum"]:
                raise ValueError("invalid record envelope or checksum")
            return record
        except (TypeError, ValueError, UnicodeError) as exc:
            raise LifecycleError("repository_corrupt", "record could not be validated", details={"kind": row["kind"], "id": row["id"]}) from exc

    def get(self, kind: str, id: str) -> Optional[Dict[str, Any]]:
        if not _label(kind) or not _label(id):
            raise LifecycleError("invalid_record", "record kind and id must be nonempty bounded strings")
        rows = self._query("SELECT * FROM records WHERE kind=? AND id=?", (kind, id))
        return self._record(rows[0]) if rows else None

    def list(self, kind: str):
        if not _label(kind):
            raise LifecycleError("invalid_record", "record kind must be a nonempty bounded string")
        return [self._record(row) for row in self._query("SELECT * FROM records WHERE kind=? ORDER BY id", (kind,))]

    def put(self, kind: str, id: str, data: Dict[str, Any], expected_revision: int = 0) -> Dict[str, Any]:
        if not _label(kind) or not _label(id) or not isinstance(data, dict) or type(expected_revision) is not int or expected_revision < 0:
            raise LifecycleError("invalid_record", "invalid record identity, data or expected revision")
        try:
            encoded = canonical_json(data)
            record = {"kind": kind, "id": id, "revision": expected_revision + 1, "data": json.loads(encoded)}
            checksum = digest(record)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise LifecycleError("invalid_record", "record data must be strict JSON") from exc
        with self.atomic():
            previous = self.get(kind, id)
            actual_revision = previous["revision"] if previous else 0
            if actual_revision != expected_revision:
                raise LifecycleError("revision_conflict", "record changed since the approved revision", details={"kind": kind, "id": id, "expected_revision": expected_revision, "actual_revision": actual_revision})
            self._connection.execute("INSERT OR REPLACE INTO records (kind,id,revision,data,checksum) VALUES (?,?,?,?,?)", (kind, id, record["revision"], encoded, checksum))
        return record

    def append_event(self, stream: str, event_type: str, payload: Dict[str, Any], actor: str, tool: str) -> Dict[str, Any]:
        if not all(_label(value) for value in (stream, event_type, actor, tool)) or not isinstance(payload, dict):
            raise LifecycleError("invalid_record", "invalid event labels or payload")
        try:
            encoded = canonical_json(payload)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise LifecycleError("invalid_record", "event payload must be strict JSON") from exc
        with self.atomic():
            self._check_schema()
            # Select the next sequence under BEGIN IMMEDIATE before writing so
            # the checksum covers the immutable sequence without UPDATE.
            sequence = self._connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events").fetchone()[0]
            event = {"sequence": sequence, "stream": stream, "event_type": event_type, "payload": json.loads(encoded), "actor": actor, "tool": tool, "created_at": utc_now()}
            self._connection.execute("INSERT INTO events (sequence,stream,event_type,payload,actor,tool,created_at,checksum) VALUES (?,?,?,?,?,?,?,?)", (sequence, stream, event_type, encoded, actor, tool, event["created_at"], digest(event)))
        return event

    def events(self, stream: str, *, limit: Optional[int] = None, after_sequence: Optional[int] = None):
        """Read ordered history, optionally by a bounded stable sequence cursor.

        Omitting both options preserves the original full-stream behavior.
        A cursor belongs to the global event sequence, so other streams may
        leave gaps; consumers resume after the last returned sequence.
        """
        if not _label(stream):
            raise LifecycleError("invalid_record", "event stream must be a nonempty bounded string")
        if (limit is not None and (type(limit) is not int or not 1 <= limit <= 1000)
                or after_sequence is not None and (type(after_sequence) is not int or after_sequence < 0)):
            raise LifecycleError("invalid_record", "event limit must be an integer from 1 to 1000 and cursor a nonnegative integer")
        sql = "SELECT * FROM events WHERE stream=?"
        parameters = [stream]
        if after_sequence is not None:
            sql += " AND sequence>?"
            # SQLite event sequences cannot exceed signed 64-bit integers.
            # Larger valid cursors mean an empty page, not an adapter overflow.
            parameters.append(min(after_sequence, 2 ** 63 - 1))
        sql += " ORDER BY sequence"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        events = []
        for row in self._query(sql, parameters):
            try:
                event = {key: row[key] for key in ("sequence", "stream", "event_type", "actor", "tool", "created_at")}
                event["payload"] = json.loads(row["payload"])
                if type(event["sequence"]) is not int or event["sequence"] < 1 or not isinstance(event["payload"], dict) or not all(_label(event[key]) for key in ("stream", "event_type", "actor", "tool", "created_at")) or digest(event) != row["checksum"]:
                    raise ValueError("invalid event envelope or checksum")
                events.append(event)
            except (TypeError, ValueError, UnicodeError) as exc:
                raise LifecycleError("repository_corrupt", "event could not be validated", details={"stream": stream}) from exc
        return events
