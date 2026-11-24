import json
import logging

from sqlalchemy import select

from src.config.project import SPEC
from src.constants.enums import Decision, Role, RunStatus
from src.domain.analysis import analyze
from src.models.base import utc_now
from src.models.workflow import AnalysisRun, AnalysisSnapshot, AuditEvent, Observation, Review
from src.services import corrections
from src.services.errors import BusinessError
from src.services.validation import validated

logger = logging.getLogger(__name__)


def create_run(db, request, actor):
    parameters = validated(SPEC['parameters'], request.parameters, defaults=True)
    observations = list(db.scalars(select(Observation).order_by(Observation.id)))
    if not observations:
        raise BusinessError('请先导入观测记录')
    # 按发起时的有效测次固化快照：同一批补正要么整批用新值，要么整批用旧值。
    snapshot, batches = corrections.effective_snapshot(db)
    item = AnalysisRun(title=request.title, owner=actor, parameters=parameters,
                       observation_ids=[row.id for row in observations],
                       last_observation_id=observations[-1].id, sample_size=len(observations))
    db.add(item)
    db.flush()
    db.add(AnalysisSnapshot(run_id=item.id, rows=snapshot, batches=batches))
    detail = f'分析 #{item.id}，固化 {item.sample_size} 条观测'
    if batches:
        detail += '，补正批次 ' + '、'.join(f'#{batch["id"]}' for batch in batches)
    db.add(AuditEvent(actor=actor, action='run.created', detail=detail[:200]))
    db.flush()
    return item


def visible_runs(db, user, run_id=None):
    query = (select(AnalysisRun, Review, AnalysisSnapshot)
             .outerjoin(Review, Review.run_id == AnalysisRun.id)
             .outerjoin(AnalysisSnapshot, AnalysisSnapshot.run_id == AnalysisRun.id))
    if user['role'] == Role.VIEWER:
        query = query.where(Review.decision == Decision.APPROVED)
    if run_id is not None:
        row = db.execute(query.where(AnalysisRun.id == run_id)).first()
        if row is None:
            raise BusinessError('没有可查看的分析结果', 404)
        return row
    return db.execute(query.order_by(AnalysisRun.id.desc()).limit(100)).all()


def submit_review(db, run_id, request, actor):
    row = db.get(AnalysisRun, run_id, with_for_update=True)
    if row is None:
        raise BusinessError('分析不存在', 404)
    if row.status != RunStatus.SUCCEEDED:
        raise BusinessError('分析算完后才能复核', 409)
    if db.scalar(select(Review).where(Review.run_id == run_id)):
        raise BusinessError('这份分析已完成复核。请发起新分析并重新复核。', 409)
    if row.owner == actor:
        raise BusinessError('分析须由另一位负责人复核', 409)
    review = Review(run_id=run_id, decision=request.decision, note=request.note, reviewer=actor)
    db.add(review)
    db.add(AuditEvent(actor=actor, action=f'review.{request.decision}', detail=f'分析 #{run_id}'))
    db.flush()
    return review


def run_pending(db):
    item = db.scalar(select(AnalysisRun).where(AnalysisRun.status == RunStatus.QUEUED)
                     .order_by(AnalysisRun.id).limit(1).with_for_update(skip_locked=True))
    if item is None:
        return False
    try:
        snapshot = db.scalar(select(AnalysisSnapshot).where(AnalysisSnapshot.run_id == item.id))
        sample = snapshot.rows if snapshot is not None else list(db.scalars(
            select(Observation.payload).where(Observation.id.in_(item.observation_ids)).order_by(Observation.id)))
        result = analyze(sample, item.parameters)
        if not {'headline', 'summary', 'metrics', 'columns', 'rows'} <= set(result):
            raise RuntimeError('invalid result schema')
        json.dumps(result, allow_nan=False, ensure_ascii=False)
        item.result = result
        item.status = RunStatus.SUCCEEDED
    except ValueError as exc:
        item.error = str(exc)[:600] or '本次观测数据不足以完成计算'
        item.status = RunStatus.FAILED
    except Exception:
        logger.exception('Analysis %s raised unexpectedly', item.id)
        item.error = '分析计算失败，请检查记录和参数后重试'
        item.status = RunStatus.FAILED
    item.finished_at = utc_now()
    return True
