import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.models.base import Base, Session, engine  # noqa: E402
from src.models.workflow import AnalysisRun, AnalysisSnapshot, AuditEvent, Observation  # noqa: E402
from src.services import corrections  # noqa: E402
from src.services.observations import add_observations  # noqa: E402
from src.services.runs import create_run  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


def observation(desk, scan, amplitude, streak):
    return {'desk': desk, 'scan': scan, 'amplitude': amplitude, 'streak': streak}


def seed(rows):
    with Session.begin() as db:
        add_observations(db, rows, 'analyst@demo.local', 'observation.created')


def batch_payload(title, items, note=''):
    return {'title': title, 'note': note, 'items': items}


def submit(title, items):
    with Session.begin() as db:
        batch, created = corrections.create_batch(db, batch_payload(title, items), 'analyst@demo.local')
        return batch.id, batch.status, created


def load(batch_id):
    with Session() as db:
        batch = db.get(corrections.CorrectionBatch, batch_id)
        items = list(db.scalars(corrections.select(corrections.CorrectionItem)
                                .where(corrections.CorrectionItem.batch_id == batch_id)
                                .order_by(corrections.CorrectionItem.id)))
        return corrections.batch_view(batch, items)


def stage(title, items):
    """登记一个补正批次但先不核对，模拟另一个并发请求也读到同一旧值。"""
    with Session.begin() as db:
        payload = batch_payload(title, items)
        from src.services.corrections import clean_request, batch_key
        clean_title, note, rows = clean_request(payload)
        batch = corrections.CorrectionBatch(
            source_key=batch_key(clean_title, note, rows) + f':{title}',
            title=clean_title, note=note, actor='analyst@demo.local',
            status='pending', verified_by='analyst@demo.local')
        db.add(batch)
        db.flush()
        index = corrections._observation_index(db)
        for row in rows:
            matches = index.get((row['desk'], row['scan']), [])
            observation = matches[0] if len(matches) == 1 else None
            db.add(corrections.CorrectionItem(
                batch_id=batch.id, desk=row['desk'], scan=row['scan'],
                amplitude=row['amplitude'], streak=row['streak'],
                observation_id=observation.id if observation else None,
                old_amplitude=observation.payload['amplitude'] if observation else None,
                old_streak=observation.payload['streak'] if observation else None))
        db.flush()
        return batch.id


def commit(batch_id):
    with Session.begin() as db:
        batch = db.get(corrections.CorrectionBatch, batch_id)
        corrections._commit_batch(db, batch)
        return batch.status


def effective():
    with Session.begin() as db:
        rows, batches = corrections.effective_snapshot(db)
    return {(row['desk'], row['scan']): row for row in rows}, batches


def test_batch_with_missing_measurement_is_pending_then_applies(tmp_path):
    seed([observation('矿物三号台', 'SK-03-X01', 12.0, 6.0),
          observation('矿物三号台', 'SK-03-X02', 13.0, 7.0)])
    batch_id, status, created = submit('三号台补正', [
        observation('矿物三号台', 'SK-03-X01', 12.4, 6.8),
        observation('矿物三号台', 'SK-03-X03', 15.0, 8.0),
    ])
    assert created is True
    assert status == 'pending'
    view = load(batch_id)
    assert '尚未登记' in view['reason']
    rows, batches = effective()
    assert rows[('矿物三号台', 'SK-03-X01')]['amplitude'] == 12.0
    assert batches == []

    # 补齐缺失测次后整批生效
    seed([observation('矿物三号台', 'SK-03-X03', 15.0, 8.0)])
    view = load(batch_id)
    assert view['status'] == 'applied'
    rows, batches = effective()
    assert rows[('矿物三号台', 'SK-03-X01')]['amplitude'] == 12.4
    assert rows[('矿物三号台', 'SK-03-X03')]['streak'] == 8.0
    assert batches[0]['id'] == batch_id
    assert batches[0]['measurements'] == 2


