"""明细层管理业务规则：消费ODS层数据，应用清洗+去重，生成DWD明细表，版本化管理。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

MODULE = "dwd_layer"
LIST_FIELDS = ["表编号", "父层来源", "表名称", "粒度说明", "分区字段", "关联键列", "刷新水位", "创建批次"]
REQUIRED_FIELDS = ["表编号", "父层来源", "表名称"]
STATUS_ORDER = ["草稿", "待校验", "已上线", "已下线"]
ACTION_RULES = {"提交校验": "待校验", "确认上线": "已上线", "标记下线": "已下线"}
NEGATIVE_ACTIONS = []

# 迟到分区冲突的默认处理策略。确认冲突如何裁决、水位谁优先目前未定，
# 当前实现按「窗口完整性优先」：迟到数据合并进受影响窗口生成新版本，旧版本标记失效保留可查。
CONFLICT_POLICY = (
    "迟到分区默认策略（待裁决）：迟到数据合并进受影响窗口生成新版本，旧版本标记失效但保留片段；"
    "确认冲突处理与水位优先级尚未裁决，当前按窗口完整性优先，用户始终只看到一份完整DWD增量明细表。"
)


class DwdLayerService:
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
        if values.get("清洗策略"):
            entry["清洗策略"] = values["清洗策略"]
        if values.get("去重配置"):
            entry["去重配置"] = values["去重配置"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"明细表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于明细层管理可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"明细表已{action}"

    def build_dwd_from_ods(self, entry_id: int) -> dict[str, Any]:
        """从关联的ODS表读取数据，应用清洗+去重，写入DWD模拟表。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "明细表不存在"}
        # 找到父层ODS表
        parent_code = entry.get("父层来源", "")
        ods_entry = None
        for row in store.rows("ods_layer"):
            if row.get("表编号") == parent_code or row.get("表名称") == parent_code:
                ods_entry = row
                break
        if not ods_entry:
            return {"ok": False, "message": f"父层来源 {parent_code} 不存在"}
        ods_table = ods_entry.get("表名称", "")
        ods_data = store.table_rows(ods_table)
        if not ods_data:
            # 如果ODS没有数据，用seed数据生成模拟数据
            ods_data = self._mock_ods_data(ods_table)
            store.set_table_rows(ods_table, ods_data)
        # 应用清洗
        cleanse_id = entry.get("清洗策略", "")
        if cleanse_id:
            from app.services.cleanse_rule import CleanseRuleService
            cs = CleanseRuleService()
            c_entry = store.find("cleanse_rule", self._resolve_id("cleanse_rule", cleanse_id))
            if c_entry:
                ods_data = cs.apply_cleanse(c_entry["id"], ods_data)
        # 应用去重
        dedup_id = entry.get("去重配置", "")
        if dedup_id:
            from app.services.dedup_config import DedupConfigService
            ds = DedupConfigService()
            d_entry = store.find("dedup_config", self._resolve_id("dedup_config", dedup_id))
            if d_entry:
                ods_data = ds.apply_dedup(d_entry["id"], ods_data)
        # 写入DWD模拟表
        dwd_table = entry.get("表名称", "")
        store.set_table_rows(dwd_table, ods_data)
        entry["刷新水位"] = date.today().isoformat()
        store.save_version(MODULE, entry_id, entry)
        # 注册血缘
        store.add_lineage(ods_table, dwd_table, ods_entry.get("字段列表", ""),
                          transform_logic="清洗+去重", batch_id=entry.get("创建批次", ""))
        return {"ok": True, "message": f"DWD表 {dwd_table} 已生成，共{len(ods_data)}行"}

    def _resolve_id(self, module: str, code: str) -> int:
        for row in store.rows(module):
            if row.get("策略编号") == code or row.get("配置编号") == code:
                return int(row["id"])
        return 0

    def _mock_ods_data(self, table_name: str) -> list[dict[str, Any]]:
        if "order" in table_name:
            return [
                {"order_id": f"O{i:04d}", "order_amount": f"{i*10.5:.2f}", "order_status": "已完成" if i % 2 else "待支付", "dt": "2026-11-01"}
                for i in range(1, 6)
            ]
        elif "pay" in table_name:
            return [
                {"pay_id": f"P{i:04d}", "pay_amount": f"{i*8.3:.2f}", "pay_time": "2026-11-01T10:00:00", "dt": "2026-11-01"}
                for i in range(1, 6)
            ]
        elif "user" in table_name:
            return [
                {"user_id": f"U{i:04d}", "register_time": "2026-01-01T00:00:00", "user_level": "普通" if i % 2 else "VIP", "dt": "2026-11-01"}
                for i in range(1, 6)
            ]
        return [{"_id": i, "dt": "2026-11-01"} for i in range(1, 6)]

    def get_table_data(self, table_name: str) -> list[dict[str, Any]]:
        """用户视角的明细数据：只返回有效版本行，已失效的窗口旧版本不暴露。"""
        return [r for r in store.table_rows(table_name) if r.get("_valid", True)]

    # ---------- 按刷新水位的批次增量构建 ----------

    def build_incremental(self, entry_id: int, request_id: str | None = None) -> dict[str, Any]:
        """按刷新水位构建ODS新增批次，生成DWD增量明细表。

        事务边界：先完整计算（筛未处理批次→清洗→去重→窗口版本化），再一次提交
        （追加明细行 + 推进水位 + 写构建日志），保证增量批次与刷新水位一致落库；
        计算阶段任何失败都不会留下「明细已写入但水位未推进」的半截状态，可安全重试。
        """
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "status": "失败",
                    "message": f"明细表 {entry_id} 不存在或已归档",
                    "reason": f"明细表 {entry_id} 不存在或已归档，请刷新列表后重试"}
        dwd_table = entry.get("表名称", "")
        request_id = (request_id or "").strip() or None
        # 幂等：同一 request_id 重复提交只生效一次，直接回放首次结果，不产生第二份明细
        if request_id:
            existing = store.find_inc_build(request_id)
            if existing is not None:
                replay = dict(existing.get("result") or {})
                replay["deduplicated"] = True
                replay["message"] = "重复提交已拦截：该请求已生效过一次，未产生重复明细"
                return replay
        try:
            return self._do_build_incremental(entry, request_id)
        except Exception as exc:  # 异常/中断：记录失败原因，明细与水位均未变更，可重试
            log = self._new_build_log(entry, request_id, status="失败")
            log["reason"] = f"构建中断：{exc.__class__.__name__}: {exc}"
            store.append_inc_build_log(dwd_table, log)
            return {"ok": False, "status": "失败", "reason": log["reason"],
                    "message": f"增量构建失败：{exc}；明细与水位均未变更，可重试"}

    def _do_build_incremental(self, entry: dict[str, Any], request_id: str | None) -> dict[str, Any]:
        entry_id = int(entry["id"])
        dwd_table = entry.get("表名称", "")
        ods_entry = self._find_parent_ods(entry)
        if ods_entry is None:
            reason = f"父层来源 {entry.get('父层来源', '')} 不存在，请先在贴源层管理登记对应ODS表"
            self._record_build(entry, request_id, status="失败", reason=reason)
            return {"ok": False, "status": "失败", "reason": reason,
                    "message": f"增量构建失败：{reason}"}
        ods_table = ods_entry.get("表名称", "")
        ods_rows = store.table_rows(ods_table)
        if not ods_rows:
            reason = f"ODS表 {ods_table} 暂无数据记录（空态），等待新批次到达后重试"
            self._record_build(entry, request_id, status="空态", reason=reason)
            return {"ok": True, "status": "空态", "reason": reason, "message": reason,
                    "rows_written": 0, "batches": []}
        # 按批次分组：无批次标记的行归入ODS创建批次
        batches = self._group_rows_by_batch(ods_rows, ods_entry)
        processed = set(entry.get("_已处理批次") or [])
        if not entry.get("_已处理批次") and entry.get("创建批次"):
            # 明细表创建时已消费过创建批次，视为已处理
            processed.add(entry["创建批次"])
        pending_batches = [b for b in sorted(batches) if b not in processed]
        if not pending_batches:
            reason = (f"暂无新增批次：ODS表 {ods_table} 的 {len(batches)} 个批次全部已处理，"
                      f"刷新水位保持 {entry.get('刷新水位', '') or '空'}，未写入新明细")
            self._record_build(entry, request_id, status="空态", reason=reason)
            return {"ok": True, "status": "空态", "reason": reason, "message": reason,
                    "rows_written": 0, "batches": []}
        # 隐式幂等键：同一明细表对同一组待处理批次的成功构建只生效一次
        implicit_key = f"dwd:{entry_id}@batches:{','.join(pending_batches)}"
        existing = store.find_inc_build(implicit_key)
        if existing is not None:
            replay = dict(existing.get("result") or {})
            replay["deduplicated"] = True
            replay["message"] = "重复构建同一水位已拦截：该批次组合已落库，未产生重复明细"
            return replay
        # 收集未处理批次的数据行
        raw_rows: list[dict[str, Any]] = []
        for batch_no in pending_batches:
            for row in batches[batch_no]:
                raw_rows.append({**row, "_batch": batch_no})
        # 清洗 + 去重
        processed_rows = self._apply_cleanse_dedup(entry, raw_rows)
        # 窗口版本化：迟到分区与已推进水位冲突时，旧版本失效、生成新窗口版本
        existing_rows = store.table_rows(dwd_table)
        valid_by_window: dict[str, list[dict[str, Any]]] = {}
        window_versions: dict[str, int] = {}
        for row in existing_rows:
            window = self._row_window(row)
            window_versions[window] = max(window_versions.get(window, 0), int(row.get("_version", 1)))
            if row.get("_valid", True):
                valid_by_window.setdefault(window, []).append(row)
        new_by_window: dict[str, list[dict[str, Any]]] = {}
        for row in processed_rows:
            window = str(row.get("dt") or self._batch_date(row.get("_batch", "")))
            new_by_window.setdefault(window, []).append(row)
        late_windows: list[str] = []
        commit_rows: list[dict[str, Any]] = []
        for window in sorted(new_by_window):
            rows = new_by_window[window]
            if window in valid_by_window:
                # 迟到分区冲突：保留原窗口版本（标记失效），合并迟到行生成新版本片段
                late_windows.append(window)
                new_version = window_versions.get(window, 1) + 1
                merged = [self._business_copy(r) for r in valid_by_window[window]] + rows
                merged = self._dedup_rows(entry, merged)
                for old in valid_by_window[window]:
                    old["_valid"] = False
                    old["_superseded_by"] = new_version
                for row in merged:
                    commit_rows.append({**row, "_window": window, "_version": new_version, "_valid": True})
            else:
                for row in rows:
                    commit_rows.append({**row, "_window": window, "_version": 1, "_valid": True})
        # ---- 原子提交：明细行、刷新水位、构建日志在同一边界内落库 ----
        watermark_before = str(entry.get("刷新水位", ""))
        batch_before = entry.get("_批次水位") or entry.get("创建批次", "")
        processed.update(pending_batches)
        all_windows = set(valid_by_window) | set(new_by_window)
        new_watermark = max(all_windows) if all_windows else watermark_before
        store.append_table_rows(dwd_table, commit_rows)
        entry["_已处理批次"] = sorted(processed)
        entry["_批次水位"] = max(processed) if processed else entry.get("创建批次", "")
        entry["刷新水位"] = new_watermark
        store.save_version(MODULE, entry_id, entry)
        store.add_lineage(ods_table, dwd_table, ods_entry.get("字段列表", ""),
                          transform_logic="增量构建（清洗+去重）", batch_id=",".join(pending_batches))
        message = f"增量构建完成：处理{len(pending_batches)}个批次，写入{len(commit_rows)}行"
        if late_windows:
            message += f"，{len(late_windows)}个迟到窗口（{'、'.join(late_windows)}）已生成新版本并标记旧版本失效"
        result: dict[str, Any] = {
            "ok": True, "status": "成功", "message": message,
            "rows_written": len(commit_rows), "batches": pending_batches,
            "late_windows": late_windows,
            "watermark_before": watermark_before, "watermark_after": new_watermark,
        }
        log = self._record_build(entry, request_id, status="成功", batches=pending_batches,
                                 late_windows=late_windows, rows_written=len(commit_rows),
                                 watermark_before=watermark_before, batch_before=batch_before,
                                 watermark_after=new_watermark, result=result)
        store.register_inc_request(implicit_key, log)
        result["build_id"] = log["build_id"]
        return result

    def simulate_ods_batch(self, entry_id: int, late: bool = False) -> dict[str, Any]:
        """模拟ODS新批次到达：正常批次落在水位之后；迟到批次落在已处理窗口内。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": f"明细表 {entry_id} 不存在或已归档"}
        ods_entry = self._find_parent_ods(entry)
        if not ods_entry:
            return {"ok": False, "message": f"父层来源 {entry.get('父层来源', '')} 不存在"}
        ods_table = ods_entry.get("表名称", "")
        ods_rows = store.table_rows(ods_table)
        known_batches = {str(r.get("batch") or r.get("_batch") or "") for r in ods_rows}
        known_batches.discard("")
        if ods_entry.get("创建批次"):
            known_batches.add(ods_entry["创建批次"])
        known_dates = [d for d in (self._batch_date(b) for b in known_batches) if d]
        max_date = max(known_dates) if known_dates else date.today().isoformat()
        next_date = (date.fromisoformat(max_date) + timedelta(days=1)).isoformat()
        new_batch = f"RAW-{next_date.replace('-', '')}"
        if late:
            processed_windows = sorted({
                self._row_window(r)
                for r in store.table_rows(entry.get("表名称", ""))
                if r.get("_valid", True)
            })
            if not processed_windows:
                return {"ok": False,
                        "message": "当前没有已处理窗口，无法模拟迟到分区，请先完成一次增量构建"}
            row_dt = processed_windows[0]
        else:
            row_dt = next_date
        fields = [f.strip() for f in str(ods_entry.get("字段列表", "")).split(",") if f.strip()] or ["_id"]
        start_seq = len(ods_rows) + 1
        new_rows = [self._mock_row(fields, new_batch, row_dt, start_seq + i) for i in range(3)]
        store.append_table_rows(ods_table, new_rows)
        kind = "迟到批次" if late else "新批次"
        return {"ok": True,
                "message": f"{kind} {new_batch} 已到达 {ods_table}（业务日期 {row_dt}，{len(new_rows)}行）",
                "batch": new_batch, "dt": row_dt, "rows": len(new_rows)}

    def get_incremental_detail(self, entry_id: int) -> dict[str, Any] | None:
        """DWD增量明细表详情：当前水位、构建日志（水位可回查）、有效明细行与窗口版本。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        dwd_table = entry.get("表名称", "")
        all_rows = store.table_rows(dwd_table)
        # 用户只看到一份完整DWD增量明细表：只暴露有效版本行
        valid_rows = [r for r in all_rows if r.get("_valid", True)]
        windows: dict[str, dict[str, Any]] = {}
        for row in all_rows:
            window = self._row_window(row)
            slot = windows.setdefault(window, {"window": window, "versions": set(),
                                               "valid_rows": 0, "expired_rows": 0})
            slot["versions"].add(int(row.get("_version", 1)))
            if row.get("_valid", True):
                slot["valid_rows"] += 1
            else:
                slot["expired_rows"] += 1
        window_list = [
            {**slot, "versions": sorted(slot["versions"]), "current_version": max(slot["versions"])}
            for slot in sorted(windows.values(), key=lambda item: item["window"])
        ]
        logs = store.inc_build_logs(dwd_table)
        return {
            "entry": entry,
            "刷新水位": entry.get("刷新水位", ""),
            "批次水位": entry.get("_批次水位") or entry.get("创建批次", ""),
            "已处理批次": entry.get("_已处理批次") or [],
            "rows": valid_rows,
            "total_rows": len(valid_rows),
            "logs": list(reversed(logs)),
            "windows": window_list,
            "conflict_policy": CONFLICT_POLICY,
        }

    # ---------- 增量构建内部工具 ----------

    def _find_parent_ods(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        parent_code = entry.get("父层来源", "")
        for row in store.rows("ods_layer"):
            if row.get("表编号") == parent_code or row.get("表名称") == parent_code:
                return row
        return None

    def _record_build(self, entry: dict[str, Any], request_id: str | None, *,
                      status: str, reason: str = "", batches: list[str] | None = None,
                      late_windows: list[str] | None = None, rows_written: int = 0,
                      watermark_before: str | None = None, batch_before: str | None = None,
                      watermark_after: str | None = None,
                      result: dict[str, Any] | None = None) -> dict[str, Any]:
        log = self._new_build_log(entry, request_id, status=status)
        log["reason"] = reason
        log["batches"] = batches or []
        log["late_windows"] = late_windows or []
        log["rows_written"] = rows_written
        if watermark_before is not None:
            log["watermark_before"] = watermark_before
        if batch_before is not None:
            log["batch_before"] = batch_before
        if watermark_after is not None:
            log["watermark_after"] = watermark_after
            log["batch_after"] = entry.get("_批次水位") or entry.get("创建批次", "")
        log["result"] = result
        store.append_inc_build_log(entry.get("表名称", ""), log)
        return log

    def _new_build_log(self, entry: dict[str, Any], request_id: str | None, *,
                       status: str) -> dict[str, Any]:
        watermark = entry.get("刷新水位", "")
        batch_watermark = entry.get("_批次水位") or entry.get("创建批次", "")
        return {
            "build_id": store.next_inc_build_id(),
            "request_id": request_id or "",
            "表名称": entry.get("表名称", ""),
            "status": status,
            "reason": "",
            "batches": [],
            "late_windows": [],
            "rows_written": 0,
            "watermark_before": watermark,
            "watermark_after": watermark,
            "batch_before": batch_watermark,
            "batch_after": batch_watermark,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "result": None,
        }

    def _group_rows_by_batch(self, ods_rows: list[dict[str, Any]],
                             ods_entry: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        default_batch = ods_entry.get("创建批次", "") or "UNKNOWN"
        batches: dict[str, list[dict[str, Any]]] = {}
        for row in ods_rows:
            batch_no = str(row.get("batch") or row.get("_batch") or default_batch)
            batches.setdefault(batch_no, []).append(row)
        return batches

    def _apply_cleanse_dedup(self, entry: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleanse_id = entry.get("清洗策略", "")
        if cleanse_id:
            from app.services.cleanse_rule import CleanseRuleService
            cs = CleanseRuleService()
            c_entry = store.find("cleanse_rule", self._resolve_id("cleanse_rule", cleanse_id))
            if c_entry:
                rows = cs.apply_cleanse(c_entry["id"], rows)
        return self._dedup_rows(entry, rows)

    def _dedup_rows(self, entry: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        key_col = ""
        dedup_id = entry.get("去重配置", "")
        if dedup_id:
            d_entry = store.find("dedup_config", self._resolve_id("dedup_config", dedup_id))
            if d_entry:
                key_col = d_entry.get("业务键列", "")
        if not key_col:
            key_col = str(entry.get("关联键列", "")).split(",")[0].strip()
        if not key_col:
            return rows
        seen: dict[str, dict[str, Any]] = {}
        nokey: list[dict[str, Any]] = []
        for row in rows:
            key_val = str(row.get(key_col, "")).strip()
            if key_val:
                seen[key_val] = row
            else:
                nokey.append(row)
        return [*seen.values(), *nokey]

    def _business_copy(self, row: dict[str, Any]) -> dict[str, Any]:
        """剥离版本化元数据，保留业务字段与批次归属，用于窗口重放合并。"""
        return {k: v for k, v in row.items() if k not in ("_valid", "_version", "_window", "_superseded_by")}

    def _row_window(self, row: dict[str, Any]) -> str:
        return str(row.get("_window") or row.get("dt")
                   or self._batch_date(str(row.get("_batch") or row.get("batch") or "")))

    def _batch_date(self, batch_no: str) -> str:
        digits = "".join(ch for ch in str(batch_no) if ch.isdigit())
        if len(digits) >= 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        return ""

    def _mock_row(self, fields: list[str], batch_no: str, dt: str, seq: int) -> dict[str, Any]:
        row: dict[str, Any] = {"batch": batch_no, "dt": dt}
        for field in fields:
            if field == "dt":
                row["dt"] = dt
            elif field.endswith("_id"):
                prefix = (field.split("_")[0][:1] or "X").upper()
                row[field] = f"{prefix}{seq:04d}"
            elif "amount" in field:
                row[field] = f"{seq * 10.5:.2f}"
            elif "time" in field:
                row[field] = f"{dt}T10:00:00"
            elif "status" in field:
                row[field] = "已完成" if seq % 2 else "待支付"
            elif "level" in field:
                row[field] = "VIP" if seq % 2 else "普通"
            elif field == "_id":
                row[field] = seq
            else:
                row.setdefault(field, f"v{seq}")
        return row
