"""cold_archive 领域服务：冷热数据分层归档。

业务逻辑：
1. 冷热分层：热数据保留天数和冷存储路径
2. 归档链路：cold_archive → retention（归档后的数据由保留策略管理生命周期）
3. 失败场景：冷存储路径不可达、查询透传失败、归档格式不兼容
4. 与上下游流水线：为 retention 提供归档清单

跨模块数据流：
  cold_archive → retention（冷归档清单决定保留策略的清理范围）
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ColdArchiveService(BaseModuleService):
    MODULE = "cold_archive"
    ENTITY = "归档策略"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["策略编号", "源表名称", "热数据天数", "冷存储路径", "归档格式", "压缩算法", "查询透传", "归档时间"]
    REQUIRED_FIELDS = ["策略编号", "源表名称", "热数据天数"]
    STATUS_ORDER = ["草稿", "待验证", "已生效", "已停用"]
    ACTION_RULES = {"提交验证": "待验证", "标记生效": "已生效", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    PIPELINE = "cold_archive_pipeline"
    DOWNSTREAM_MODULES = ["retention"]
    PRODUCES_ARTIFACT = "冷归档操作记录"

    def execute_archive(self, entry_id: int) -> dict[str, Any]:
        """执行冷热分层归档。

        失败场景：
        - 冷存储路径不可达
        - 归档格式不兼容
        - 压缩算法不支持
        - 查询透传配置错误导致归档后不可查询
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": f"策略 {entry_id} 不存在"}
        if entry.get("status") != "已生效":
            return {"ok": False, "reason": f"策略未生效，当前状态: {entry.get('status')}"}

        table_name = str(entry.get("源表名称", ""))
        hot_days = int(str(entry.get("热数据天数", "7")))
        cold_path = str(entry.get("冷存储路径", ""))
        archive_fmt = str(entry.get("归档格式", "parquet"))
        compression = str(entry.get("压缩算法", "zstd"))
        query_pass_through = str(entry.get("查询透传", "disabled"))

        if not cold_path:
            return {"ok": False, "reason": "冷存储路径为空，无法归档"}

        if archive_fmt not in ("parquet", "orc", "avro"):
            return {"ok": False, "reason": f"归档格式 {archive_fmt} 不支持"}

        if compression not in ("snappy", "gzip", "lz4", "zstd"):
            return {"ok": False, "reason": f"压缩算法 {compression} 不支持"}

        # 模拟归档操作
        archived_rows = 0
        table_rows = store.rows(table_name) if table_name in store._tables else []
        # 模拟：按热数据天数过滤
        cutoff_ts = time.time() - hot_days * 86400
        cold_rows = [r for r in table_rows if float(str(r.get("_ts", time.time()))) < cutoff_ts]
        archived_rows = len(cold_rows) or len(table_rows)  # 如果没有_ts则视为全部归档

        archive_record = {
            "策略编号": entry.get("策略编号"),
            "源表名称": table_name,
            "冷存储路径": cold_path,
            "归档格式": archive_fmt,
            "压缩算法": compression,
            "归档时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "archived_rows": archived_rows,
        }

        # 写入归档清单（供 retention 消费）
        store.add_archive_record(archive_record)

        # 已确认归档状态回写清理清单：生成归档凭证（按表+版本，覆盖超热数据天数的分区）
        covered = [
            str(obj["分区"]) for obj in store.cleanup_objects_for_table(table_name)
            if float(str(obj.get("_ts", time.time()))) < cutoff_ts
        ]
        snap_version = ""
        readable = store.readable_snapshots_for_table(table_name)
        if readable:
            snap_version = str(readable[0].get("版本", ""))
        credential = store.add_archive_credential({
            "表名": table_name,
            "覆盖分区": covered,
            "归档版本": snap_version or f"archive-{int(time.time())}",
            "状态": "已确认",
            "来源策略": str(entry.get("策略编号", "")),
            "归档时间": archive_record["归档时间"],
        })

        entry["归档时间"] = time.strftime("%Y-%m-%d %H:%M:%S")

        batch_id = f"COLD_ARCHIVE_{entry_id}_{int(time.time())}"
        self.produce_artifact([archive_record], batch_id)

        return {
            "ok": True,
            "entry_id": entry_id,
            "table": table_name,
            "archived_rows": archived_rows,
            "cold_path": cold_path,
            "archive_fmt": archive_fmt,
            "credential": credential["凭证编号"],
        }

    def emit_archive_manifest(self) -> list[dict[str, Any]]:
        """向 retention 暴露归档清单"""
        return store.archive_manifest_for_table("") or store._archive_manifest

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["archive_records"] = len(store._archive_manifest)
        return base