def test_concurrent_same_old_value_only_one_accepted():
    seed([observation('避振二号台', 'BZ-02-01', 10.0, 5.0),
          observation('避振二号台', 'BZ-02-02', 11.0, 6.0)])
    # 两个请求都在旧值 10.0 上登记，之后才先后核对
    first_id = stage('补正甲', [observation('避振二号台', 'BZ-02-01', 10.6, 5.4)])
    second_id = stage('补正乙', [observation('避振二号台', 'BZ-02-01', 10.9, 5.1)])
    assert commit(first_id) == 'applied'
    assert commit(second_id) == 'conflicted'
    reason = load(second_id)['reason']
    assert f'#{first_id}' in reason  # 待复核原因能查到被哪一批接纳
    rows, _ = effective()
    assert rows[('避振二号台', 'BZ-02-01')]['amplitude'] == 10.6


def test_retry_same_batch_does_not_duplicate_measurements():
    seed([observation('窗边一号台', 'CB-01-01', 6.0, 12.0),
          observation('窗边一号台', 'CB-01-02', 6.5, 12.5)])
    payload = [observation('窗边一号台', 'CB-01-01', 6.2, 12.2)]
    first_id, first_status, created = submit('窗边补正', payload)
    second_id, second_status, again = submit('窗边补正', payload)
    assert first_id == second_id
    assert created is True and again is False
    with Session() as db:
        items = list(db.scalars(corrections.select(corrections.CorrectionItem)
                                .where(corrections.CorrectionItem.batch_id == first_id)))
        batches = list(db.scalars(corrections.select(corrections.CorrectionBatch)))
    assert len(items) == 1
    assert len(batches) == 1
    assert first_status == second_status == 'applied'


def test_run_snapshot_records_batch_and_ignores_later_corrections():
    seed([observation('窗边一号台', 'CB-01-01', 6.0, 12.0),
          observation('窗边一号台', 'CB-01-02', 7.0, 13.0),
          observation('避振二号台', 'BZ-02-01', 16.0, 9.0),
          observation('避振二号台', 'BZ-02-02', 17.0, 10.0)])
    batch_id, status, _ = submit('窗边补正', [observation('窗边一号台', 'CB-01-01', 6.3, 12.4)])
    assert status == 'applied'

    class Request:
        title = '首次复核'
        parameters = {}

    with Session.begin() as db:
        run = create_run(db, Request(), 'analyst@demo.local')
        run_id = run.id
    with Session() as db:
        first = db.get(AnalysisRun, run_id)
        snapshot = db.scalar(corrections.select(AnalysisSnapshot)
                             .where(AnalysisSnapshot.run_id == run_id))
        assert snapshot.batches[0]['id'] == batch_id
        frozen = {row['scan']: row for row in snapshot.rows}
        assert frozen['CB-01-01']['amplitude'] == 6.3

    # 之后再补正，不影响已经固化的分析
    submit('窗边再补正', [observation('窗边一号台', 'CB-01-01', 9.9, 14.4)])
    with Session() as db:
        snapshot = db.scalar(corrections.select(AnalysisSnapshot)
                             .where(AnalysisSnapshot.run_id == run_id))
        frozen = {row['scan']: row for row in snapshot.rows}
        assert frozen['CB-01-01']['amplitude'] == 6.3


def test_batch_is_all_or_nothing_when_one_line_conflicts():
    seed([observation('固定四号台', 'GD-04-01', 34.0, 4.0),
          observation('固定四号台', 'GD-04-02', 35.0, 5.0)])
    submit('第一批', [observation('固定四号台', 'GD-04-01', 34.5, 4.5)])
    # 第二条测次在旧值 35.0 上，与另一批并发；整批不能只写一半。
    mixed_id = stage('混合批', [
        observation('固定四号台', 'GD-04-02', 35.5, 5.5),
        observation('固定四号台', 'GD-04-01', 36.0, 6.0),
    ])
    assert commit(mixed_id) == 'conflicted'
    rows, batches = effective()
    assert rows[('固定四号台', 'GD-04-02')]['amplitude'] == 35.0  # 未部分写入
    assert rows[('固定四号台', 'GD-04-01')]['amplitude'] == 34.5
    assert all(batch['id'] != mixed_id for batch in batches)
    assert '批次 #' in load(mixed_id)['reason']


