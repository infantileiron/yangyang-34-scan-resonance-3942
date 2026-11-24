"""成批补正扫描台的共振幅值和纹影长度。

补正批次是独立于原始观测的台账：原始观测保持不动，整批核对通过后才写入「当前
有效值」。同一测次任一时刻最多只有一个生效补正，靠 effective_key 唯一约束兜底；
核对时对本批目标观测加行锁，保证并发补正同一旧值只有一批被接纳，另一批留下待
复核原因。
"""

import hashlib
import json
from collections import defaultdict

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from src.config.project import SPEC
from src.constants.enums import BatchStatus
from src.models.base import utc_now
from src.models.workflow import AuditEvent, CorrectionBatch, CorrectionItem, Observation
from src.services.errors import BusinessError
from src.services.validation import validated
from src.utils.formatting import timestamp

FIELDS = {field['key']: field for field in SPEC['fields']}
LINE_FIELDS = tuple(FIELDS[key] for key in ('desk', 'scan', 'amplitude', 'streak'))
MEASURES = ('amplitude', 'streak')
MAX_ITEMS = 500
STATUS_LABEL = {
    BatchStatus.PENDING: '暂挂待补',
    BatchStatus.APPLIED: '已生效',
    BatchStatus.CONFLICTED: '待复核',
}


def measure_key(desk, scan):
    return 'm:' + hashlib.sha256(json.dumps([desk, scan], ensure_ascii=False).encode()).hexdigest()


