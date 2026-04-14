import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Collection, CollectionItem, Report, User, WorkspaceMember
from app.schemas import CollectionCreate, CollectionUpdate, CollectionItemCreate
from app.services.auth import get_current_user

router = APIRouter(prefix="/api/collections", tags=["collections"])


async def _check_collection_access(collection: Collection, user: User, db: AsyncSession, require_owner: bool = False):
    """Raise 404/403 if user cannot access the collection."""
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    if collection.user_id == user.id:
        return
    if require_owner:
        raise HTTPException(status_code=403, detail="Only the collection owner can do this")
    # Check workspace membership
    if collection.workspace_id:
        result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == collection.workspace_id,
                WorkspaceMember.user_id == user.id,
            )
        )
        if result.scalar_one_or_none():
            return
    raise HTTPException(status_code=404, detail="Collection not found")


@router.get("")
async def list_collections(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Personal collections
    personal_q = select(Collection).where(
        Collection.user_id == user.id,
        Collection.workspace_id.is_(None),
    )

    # Workspace collections the user can see
    ws_ids_q = select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
    workspace_q = select(Collection).where(Collection.workspace_id.in_(ws_ids_q))

    result = await db.execute(
        select(Collection)
        .where(Collection.id.in_(select(personal_q.subquery().c.id).union(select(workspace_q.subquery().c.id))))
        .order_by(Collection.created_at.desc())
    )
    collections = result.scalars().all()

    # Get item counts
    coll_ids = [c.id for c in collections]
    counts = {}
    if coll_ids:
        count_result = await db.execute(
            select(CollectionItem.collection_id, func.count(CollectionItem.id))
            .where(CollectionItem.collection_id.in_(coll_ids))
            .group_by(CollectionItem.collection_id)
        )
        counts = dict(count_result.all())

    return [
        {
            "id": c.id,
            "workspace_id": c.workspace_id,
            "user_id": c.user_id,
            "name": c.name,
            "description": c.description,
            "item_count": counts.get(c.id, 0),
            "created_at": c.created_at,
        }
        for c in collections
    ]


@router.post("")
async def create_collection(
    data: CollectionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify workspace membership if workspace_id provided
    if data.workspace_id:
        result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == data.workspace_id,
                WorkspaceMember.user_id == user.id,
            )
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="Not a member of this workspace")

    collection = Collection(
        user_id=user.id,
        workspace_id=data.workspace_id,
        name=data.name,
        description=data.description,
    )
    db.add(collection)
    await db.commit()
    await db.refresh(collection)

    return {
        "id": collection.id,
        "workspace_id": collection.workspace_id,
        "user_id": collection.user_id,
        "name": collection.name,
        "description": collection.description,
        "item_count": 0,
        "created_at": collection.created_at,
    }


@router.get("/{collection_id}")
async def get_collection(
    collection_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    collection = await db.get(Collection, collection_id)
    await _check_collection_access(collection, user, db)

    # Get items with report info
    result = await db.execute(
        select(CollectionItem, Report)
        .join(Report, Report.id == CollectionItem.report_id)
        .where(CollectionItem.collection_id == collection_id)
        .order_by(CollectionItem.created_at.desc())
    )
    items = [
        {
            "id": item.id,
            "collection_id": item.collection_id,
            "report_id": item.report_id,
            "scout_id": report.scout_id,
            "report_title": report.title,
            "report_summary": report.summary[:300] if report.summary else None,
            "note": item.note,
            "created_at": item.created_at,
        }
        for item, report in result.all()
    ]

    return {
        "id": collection.id,
        "workspace_id": collection.workspace_id,
        "user_id": collection.user_id,
        "name": collection.name,
        "description": collection.description,
        "item_count": len(items),
        "created_at": collection.created_at,
        "items": items,
    }


@router.put("/{collection_id}")
async def update_collection(
    collection_id: uuid.UUID,
    data: CollectionUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    collection = await db.get(Collection, collection_id)
    await _check_collection_access(collection, user, db, require_owner=True)

    if data.name is not None:
        collection.name = data.name
    if data.description is not None:
        collection.description = data.description
    await db.commit()
    await db.refresh(collection)

    return {
        "id": collection.id,
        "workspace_id": collection.workspace_id,
        "user_id": collection.user_id,
        "name": collection.name,
        "description": collection.description,
        "created_at": collection.created_at,
    }


@router.delete("/{collection_id}")
async def delete_collection(
    collection_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    collection = await db.get(Collection, collection_id)
    await _check_collection_access(collection, user, db, require_owner=True)
    await db.delete(collection)
    await db.commit()
    return {"detail": "Collection deleted"}


@router.post("/{collection_id}/items")
async def add_item(
    collection_id: uuid.UUID,
    data: CollectionItemCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    collection = await db.get(Collection, collection_id)
    await _check_collection_access(collection, user, db)

    # Verify report exists
    report = await db.get(Report, data.report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    # Check not already in collection
    existing = await db.execute(
        select(CollectionItem).where(
            CollectionItem.collection_id == collection_id,
            CollectionItem.report_id == data.report_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Report already in this collection")

    item = CollectionItem(
        collection_id=collection_id,
        report_id=data.report_id,
        note=data.note,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)

    return {
        "id": item.id,
        "collection_id": item.collection_id,
        "report_id": item.report_id,
        "scout_id": report.scout_id,
        "report_title": report.title,
        "report_summary": report.summary[:300] if report.summary else None,
        "note": item.note,
        "created_at": item.created_at,
    }


@router.delete("/{collection_id}/items/{item_id}")
async def remove_item(
    collection_id: uuid.UUID,
    item_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    collection = await db.get(Collection, collection_id)
    await _check_collection_access(collection, user, db)

    item = await db.get(CollectionItem, item_id)
    if not item or item.collection_id != collection_id:
        raise HTTPException(status_code=404, detail="Item not found")
    await db.delete(item)
    await db.commit()
    return {"detail": "Item removed"}
