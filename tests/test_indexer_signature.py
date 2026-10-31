import time

from agent_conversation_search.indexer import _signature


def test_sqlite_signature_ignores_shm_and_empty_wal(tmp_path):
    db = tmp_path / "x.db"
    db.write_bytes(b"db")
    shm = tmp_path / "x.db-shm"
    shm.write_bytes(b"")
    wal = tmp_path / "x.db-wal"
    wal.write_bytes(b"")

    before = _signature(db, "sqlite")
    time.sleep(0.01)
    shm.write_bytes(b"touched")
    wal.touch()
    after = _signature(db, "sqlite")
    assert before == after

    wal.write_bytes(b"x" * 100)
    changed = _signature(db, "sqlite")
    assert changed[0] > before[0]