def batch_key(title, note, rows):
    payload = {
        'title': title,
        'note': note,
        'items': sorted(({key: row[key] for key in ('desk', 'scan', 'amplitude', 'streak')} for row in rows),
                        key=lambda row: (row['desk'], row['scan'])),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return 'batch:' + digest


def _clean_text(value, label, limit):
    if not isinstance(value, str) or not value.strip():
        raise BusinessError(f'请填写{label}')
    value = value.strip()
    if len(value) > limit:
        raise BusinessError(f'{label}不能超过 {limit} 个字符')
    return value


def clean_request(payload):
    if not isinstance(payload, dict):
        raise BusinessError('补正批次格式不正确')
    extra = set(payload) - {'title', 'note', 'items'}
    if extra:
        raise BusinessError(f'无法识别字段 {", ".join(sorted(extra))}')
    title = _clean_text(payload.get('title'), '补正批次名称', 120)
    note = payload.get('note') or ''
    if not isinstance(note, str) or len(note.strip()) > 300:
        raise BusinessError('补正说明不能超过 300 个字符')
    items = payload.get('items')
    if not isinstance(items, list) or not items:
        raise BusinessError('补正批次至少需要一条测次')
    if len(items) > MAX_ITEMS:
        raise BusinessError(f'单个补正批次最多 {MAX_ITEMS} 条测次')
    rows, seen = [], set()
    for index, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            raise BusinessError(f'第 {index} 条补正格式不正确')
        extra = set(raw) - {'desk', 'scan', 'amplitude', 'streak'}
        if extra:
            raise BusinessError(f'第 {index} 条补正含无法识别字段 {", ".join(sorted(extra))}')
        try:
            row = validated(LINE_FIELDS, raw)
        except BusinessError as exc:
            raise BusinessError(f'第 {index} 条补正：{exc}') from exc
        if min(row['amplitude'], row['streak']) < 0:
            raise BusinessError(f'第 {index} 条补正：共振幅值和纹影长度均不能为负')
        key = row['desk'], row['scan']
        if key in seen:
            raise BusinessError(f'第 {index} 条补正重复：{row["desk"]} / {row["scan"]}')
        seen.add(key)
        rows.append(row)
    return title, note.strip(), rows


def _observation_index(db):
    index = defaultdict(list)
    for observation in db.scalars(select(Observation).order_by(Observation.id)):
        payload = observation.payload or {}
        index[(payload.get('desk'), payload.get('scan'))].append(observation)
    return index


def _effective_items(db, observation_ids, *, lock=False):
    if not observation_ids:
        return {}
    query = select(CorrectionItem).where(
        CorrectionItem.observation_id.in_(observation_ids),
        CorrectionItem.effective_key.is_not(None),
    ).order_by(CorrectionItem.id)
    if lock:
        query = query.with_for_update()
    return {item.observation_id: item for item in db.scalars(query)}


def _close(left, right):
    return abs(left - right) <= 1e-9 * max(1.0, abs(left), abs(right))


def _current_values(observation, effective):
    item = effective.get(observation.id)
    if item is not None:
        return item.amplitude, item.streak
    return observation.payload['amplitude'], observation.payload['streak']


def _mark(db, batch, status, reason):
    reason = reason[:300]
    changed = batch.status != status or batch.reason != reason
    batch.status = status
    batch.reason = reason
    batch.verified_at = utc_now()
    if changed:
        action = 'correction.pending' if status == BatchStatus.PENDING else 'correction.conflicted'
        db.add(AuditEvent(actor=batch.verified_by or batch.actor, action=action,
                          detail=f'补正批次 #{batch.id}：{reason}'[:200]))
    return batch


def _evaluate(db, batch):
    """整批核对：全部测次都对上旧值才整批生效，否则整批暂挂或标待复核。"""
    items = list(db.scalars(select(CorrectionItem).where(CorrectionItem.batch_id == batch.id)
                            .order_by(CorrectionItem.id)))
    index = _observation_index(db)
    targets, missing, duplicated = [], [], []
    for item in items:
        matches = index.get((item.desk, item.scan), [])
        if not matches:
            missing.append(f'{item.desk} / {item.scan}')
        elif len(matches) > 1:
            duplicated.append(f'{item.desk} / {item.scan}')
        else:
            targets.append((item, matches[0]))

    if duplicated:
        return _mark(db, batch, BatchStatus.CONFLICTED,
                     '原测次重复登记，无法确定补正对象：' + '；'.join(duplicated[:6]) + '；请核对台账后重新提交。')

    observation_ids = sorted({observation.id for _, observation in targets})
    locked = {row.id: row for row in db.scalars(
        select(Observation).where(Observation.id.in_(observation_ids)).order_by(Observation.id).with_for_update())}
    effective = _effective_items(db, observation_ids, lock=True)

    conflicts = []
    for item, observation in targets:
        observation = locked.get(observation.id, observation)
        effective_item = effective.get(observation.id)
        amplitude, streak = _current_values(observation, effective)
        if item.old_amplitude is None or item.old_streak is None:
            # 创建时原测次尚未登记，旧值以补登的原始登记值为准，而不是当时已生效的补正。
            item.old_amplitude = observation.payload['amplitude']
            item.old_streak = observation.payload['streak']
        if not (_close(item.old_amplitude, amplitude) and _close(item.old_streak, streak)):
            # 本批声明的旧值已不再是当前有效值：并发下同一旧值已被另一批抢先接纳。
            source = f'批次 #{effective_item.batch_id} ' if effective_item is not None else '其他登记 '
            conflicts.append(f'{item.desk} / {item.scan} 旧值 {item.old_amplitude:g}/{item.old_streak:g} '
                             f'已被{source}补正为 {amplitude:g}/{streak:g}')
            continue
        item.observation_id = observation.id

    if conflicts:
        return _mark(db, batch, BatchStatus.CONFLICTED,
                     '；'.join(conflicts[:6]) + '；本批整批未写入，保留待复核。')

    if missing:
        return _mark(db, batch, BatchStatus.PENDING,
                     '原测次尚未登记：' + '；'.join(missing[:6]) + '；补齐后再整批核对，期间分析照常使用原值。')

    for item, observation in targets:
        previous = effective.get(observation.id)
        if previous is not None and previous.id != item.id:
            previous.effective_key = None
        item.observation_id = observation.id
        item.effective_key = measure_key(item.desk, item.scan)

    batch.status = BatchStatus.APPLIED
    batch.reason = None
    batch.verified_by = batch.verified_by or batch.actor
    batch.verified_at = utc_now()
    db.add(AuditEvent(actor=batch.verified_by, action='correction.applied',
                      detail=f'补正批次 #{batch.id} 整批生效 {len(targets)} 条测次'))
    return batch


def _commit_batch(db, batch):
    """整批核对并把结果落库。

    写有效值用保存点保护：若并发下同一旧值已被另一批抢先写入，唯一约束报错时只回滚
    本批的写入，再把本批标成待复核，让「另一批的待复核原因」照样查得到。
    """
    batch_id = batch.id
    try:
        with db.begin_nested():
            _evaluate(db, batch)
            db.flush()
    except IntegrityError:
        db.expire_all()
        batch = db.get(CorrectionBatch, batch_id, with_for_update=True)
        items = list(db.scalars(select(CorrectionItem).where(CorrectionItem.batch_id == batch_id)))
        keys = [measure_key(item.desk, item.scan) for item in items]
        previous = db.scalar(select(CorrectionItem.batch_id).where(
            CorrectionItem.effective_key.in_(keys),
            CorrectionItem.batch_id != batch_id).limit(1)) if keys else None
        source = f'批次 #{previous}' if previous is not None else '另一批'
        _mark(db, batch, BatchStatus.CONFLICTED,
              f'并发补正同一旧值：已被{source}接纳，本批整批未写入，保留待复核。')
        db.flush()
    return batch


def _existing_batch(db, source_key):
    return db.scalar(select(CorrectionBatch).where(CorrectionBatch.source_key == source_key))


def create_batch(db, payload, actor):
    title, note, rows = clean_request(payload)
    source_key = batch_key(title, note, rows)
    existing = _existing_batch(db, source_key)
    if existing is not None:
        # 相同批次重试：返回原批次，不重复登记，也不新增有效测次。
        if existing.status != BatchStatus.APPLIED:
            _commit_batch(db, existing)
        return existing, False

    batch = CorrectionBatch(source_key=source_key, title=title, note=note, actor=actor,
                            status=BatchStatus.PENDING, verified_by=actor)
    try:
        # 保存点包住批次行写入：唯一键冲突只回滚这一行，再取回已受理的批次。
        with db.begin_nested():
            db.add(batch)
            db.flush([batch])
    except IntegrityError:
        if batch in db:
            db.expunge(batch)
        existing = _existing_batch(db, source_key)
        if existing is None:
            raise
        return existing, False
    index = _observation_index(db)
    matched = {}
    for row in rows:
        matches = index.get((row['desk'], row['scan']), [])
        if len(matches) == 1:
            matched[(row['desk'], row['scan'])] = matches[0]
    effective = _effective_items(db, [observation.id for observation in matched.values()])
    for row in rows:
        observation = matched.get((row['desk'], row['scan']))
        current = effective.get(observation.id) if observation is not None else None
        db.add(CorrectionItem(
            batch_id=batch.id, desk=row['desk'], scan=row['scan'],
            amplitude=row['amplitude'], streak=row['streak'],
            observation_id=observation.id if observation is not None else None,
            old_amplitude=(current.amplitude if current else observation.payload['amplitude'])
            if observation is not None else None,
            old_streak=(current.streak if current else observation.payload['streak'])
            if observation is not None else None,
        ))

    db.add(AuditEvent(actor=actor, action='correction.created',
                      detail=f'补正批次 #{batch.id}，{len(rows)} 条测次'))
    try:
        db.flush()
    except IntegrityError as exc:
        raise BusinessError('补正批次写入冲突，请刷新后重试', 409) from exc
    return _commit_batch(db, batch), True


def verify_batch(db, batch_id, actor):
    batch = db.get(CorrectionBatch, batch_id, with_for_update=True)
    if batch is None:
        raise BusinessError('补正批次不存在', 404)
    if batch.status != BatchStatus.APPLIED:
        batch.verified_by = actor
        return _commit_batch(db, batch)
    return batch


def recheck_pending(db, keys):
    """补登原测次后，重核引用这些测次的暂挂批次。"""
    keys = set(keys)
    if not keys:
        return 0
    applied = 0
    for batch in list(db.scalars(select(CorrectionBatch).where(CorrectionBatch.status == BatchStatus.PENDING)
                                 .order_by(CorrectionBatch.id).with_for_update())):
        targets = db.scalar(select(CorrectionItem.id).where(
            CorrectionItem.batch_id == batch.id,
            or_(*[and_(CorrectionItem.desk == desk, CorrectionItem.scan == scan) for desk, scan in keys]),
        ).limit(1))
        if targets is None:
            continue
        _commit_batch(db, batch)
        applied += batch.status == BatchStatus.APPLIED
    db.flush()
    return applied


def effective_snapshot(db):
    """按发起时的有效补正值生成分析快照，并记录用到的补正批次。"""
    observations = list(db.scalars(select(Observation).order_by(Observation.id)))
    effective = _effective_items(db, [row.id for row in observations])
    rows, used = [], defaultdict(int)
    for observation in observations:
        item = effective.get(observation.id)
        payload = dict(observation.payload)
        if item is not None:
            for key in MEASURES:
                payload[key] = getattr(item, key)
            used[item.batch_id] += 1
        rows.append(payload)
    return rows, _batch_usage(db, used)


def _batch_usage(db, used):
    if not used:
        return []
    titles = {row.id: row for row in db.scalars(select(CorrectionBatch).where(CorrectionBatch.id.in_(used)))}
    return [{'id': batch_id, 'title': titles[batch_id].title if batch_id in titles else '',
             'measurements': count}
            for batch_id, count in sorted(used.items())]


def effective_by_observation(db, observations):
    """给台账补上当前有效值和来源补正批次。"""
    effective = _effective_items(db, [row.id for row in observations])
    batch_ids = {item.batch_id for item in effective.values()}
    labels = {row.id: row.title for row in db.scalars(
        select(CorrectionBatch).where(CorrectionBatch.id.in_(batch_ids)))} if batch_ids else {}
    return {item_id: {'amplitude': item.amplitude, 'streak': item.streak,
                      'batch_id': item.batch_id, 'batch_title': labels.get(item.batch_id, '')}
            for item_id, item in effective.items()}


def batch_view(batch, items=None):
    view = {
        'id': batch.id, 'title': batch.title, 'note': batch.note, 'actor': batch.actor,
        'status': batch.status, 'status_label': STATUS_LABEL.get(batch.status, batch.status),
        'reason': batch.reason, 'created_at': timestamp(batch.created_at),
        'verified_by': batch.verified_by, 'verified_at': timestamp(batch.verified_at),
    }
    if items is not None:
        view['items'] = [{
            'desk': item.desk, 'scan': item.scan,
            'amplitude': item.amplitude, 'streak': item.streak,
            'old_amplitude': item.old_amplitude, 'old_streak': item.old_streak,
            'effective': item.effective_key is not None,
            'observation_id': item.observation_id,
        } for item in items]
        view['item_count'] = len(items)
    return view
