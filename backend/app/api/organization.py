"""Organization management API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin, get_current_user
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db
from app.models.user import Department, User
from app.schemas.schemas import DepartmentCreate, DepartmentOut, DepartmentTree, UserOut, UserUpdate

router = APIRouter(prefix="/org", tags=["organization"])


# ─── Departments ────────────────────────────────────────


@router.get("/departments", response_model=list[DepartmentTree])
async def get_department_tree(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get full department tree."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await db.execute(
        select(Department).where(Department.tenant_id == target_tenant_id).order_by(Department.sort_order)
    )
    departments = [dept for dept in result.scalars().all() if getattr(dept, "tenant_id", None) == target_tenant_id]

    # Build tree
    dept_map: dict[uuid.UUID, DepartmentTree] = {}
    roots: list[DepartmentTree] = []

    for dept in departments:
        # Count members
        member_count_result = await db.execute(
            select(func.count(User.id)).where(
                User.department_id == dept.id,
                User.tenant_id == target_tenant_id,
            )
        )
        member_count = member_count_result.scalar() or 0

        node = DepartmentTree(
            id=dept.id,
            name=dept.name,
            parent_id=dept.parent_id,
            manager_id=dept.manager_id,
            sort_order=dept.sort_order,
            created_at=dept.created_at,
            member_count=member_count,
        )
        dept_map[dept.id] = node

    for node in dept_map.values():
        if node.parent_id and node.parent_id in dept_map:
            dept_map[node.parent_id].children.append(node)
        else:
            roots.append(node)

    return roots


@router.post("/departments", response_model=DepartmentOut, status_code=status.HTTP_201_CREATED)
async def create_department(
    data: DepartmentCreate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new department (admin only)."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    dept = Department(
        tenant_id=target_tenant_id,
        name=data.name,
        parent_id=data.parent_id,
        manager_id=data.manager_id,
    )
    db.add(dept)
    await db.flush()
    return DepartmentOut.model_validate(dept)


@router.patch("/departments/{dept_id}", response_model=DepartmentOut)
async def update_department(
    dept_id: uuid.UUID,
    data: DepartmentCreate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a department."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await db.execute(
        select(Department).where(
            Department.id == dept_id,
            Department.tenant_id == target_tenant_id,
        )
    )
    dept = result.scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")

    dept.name = data.name
    if data.parent_id is not None:
        dept.parent_id = data.parent_id
    if data.manager_id is not None:
        dept.manager_id = data.manager_id
    await db.flush()
    return DepartmentOut.model_validate(dept)


@router.delete("/departments/{dept_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_department(
    dept_id: uuid.UUID,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a department (admin only)."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await db.execute(
        select(Department).where(
            Department.id == dept_id,
            Department.tenant_id == target_tenant_id,
        )
    )
    dept = result.scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    await db.delete(dept)


# ─── Users Management ──────────────────────────────────


@router.get("/users", response_model=list[UserOut])
async def list_users(
    department_id: uuid.UUID | None = None,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List users, optionally filtered by department."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    query = select(User).where(User.is_active, User.tenant_id == target_tenant_id)
    if department_id:
        query = query.where(User.department_id == department_id)
    query = query.order_by(User.display_name)

    result = await db.execute(query)
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@router.patch("/users/{user_id}", response_model=UserOut)
async def admin_update_user(
    user_id: uuid.UUID,
    data: UserUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin update user profile (role, department). Tenant-scoped."""
    result = await db.execute(select(User).where(User.id == user_id, User.tenant_id == current_user.tenant_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await db.flush()
    return UserOut.model_validate(user)
