"""Preserve the pre-upgrade SQLite state and execute schema changes without implicit commits."""
import hashlib
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .data_paths import data_directory


def add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Skip existing columns; propagate every other SQL failure to the transaction owner."""
    if column not in {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def execute_schema(conn: sqlite3.Connection, source: str) -> None:
    """Execute internal schema SQL in the caller's transaction, unlike executescript()."""
    statement = ''
    for line in source.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ''
    if statement.strip():
        raise ValueError('Schema SQL ends with an incomplete statement')


def backup_before_upgrade(path: Path, version: int, target: int) -> Path:
    """Snapshot committed pages while the caller holds a reserved lock, before any writes."""
    folder = data_directory() / 'backups' / 'upgrades'
    folder.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
    fd, name = tempfile.mkstemp(prefix=f'{path.stem}-{identity}-v{version}-to-v{target}-',
                                suffix='.pending', dir=folder)
    os.close(fd)
    staged = Path(name)
    snapshot = staged.with_suffix('.sqlite3')
    published = False
    try:
        # Use a separate reader: backing up the connection with an open write
        # transaction would wait on itself. The reserved lock excludes writers.
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(staged)) as destination:
                source.backup(destination)
                if destination.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('Pre-upgrade SQLite snapshot failed integrity_check')
        with staged.open('rb') as stream:
            os.fsync(stream.fileno())
        # Publish exclusively only after validation; a crash during copying
        # leaves a .pending file, never a plausible completed snapshot.
        os.link(staged, snapshot)
        published = True
        staged.unlink()
        if os.name == 'posix':
            directory_fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        staged.unlink(missing_ok=True)
        if published:
            snapshot.unlink(missing_ok=True)
        raise
    return snapshot
