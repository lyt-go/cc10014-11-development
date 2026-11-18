"""汇总层管理业务规则：消费DWD明细数据，按维度聚合生成汇总表，支持版本和血缘。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "dws_layer"
LIST_FIELDS = ["表编号", "统计粒度", "汇总维度", "度量字段", "聚合函数", "时间范围", "刷新周期", "创建批次"]
REQUIRED_FIELDS = ["表编号", "统计粒度", "汇总维度"]
STATUS_ORDER = ["草稿", "待校验", "已上线", "已下线"]
ACTION_RULES = {"提交校验": "待校验", "确认上线": "已上线", "标记下线": "已下线"}
NEGATIVE_ACTIONS = []


class DwsLayerService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("表编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        if values.get("来源明细表"):
            entry["来源明细表"] = values["来源明细表"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"汇总表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于汇总层管理可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"汇总表已{action}"

    def aggregate_from_dwd(self, entry_id: int) -> dict[str, Any]:
        """从关联DWD表读取数据，按汇总维度聚合。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "汇总表不存在"}
        dwd_code = entry.get("来源明细表", "")
        dwd_entry = None
        for row in store.rows("dwd_layer"):
            if row.get("表编号") == dwd_code or row.get("表名称") == dwd_code:
                dwd_entry = row
                break
        if not dwd_entry:
            return {"ok": False, "message": f"来源明细表 {dwd_code} 不存在"}
        dwd_table = dwd_entry.get("表名称", "")
        # 只消费有效版本行：迟到分区的旧版本已失效，汇总层始终只看到一份完整增量结果
        dwd_data = [r for r in store.table_rows(dwd_table) if r.get("_valid", True)]
        if not dwd_data:
            from app.services.dwd_layer import DwdLayerService
            dwd_svc = DwdLayerService()
            dwd_svc.build_dwd_from_ods(dwd_entry["id"])
            dwd_data = [r for r in store.table_rows(dwd_table) if r.get("_valid", True)]
        dims = [d.strip() for d in entry.get("汇总维度", "").split(",")]
        measure = entry.get("度量字段", "")
        agg_func = entry.get("聚合函数", "SUM")
        result = self._do_aggregate(dwd_data, dims, measure, agg_func)
        dws_table = entry.get("表名称", f"dws_{entry_id}")
        store.set_table_rows(dws_table, result)
        store.save_version(MODULE, entry_id, entry)
        # 注册血缘
        store.add_lineage(dwd_table, dws_table, ",".join(dims),
                          transform_logic=f"{agg_func}({measure}) GROUP BY {','.join(dims)}",
                          batch_id=entry.get("创建批次", ""))
        return {"ok": True, "message": f"汇总表 {dws_table} 已生成，共{len(result)}行", "rows": result}

    def _do_aggregate(self, data: list[dict[str, Any]], dims: list[str], measure: str, agg: str) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for row in data:
            key_parts = []
            for d in dims:
                key_parts.append(str(row.get(d, "")))
            key = "|".join(key_parts)
            if key not in groups:
                groups[key] = {d: row.get(d) for d in dims}
                groups[key]["_values"] = []
            try:
                groups[key]["_values"].append(float(row.get(measure, 0) or 0))
            except Exception:
                groups[key]["_values"].append(0.0)
        result = []
        for key, g in groups.items():
            out = {d: g[d] for d in dims}
            vals = g["_values"]
            if agg == "SUM":
                out[f"{measure}_agg"] = sum(vals)
            elif agg == "COUNT":
                out[f"{measure}_agg"] = len(vals)
            elif agg == "AVG":
                out[f"{measure}_agg"] = sum(vals) / len(vals) if vals else 0
            elif agg == "MAX":
                out[f"{measure}_agg"] = max(vals) if vals else 0
            elif agg == "MIN":
                out[f"{measure}_agg"] = min(vals) if vals else 0
            else:
                out[f"{measure}_agg"] = sum(vals)
            result.append(out)
        return result

    def get_table_data(self, table_name: str) -> list[dict[str, Any]]:
        return store.table_rows(table_name)