def test_verify_endpoint_reevaluates_pending_batch():
    seed([observation('矿物三号台', 'SK-03-X01', 12.0, 6.0),
          observation('矿物三号台', 'SK-03-X02', 13.0, 7.0)])
    batch_id, status, _ = submit('待补批', [
        observation('矿物三号台', 'SK-03-X01', 12.2, 6.2),
        observation('矿物三号台', 'SK-03-X09', 20.0, 9.0),
    ])
    assert status == 'pending'
    seed([observation('矿物三号台', 'SK-03-X09', 20.0, 9.0)])
    with Session.begin() as db:
        batch = corrections.verify_batch(db, batch_id, 'supervisor@demo.local')
        assert batch.status == 'applied'


def test_audit_records_both_batch_outcomes():
    seed([observation('窗边一号台', 'CB-01-01', 6.0, 12.0),
          observation('窗边一号台', 'CB-01-02', 6.5, 12.5)])
    first_id = stage('甲批', [observation('窗边一号台', 'CB-01-01', 7.1, 13.1)])
    second_id = stage('乙批', [observation('窗边一号台', 'CB-01-01', 6.6, 12.6)])
    assert commit(first_id) == 'applied'
    assert commit(second_id) == 'conflicted'
    with Session() as db:
        events = list(db.scalars(corrections.select(AuditEvent).order_by(AuditEvent.id)))
    actions = [event.action for event in events]
    assert 'correction.applied' in actions
    assert 'correction.conflicted' in actions
    conflicted = [event for event in events if event.action == 'correction.conflicted']
    assert any(f'#{second_id}' in event.detail for event in conflicted)
    applied = [event for event in events if event.action == 'correction.applied']
    assert any(f'#{first_id}' in event.detail for event in applied)


def test_duplicate_original_registration_marks_batch_conflicted():
    seed([observation('窗边一号台', 'CB-01-01', 6.0, 12.0),
          observation('窗边一号台', 'CB-01-02', 6.5, 12.5)])
    with Session.begin() as db:
        db.add(Observation(source_key='manual-dup', payload=observation('窗边一号台', 'CB-01-01', 6.0, 12.0)))
    batch_id, status, _ = submit('重复批', [observation('窗边一号台', 'CB-01-01', 6.4, 12.1)])
    assert status == 'conflicted'
    assert '重复登记' in load(batch_id)['reason']


def test_worker_computes_from_frozen_snapshot_with_all_new_values():
    from src.services.runs import run_pending

    seed([observation('窗边一号台', 'CB-01-01', 6.0, 12.0),
          observation('窗边一号台', 'CB-01-02', 7.0, 13.0),
          observation('避振二号台', 'BZ-02-01', 16.0, 9.0),
          observation('避振二号台', 'BZ-02-02', 17.0, 10.0),
          observation('固定四号台', 'GD-04-01', 34.0, 4.0),
          observation('固定四号台', 'GD-04-02', 35.0, 5.0)])
    # 整批同时补正两个台的多个测次
    first, status, _ = submit('整批', [
        observation('窗边一号台', 'CB-01-01', 6.8, 12.9),
        observation('窗边一号台', 'CB-01-02', 7.8, 13.9),
        observation('避振二号台', 'BZ-02-01', 16.7, 9.6),
        observation('避振二号台', 'BZ-02-02', 17.7, 10.6),
    ])
    assert status == 'applied'

    class Request:
        title = '整批复核'
        parameters = {}

    with Session.begin() as db:
        run = create_run(db, Request(), 'analyst@demo.local')
        run_id = run.id
        snapshot = db.scalar(corrections.select(AnalysisSnapshot)
                             .where(AnalysisSnapshot.run_id == run_id))
        values = {(row['desk'], row['scan']): row['amplitude'] for row in snapshot.rows}
    # 同一批补正要么全用新值，要么全用旧值
    assert values[('窗边一号台', 'CB-01-01')] == 6.8
    assert values[('窗边一号台', 'CB-01-02')] == 7.8
    assert values[('避振二号台', 'BZ-02-01')] == 16.7
    assert values[('避振二号台', 'BZ-02-02')] == 17.7
    assert values[('固定四号台', 'GD-04-01')] == 34.0  # 未被补正的测次保持原值

    with Session.begin() as db:
        assert run_pending(db) is True
    with Session() as db:
        stored = db.get(AnalysisRun, run_id)
        assert stored.status == 'succeeded'
        summary = stored.result['summary']
        assert '3 张有重复测次的扫描台' in summary


