"""保留管理 · 清理预览接口：预览生成/回查、对象详情、补归档、撤销快照、提交执行。

路由前缀 /api/retention/cleanup/*，三段以上路径与通用资源路由
（/api/{module}、/api/{module}/{entry_id:int}）不冲突。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api/retention/cleanup", tags=["保留管理·清理预览"])


def _service():
    service = service_for("retention")
    if service is None:
        raise HTTPException(status_code=404, detail="保留管理模块未注册")
    return service


class PreviewPayload(BaseModel):
    policy_id: int


class CredentialPayload(BaseModel):
    表名: str
    分区: str
    归档版本: str
    来源策略: str = "人工补录"


class RevokePayload(BaseModel):
    快照编号: str


class ExecutePayload(BaseModel):
    preview_id: str


@router.post("/preview")
def create_preview(payload: PreviewPayload) -> dict[str, Any]:
    """生成清理预览单：按表列出各对象清理依据，分四组展示暂缓项。"""
    result = _service().cleanup_preview(payload.policy_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "预览生成失败"))
    return result


@router.get("/previews")
def list_previews() -> dict[str, Any]:
    """历史预览单列表。"""
    return _service().list_previews()


@router.get("/previews/{preview_id}")
def get_preview(preview_id: str) -> dict[str, Any]:
    """历史预览回查：按生成时冻结的条件原样返回。"""
    result = _service().get_preview(preview_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason"))
    return result


@router.get("/previews/{preview_id}/objects/{partition}")
def get_object_detail(preview_id: str, partition: str) -> dict[str, Any]:
    """对象详情：关联最近可用快照，呈现当前凭证覆盖的分区。"""
    result = _service().preview_object_detail(preview_id, partition)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason"))
    return result


@router.post("/credentials")
def supplement_credential(payload: CredentialPayload) -> dict[str, Any]:
    """补归档：为指定分区补录已确认归档凭证并回写清理清单。"""
    result = _service().supplement_credential(
        payload.表名, payload.分区, payload.归档版本, payload.来源策略,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason"))
    return result


@router.post("/snapshots/revoke")
def revoke_snapshot(payload: RevokePayload) -> dict[str, Any]:
    """撤销快照：撤销后不再作为任何对象的恢复依据。"""
    result = _service().revoke_snapshot(payload.快照编号)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.post("/execute")
def execute_cleanup(payload: ExecutePayload) -> dict[str, Any]:
    """提交执行：基于执行时刻完整凭证核对清理范围、暂缓项与下一次可确认分区。"""
    result = _service().execute_cleanup(payload.preview_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason"))
    return result


@router.get("/executions")
def list_executions() -> dict[str, Any]:
    """清理执行清单列表。"""
    return _service().list_executions()
