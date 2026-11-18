"""测试：明细层按刷新水位的批次增量构建。

验证：
- 增量构建只消费水位之后的未处理批次
- 增量批次与刷新水位一致落库（原子性）
- 重复提交（同 request_id / 同一水位）只生效一次，没有第二份明细
- 空态、失败均说明原因且水位不动，可安全重试
- 迟到分区触发窗口版本化：旧版本失效、新版本合并迟到行，用户只看到一份完整明细
- 汇总层只消费有效版本行，重复构建后仍是一份完整增量结果
"""
from __future__ import annotations

from app.store import store
from app.services.dwd_layer import DwdLayerService
from app.services.dws_layer import DwsLayerService


def reset_store():
    from app.seed import SEED_ROWS
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._table_data.clear()
    store._versions.clear()
    store._lineage_edges.clear()
    store._quality_results.clear()
    store._snapshot_data.clear()
    store._archive_records.clear()
    store._backfill_partitions.clear()
    store._inc_build_logs.clear()
    store._inc_request_index.clear()


def seed_ods_batches():
    """给 ods_orders 注入三个批次：创建批次（已处理）+ 两个新批次。"""
    store.set_table_rows("ods_orders", [
        {"order_id": "O001", "order_amount": "100.00", "order_status": "已完成",
         "dt": "2026-11-01", "batch": "RAW-20261101"},
        {"order_id": "O002", "order_amount": "200.00", "order_status": "待支付",
         "dt": "2026-11-02", "batch": "RAW-20261102"},
        {"order_id": "O003", "order_amount": "50.00", "order_status": "已完成",
         "dt": "2026-11-02", "batch": "RAW-20261102"},
        {"order_id": "O004", "order_amount": "80.00", "order_status": "已完成",
         "dt": "2026-11-04", "batch": "RAW-20261104"},
    ])


