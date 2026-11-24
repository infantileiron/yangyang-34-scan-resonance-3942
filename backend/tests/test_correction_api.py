import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.base import Base, Session, engine
from src.services import corrections
from src.services.observations import add_observations


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app) as client:
        yield client


def login(client, email, password):
    response = client.post('/api/auth/login', json={'email': email, 'password': password})
    assert response.status_code == 200, response.text
    return {'Authorization': f'Bearer {response.json()["token"]}'}


def row(desk, scan, amplitude, streak):
    return {'desk': desk, 'scan': scan, 'amplitude': amplitude, 'streak': streak}


@pytest.fixture()
def ledger():
    with Session.begin() as db:
        add_observations(db, [
            row('窗边一号台', 'CB-01-01', 6.0, 12.0), row('窗边一号台', 'CB-01-02', 7.0, 13.0),
            row('避振二号台', 'BZ-02-01', 16.0, 9.0), row('避振二号台', 'BZ-02-02', 17.0, 10.0),
        ], 'analyst@demo.local', 'observation.created')


def test_correction_api_lifecycle(client, ledger):
    analyst = login(client, 'analyst@demo.local', 'Analyst-2026!')
    supervisor = login(client, 'supervisor@demo.local', 'Supervisor-2026!')
    viewer = login(client, 'viewer@demo.local', 'Viewer-2026!')

    payload = {'title': '窗边补正', 'note': '复测', 'items': [
        row('窗边一号台', 'CB-01-01', 6.4, 12.4),
        row('窗边一号台', 'CB-01-03', 8.0, 14.0),
    ]}
    created = client.post('/api/corrections', json=payload, headers=analyst)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body['status'] == 'pending'
    assert '尚未登记' in body['reason']
    batch_id = body['id']

    # 相同批次重试不新增
    again = client.post('/api/corrections', json=payload, headers=analyst)
    assert again.json()['id'] == batch_id
    listed = client.get('/api/corrections', headers=analyst).json()
    assert len(listed) == 1

    # 只读账号不能提交补正
    denied = client.post('/api/corrections', json=payload, headers=viewer)
    assert denied.status_code == 403

    # 补齐缺失测次后自动重核
    added = client.post('/api/observations', json=row('窗边一号台', 'CB-01-03', 8.0, 14.0), headers=analyst)
    assert added.status_code == 201, added.text
    detail = client.get(f'/api/corrections/{batch_id}', headers=analyst).json()
    assert detail['status'] == 'applied'

    # 台账显示补正后的有效值
    observations = client.get('/api/observations?limit=100', headers=analyst).json()['items']
    target = next(item for item in observations if item['data']['scan'] == 'CB-01-01')
    assert target['effective']['amplitude'] == 6.4

    # 发起分析，结果能查到用过的补正批次
    run = client.post('/api/runs', json={'title': '复核实测', 'parameters': {}}, headers=analyst)
    assert run.status_code == 201, run.text
    run_id = run.json()['id']
    assert run.json()['correction_batches'][0]['id'] == batch_id
    result = client.get(f'/api/runs/{run_id}', headers=analyst).json()
    assert result['correction_batches'][0]['measurements'] == 2


def test_concurrent_conflict_reason_visible_through_api(client, ledger):
    analyst = login(client, 'analyst@demo.local', 'Analyst-2026!')
    with Session.begin() as db:
        from src.models.workflow import CorrectionBatch
        from src.services.corrections import CorrectionItem, _observation_index, batch_key
        observation = _observation_index(db)[('避振二号台', 'BZ-02-01')][0]
        staged = []
        for title, amplitude, streak in (('甲', 16.5, 9.5), ('乙', 16.9, 9.1)):
            batch = CorrectionBatch(source_key=batch_key(title, '', []) + title, title=title,
                                    actor='analyst@demo.local', status='pending',
                                    verified_by='analyst@demo.local')
            db.add(batch)
            db.flush()
            db.add(CorrectionItem(batch_id=batch.id, desk='避振二号台', scan='BZ-02-01',
                                  amplitude=amplitude, streak=streak, observation_id=observation.id,
                                  old_amplitude=observation.payload['amplitude'],
                                  old_streak=observation.payload['streak']))
            db.flush()
            staged.append(batch.id)
    # 两批都在旧值 16.0 上登记，先后核对时只有一批被接纳
    with Session.begin() as db:
        assert corrections.verify_batch(db, staged[0], 'supervisor@demo.local').status == 'applied'
    with Session.begin() as db:
        corrections.verify_batch(db, staged[1], 'supervisor@demo.local')
    detail = client.get(f'/api/corrections/{staged[1]}', headers=analyst).json()
    assert detail['status'] == 'conflicted'
    assert f'#{staged[0]}' in detail['reason']
    assert detail['reason']


def test_supervisor_can_verify_pending_batch(client, ledger):
    analyst = login(client, 'analyst@demo.local', 'Analyst-2026!')
    supervisor = login(client, 'supervisor@demo.local', 'Supervisor-2026!')
    batch = client.post('/api/corrections', json={
        'title': '待补', 'items': [row('避振二号台', 'BZ-02-09', 18.0, 11.0)]}, headers=analyst).json()
    assert batch['status'] == 'pending'
    blocked = client.post(f"/api/corrections/{batch['id']}/verify", json={}, headers=analyst)
    assert blocked.status_code == 403
    still = client.post(f"/api/corrections/{batch['id']}/verify", json={}, headers=supervisor).json()
    assert still['status'] == 'pending'
    client.post('/api/observations', json=row('避振二号台', 'BZ-02-09', 18.0, 11.0), headers=analyst)
    done = client.post(f"/api/corrections/{batch['id']}/verify", json={}, headers=supervisor).json()
    assert done['status'] == 'applied'
    audit = client.get('/api/reports/audit', headers=supervisor).json()
    assert any('补正批次' in entry['detail'] for entry in audit)


def test_invalid_batch_is_rejected_with_row_context(client, ledger):
    analyst = login(client, 'analyst@demo.local', 'Analyst-2026!')
    response = client.post('/api/corrections', json={
        'title': '坏批', 'items': [row('窗边一号台', 'CB-01-01', -1, 12.0)]}, headers=analyst)
    assert response.status_code == 422
    assert '第 1 条补正' in response.json()['detail']
