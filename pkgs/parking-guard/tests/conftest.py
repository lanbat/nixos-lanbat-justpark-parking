"""A throwaway PostgreSQL cluster for the tests that touch the store.

The tests run against a real server rather than a stub, because the interesting
parts of ``store.py`` are the SQL itself: the single-statement token claim, the
insert-if-absent incident, and the upsert that refuses to overwrite a known
contact detail with a blank one.

``initdb`` and ``pg_ctl`` come from ``nativeCheckInputs`` in the Nix build. When
they are missing — a bare checkout with no PostgreSQL on PATH — these tests skip
rather than fail, and the rest of the suite still runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from parking_guard.store import Store

CLUSTER_USER = "postgres"


def _have_postgres() -> bool:
    return bool(shutil.which("initdb") and shutil.which("pg_ctl"))


@pytest.fixture(scope="session")
def postgres_cluster(tmp_path_factory) -> str:
    """Start one cluster for the whole session, listening on a unix socket only."""
    if not _have_postgres():
        pytest.skip("PostgreSQL (initdb/pg_ctl) is not on PATH")

    base = tmp_path_factory.mktemp("postgres")
    data_dir = base / "data"
    socket_dir = base / "socket"
    socket_dir.mkdir()
    log_file = base / "postgres.log"

    _run(
        [
            "initdb",
            "--pgdata", str(data_dir),
            "--username", CLUSTER_USER,
            "--encoding", "UTF8",
            "--locale", "C",
            "--auth", "trust",
        ]
    )
    _run(
        [
            "pg_ctl",
            "--pgdata", str(data_dir),
            "--log", str(log_file),
            "--options", f"-k {socket_dir} -c listen_addresses= -c fsync=off",
            "--wait",
            "start",
        ],
        log_file=log_file,
    )

    try:
        yield f"postgresql://{CLUSTER_USER}@/{CLUSTER_USER}?host={socket_dir}"
    finally:
        _run(
            ["pg_ctl", "--pgdata", str(data_dir), "--mode", "immediate", "--wait", "stop"],
            check=False,
        )


def _run(command: list[str], *, check: bool = True, log_file: Path | None = None) -> None:
    env = {**os.environ, "PGTZ": "UTC", "TZ": "UTC"}
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        if log_file is not None and log_file.exists():
            detail = f"{detail}\n{log_file.read_text(encoding='utf-8')}"
        raise RuntimeError(f"{command[0]} failed: {detail}")


@pytest.fixture
def store_factory(postgres_cluster, request):
    """Hand out Stores, each on its own empty database."""
    import psycopg

    created: list[Store] = []
    counter = {"n": 0}
    safe_name = request.node.name.lower()
    safe_name = "".join(character if character.isalnum() else "_" for character in safe_name)[:40]

    def factory() -> Store:
        counter["n"] += 1
        database = f"pg_{safe_name}_{counter['n']}"
        with psycopg.connect(postgres_cluster, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
            admin.execute(f'CREATE DATABASE "{database}" TEMPLATE template0')
        store = Store(postgres_cluster.replace(f"/{CLUSTER_USER}?", f"/{database}?"))
        created.append(store)
        return store

    yield factory

    for store in created:
        store.close()


@pytest.fixture
def store(store_factory) -> Store:
    return store_factory()
