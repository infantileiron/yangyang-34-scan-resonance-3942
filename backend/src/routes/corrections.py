from fastapi import APIRouter, Depends
from sqlalchemy import select

from src.constants.enums import Role
from src.models.base import Session
from src.models.workflow import CorrectionBatch, CorrectionItem
from src.services.auth import current_user, roles
from src.services.corrections import batch_view, create_batch, verify_batch
from src.services.errors import BusinessError

router = APIRouter(prefix='/api/corrections')
editor = roles(Role.ANALYST, Role.SUPERVISOR)
verifier = roles(Role.SUPERVISOR)


def _items(db, batch_id):
    return list(db.scalars(select(CorrectionItem).where(CorrectionItem.batch_id == batch_id)
                           .order_by(CorrectionItem.id)))


@router.get('')
def index(user=Depends(current_user)):
    with Session() as db:
        batches = db.scalars(select(CorrectionBatch).order_by(CorrectionBatch.id.desc()).limit(100))
        return [batch_view(batch, _items(db, batch.id)) for batch in batches]


@router.get('/{batch_id}')
def detail(batch_id: int, user=Depends(current_user)):
    with Session() as db:
        batch = db.get(CorrectionBatch, batch_id)
        if batch is None:
            raise BusinessError('补正批次不存在', 404)
        return batch_view(batch, _items(db, batch.id))


@router.post('', status_code=201)
def create(payload: dict, user=Depends(editor)):
    with Session.begin() as db:
        batch, created = create_batch(db, payload, user['sub'])
        return batch_view(batch, _items(db, batch.id))


@router.post('/{batch_id}/verify', status_code=200)
def verify(batch_id: int, user=Depends(verifier)):
    with Session.begin() as db:
        batch = verify_batch(db, batch_id, user['sub'])
        return batch_view(batch, _items(db, batch.id))
