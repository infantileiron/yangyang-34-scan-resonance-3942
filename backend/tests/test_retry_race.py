import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models.base import Base, Session, engine  # noqa: E402
from src.services import corrections  # noqa: E402
from src.services.observations import add_observations  # noqa: E402

PAYLOAD = {'title': '同一批', 'items': [{'desk': 'A台', 'scan': 'A-1', 'amplitude': 10.6, 'streak': 5.4}]}


def _reset():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session.begin() as db:
        add_observations(db, [
            {'desk': 'A台', 'scan': 'A-1', 'amplitude': 10.0, 'streak': 5.0},
            {'desk': 'A台', 'scan': 'A-2', 'amplitude': 11.0, 'streak': 6.0},
        ], 'analyst@demo.local', 'observation.created')


def test_concurrent_same_batch_returns_existing(monkeypatch):
    """并发提交同一批次：后到者读到空，却在写唯一键时撞车，应取回已受理的批次。"""
    _reset()
    with Session.begin() as db:
        first, created = corrections.create_batch(db, PAYLOAD, 'analyst@demo.local')
        first_id = first.id
    assert created is True

    original = corrections._existing_batch
    seen = {'count': 0}

    def stale_read(db, source_key):
        seen['count'] += 1
        if seen['count'] == 1:  # 后到者的第一次查询发生在第一批提交之前
            return None
        return original(db, source_key)

    monkeypatch.setattr(corrections, '_existing_batch', stale_read)
    with Session.begin() as db:
        second, again = corrections.create_batch(db, PAYLOAD, 'analyst@demo.local')
        second_id = second.id
    assert again is False
    assert second_id == first_id
    with Session() as db:
        batches = list(db.scalars(corrections.select(corrections.CorrectionBatch)))
        items = list(db.scalars(corrections.select(corrections.CorrectionItem)))
    assert len(batches) == 1
    assert len(items) == 1  # 没有新增有效测次
