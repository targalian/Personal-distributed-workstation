"""Database compatibility: unchanged schema and shared repositories with isolated connections."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lan_mesh.database import Database, SCHEMA_VERSION
from lan_mesh.protocol import Task


def test_repository_schema_matches_pre_migration(tmp_path):
    db=Database(str(tmp_path/'schema.db'))
    expected=json.loads((Path(__file__).parent/'fixtures/database_schema_contract.json').read_text(encoding='utf-8'))
    actual=[list(row) for row in db._get_conn().execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name')]
    assert actual==expected
    assert SCHEMA_VERSION==12


def test_repositories_keep_thread_local_connection_and_compatible_reads(tmp_path):
    db=Database(str(tmp_path/'threads.db'))
    barrier=threading.Barrier(6)

    def write_tasks(index):
        connection=db._get_conn()
        repository=db.repositories.tasks
        barrier.wait(timeout=5)
        for sequence in range(10):
            task=Task(task_id=f'{index}-{sequence}',name=f'task-{index}-{sequence}')
            if sequence%2:db.save_task(task)
            else:repository.save_task(task)
        assert db._get_conn() is connection
        return connection,repository

    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(write_tasks,range(6)))
    assert len({id(conn) for conn,_ in results})==6
    assert len({id(repo) for _,repo in results})==1
    assert len(db.list_tasks(limit=100))==60
    assert db.get_task('2-4')==db.repositories.tasks.get_task('2-4')
    assert 'services' not in vars(db.repositories.tasks)
