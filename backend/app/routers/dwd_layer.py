"""明细层管理接口：维护明细表，覆盖提交校验、确认上线、标记下线等动作。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.dwd_layer import DwdLayerService

router = APIRouter(prefix="/api/dwd_layer", tags=["明细层管理"])

service = DwdLayerService()

LIST_FIELDS = ["表编号", "父层来源", "表名称", "粒度说明", "分区字段", "关联键列", "刷新水位", "创建批次"]
STATUSES = ["草稿", "待校验", "已上线", "已下线"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按表编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已上线、已下线"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、表编号关键字与状态过滤明细层管理列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="dwd_layer.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条明细表明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"明细表 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条明细表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="明细表已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条明细表执行提交校验、确认上线、标记下线；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/build")
def build_dwd(entry_id: int) -> dict[str, Any]:
    """从关联ODS表读取数据，应用清洗+去重，生成DWD表。"""
    result = service.build_dwd_from_ods(entry_id)
    return result


@router.post("/{entry_id}/build_incremental")
def build_incremental(entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """按刷新水位增量构建ODS新增批次，生成DWD增量明细表。

    - 只处理未处理批次，明细与水位在同一边界内落库；
    - 携带 request_id 重复提交只生效一次；同一水位重复构建没有新增行；
    - 空态/失败会说明原因，可安全重试。
    """
    request_id = str((payload or {}).get("request_id") or "").strip() or None
    return service.build_incremental(entry_id, request_id=request_id)


@router.post("/{entry_id}/simulate_ods_batch")
def simulate_ods_batch(entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """模拟父层ODS新批次到达；late=true 时模拟业务日期落在已处理窗口的迟到批次。"""
    late = bool((payload or {}).get("late"))
    result = service.simulate_ods_batch(entry_id, late=late)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "模拟批次失败"))
    return result


@router.get("/{entry_id}/incremental")
def get_incremental_detail(entry_id: int) -> dict[str, Any]:
    """DWD增量明细表详情：当前水位、构建日志（水位可回查）、有效明细行与窗口版本。"""
    detail = service.get_incremental_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"明细表 {entry_id} 不存在或已归档")
    return detail


@router.get("/{entry_id}/data")
def get_table_data(entry_id: int) -> dict[str, Any]:
    """返回该DWD表的模拟数据。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"明细表 {entry_id} 不存在")
    data = service.get_table_data(entry.get("表名称", ""))
    return {"items": data}