def test_sequential_recorrection_of_new_value_is_allowed():
    # 先补正为 10.6，再以 10.6 为旧值继续补正到 10.9：不算并发冲突，允许生效。
    seed([observation('避振二号台', 'BZ-02-01', 10.0, 5.0),
          observation('避振二号台', 'BZ-02-02', 11.0, 6.0)])
    first_id, first_status, _ = submit('第一轮', [observation('避振二号台', 'BZ-02-01', 10.6, 5.4)])
    assert first_status == 'applied'
    with Session.begin() as db:
        # 后续批次以当前有效值 10.6 为旧值
        rows, _ = corrections.effective_snapshot(db)
        current = next(row for row in rows if row['scan'] == 'BZ-02-01')
        assert current['amplitude'] == 10.6
    batch = corrections.CorrectionBatch(
        source_key='manual-next', title='第二轮', note='', actor='analyst@demo.local',
        status='pending', verified_by='analyst@demo.local')
    with Session.begin() as db:
        db.add(batch)
        db.flush()
        observation_id = next(row.id for row in db.scalars(corrections.select(Observation))
                              if row.payload['scan'] == 'BZ-02-01')
        db.add(corrections.CorrectionItem(
            batch_id=batch.id, desk='避振二号台', scan='BZ-02-01', amplitude=10.9, streak=5.6,
            old_amplitude=10.6, old_streak=5.4, observation_id=observation_id))
        db.flush()
        corrections._commit_batch(db, batch)
        assert batch.status == 'applied'
        second_id = batch.id
    rows, batches = effective()
    assert rows[('避振二号台', 'BZ-02-01')]['amplitude'] == 10.9
    # 第一轮的补正被第二轮取代，生效批次只剩最新的那一批
    assert {batch['id'] for batch in batches} == {second_id}
    assert load(first_id)['status'] == 'applied'


def test_unique_key_fallback_marks_loser_conflicted(monkeypatch):
    """模拟真正的并发：后一批读到的仍是旧值，写有效值时撞唯一约束，也必须留下原因。"""
    seed([observation('避振二号台', 'BZ-02-01', 10.0, 5.0),
          observation('避振二号台', 'BZ-02-02', 11.0, 6.0)])
    first_id = stage('甲批', [observation('避振二号台', 'BZ-02-01', 10.6, 5.4)])
    second_id = stage('乙批', [observation('避振二号台', 'BZ-02-01', 10.9, 5.1)])
    assert commit(first_id) == 'applied'

    # 让乙批的核对“看不到”甲批已生效，只会在唯一约束处失败，走回退分支。
    original = corrections._effective_items
    monkeypatch.setattr(corrections, '_effective_items',
                        lambda db, ids, lock=False: {} if lock else original(db, ids, lock=lock))
    assert commit(second_id) == 'conflicted'
    view = load(second_id)
    assert f'#{first_id}' in view['reason']
    monkeypatch.setattr(corrections, '_effective_items', original)