class TestDwdIncremental:
    def setup_method(self):
        reset_store()
        self.dwd = DwdLayerService()

    def test_incremental_build_only_pending_batches(self):
        """增量构建只处理未处理批次，创建批次不会被重复消费。"""
        seed_ods_batches()
        result = self.dwd.build_incremental(1)
        assert result["ok"] is True
        assert result["status"] == "成功"
        assert result["batches"] == ["RAW-20261102", "RAW-20261104"]
        rows = store.table_rows("dwd_order_detail")
        assert len(rows) == 3  # O002、O003、O004；O001 属于已处理的创建批次
        assert {r["_batch"] for r in rows} == {"RAW-20261102", "RAW-20261104"}

    def test_watermark_consistent_with_rows(self):
        """增量批次与刷新水位一致落库：水位=最后批次业务日期，批次水位=最后批次。"""
        seed_ods_batches()
        result = self.dwd.build_incremental(1)
        entry = self.dwd.get_entry(1)
        assert entry["刷新水位"] == "2026-11-04" == result["watermark_after"]
        assert entry["_批次水位"] == "RAW-20261104"
        assert entry["_已处理批次"] == ["RAW-20261101", "RAW-20261102", "RAW-20261104"]
        # 水位可回查：构建日志记录了水位前后值
        logs = store.inc_build_logs("dwd_order_detail")
        assert len(logs) == 1
        assert logs[0]["watermark_before"] == "2026-11-03"
        assert logs[0]["watermark_after"] == "2026-11-04"
        assert logs[0]["batch_after"] == "RAW-20261104"

    def test_duplicate_request_id_only_effective_once(self):
        """同一 request_id 重复提交只生效一次，不产生第二份相同明细。"""
        seed_ods_batches()
        first = self.dwd.build_incremental(1, request_id="req-001")
        assert first["ok"] is True and first["rows_written"] == 3
        second = self.dwd.build_incremental(1, request_id="req-001")
        assert second["deduplicated"] is True
        assert len(store.table_rows("dwd_order_detail")) == 3  # 行数不变
        assert len(store.inc_build_logs("dwd_order_detail")) == 1  # 只落库一次

    def test_rebuild_same_watermark_no_new_rows(self):
        """重复构建同一水位时没有新增行，返回空态并说明原因。"""
        seed_ods_batches()
        self.dwd.build_incremental(1)
        again = self.dwd.build_incremental(1)
        assert again["status"] == "空态"
        assert "暂无新增批次" in again["reason"]
        assert len(store.table_rows("dwd_order_detail")) == 3
        entry = self.dwd.get_entry(1)
        assert entry["刷新水位"] == "2026-11-04"  # 水位不动

    def test_empty_ods_reports_reason(self):
        """ODS暂无数据记录时返回空态原因，水位保持不变。"""
        result = self.dwd.build_incremental(1)
        assert result["status"] == "空态"
        assert "暂无数据记录" in result["reason"]
        entry = self.dwd.get_entry(1)
        assert entry["刷新水位"] == "2026-11-03"
        # 空态也记入日志，可回查
        logs = store.inc_build_logs("dwd_order_detail")
        assert logs[-1]["status"] == "空态"

    def test_failed_build_allows_retry(self):
        """父层缺失时构建失败并说明原因；修复后重试成功且不重复。"""
        entry = self.dwd.get_entry(1)
        entry["父层来源"] = "ODS_NOT_EXIST"
        failed = self.dwd.build_incremental(1, request_id="req-retry")
        assert failed["ok"] is False
        assert failed["status"] == "失败"
        assert "不存在" in failed["reason"]
        assert len(store.table_rows("dwd_order_detail")) == 0  # 明细未写入
        # 修复父层后同一 request_id 重试：失败未占用幂等键，可以再次生效
        entry["父层来源"] = "ODS_ORD_001"
        seed_ods_batches()
        retried = self.dwd.build_incremental(1, request_id="req-retry")
        assert retried["ok"] is True and retried["rows_written"] == 3
        replayed = self.dwd.build_incremental(1, request_id="req-retry")
        assert replayed["deduplicated"] is True
        assert len(store.table_rows("dwd_order_detail")) == 3

    def test_late_partition_window_versioning(self):
        """迟到分区：旧窗口版本标记失效保留片段，新版本合并迟到行，用户视图唯一完整。"""
        seed_ods_batches()
        self.dwd.build_incremental(1)
        # 迟到批次：业务日期落在已处理窗口 2026-11-02
        store.append_table_rows("ods_orders", [
            {"order_id": "O005", "order_amount": "66.00", "order_status": "已完成",
             "dt": "2026-11-02", "batch": "RAW-20261105"},
        ])
        result = self.dwd.build_incremental(1)
        assert result["ok"] is True
        assert result["late_windows"] == ["2026-11-02"]
        all_rows = store.table_rows("dwd_order_detail")
        old_version = [r for r in all_rows if r.get("_window") == "2026-11-02" and r["_version"] == 1]
        new_version = [r for r in all_rows if r.get("_window") == "2026-11-02" and r["_version"] == 2]
        # 原窗口版本保留但已失效
        assert len(old_version) == 2 and all(r["_valid"] is False for r in old_version)
        # 新版本合并了迟到行
        assert len(new_version) == 3 and all(r["_valid"] is True for r in new_version)
        assert {r["order_id"] for r in new_version} == {"O002", "O003", "O005"}
        # 用户只看到一份完整DWD增量明细表
        detail = self.dwd.get_incremental_detail(1)
        assert detail["total_rows"] == 4  # O002、O003、O004、O005 各一份
        windows = {w["window"]: w for w in detail["windows"]}
        assert windows["2026-11-02"]["versions"] == [1, 2]
        assert windows["2026-11-02"]["expired_rows"] == 2
        assert windows["2026-11-02"]["valid_rows"] == 3

    def test_dws_consumes_single_valid_view(self):
        """汇总层跨模块消费DWD增量明细表：只读有效版本，重复构建后结果一致。"""
        seed_ods_batches()
        self.dwd.build_incremental(1)
        store.append_table_rows("ods_orders", [
            {"order_id": "O005", "order_amount": "66.00", "order_status": "已完成",
             "dt": "2026-11-02", "batch": "RAW-20261105"},
        ])
        self.dwd.build_incremental(1)
        dws = DwsLayerService()
        first = dws.aggregate_from_dwd(1)
        assert first["ok"] is True
        completed = next(r for r in first["rows"] if r.get("order_status") == "已完成"
                         and r.get("dt") == "2026-11-02")
        # 有效视图里 2026-11-02 已完成 = O003(50) + O005(66)，旧版本不计入
        assert completed["order_amount_agg"] == 116.0
        # 重复构建同一水位：没有新增行，汇总层仍只看到一份完整增量结果
        self.dwd.build_incremental(1)
        second = dws.aggregate_from_dwd(1)
        assert second["rows"] == first["rows"]

    def test_incremental_detail_traceable(self):
        """详情页数据：水位、已处理批次、构建日志均可回查。"""
        seed_ods_batches()
        self.dwd.build_incremental(1, request_id="req-trace")
        detail = self.dwd.get_incremental_detail(1)
        assert detail["刷新水位"] == "2026-11-04"
        assert detail["批次水位"] == "RAW-20261104"
        assert detail["已处理批次"] == ["RAW-20261101", "RAW-20261102", "RAW-20261104"]
        assert detail["logs"][0]["request_id"] == "req-trace"
        assert detail["conflict_policy"]

    def test_simulate_ods_batch_and_late(self):
        """模拟新批次到达后可被增量构建消费；迟到批次落在已处理窗口。"""
        seed_ods_batches()
        self.dwd.build_incremental(1)
        arrived = self.dwd.simulate_ods_batch(1)
        assert arrived["ok"] is True
        assert arrived["batch"] == "RAW-20261105"
        assert arrived["dt"] == "2026-11-05"
        result = self.dwd.build_incremental(1)
        assert result["batches"] == ["RAW-20261105"]
        # 模拟迟到批次：业务日期取已处理窗口
        late = self.dwd.simulate_ods_batch(1, late=True)
        assert late["ok"] is True
        assert late["dt"] == "2026-11-02"
        result = self.dwd.build_incremental(1)
        assert result["late_windows"] == ["2026-11-02"]
