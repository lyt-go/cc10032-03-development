"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据，并支持跨模块数据流水线。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from app.seed import SEED_ARCHIVE_CREDENTIALS, SEED_CLEANUP_OBJECTS, SEED_ROWS, SEED_SNAPSHOT_DETAILS


class DataPipeline:
    """跨模块数据流水线引擎：支持 输入→转换→版本化产物→下游消费 的完整链路。

    每条流水线定义了源模块、目标模块、转换函数和验证规则。
    产物存储为可版本化的数据快照，支持脏行检测、重复批次拒绝、冲突版本检测、中断恢复。
    """

    def __init__(self, name: str, sources: list[str], target_module: str) -> None:
        self.name = name
        self.sources = sources
        self.target_module = target_module
        self._artifacts: list[dict[str, Any]] = []
        self._versions: list[str] = []  # 版本号列表
        self._dirty_log: list[dict[str, Any]] = []
        self._batch_registry: set[str] = set()  # 防重复批次
        self._last_checkpoint: dict[str, Any] = {}  # 中断恢复点
        self._interrupted = False

    def ingest(self, rows: list[dict[str, Any]], batch_id: str) -> tuple[int, list[str]]:
        """摄入数据行，返回 (成功行数, 脏行详情列表)"""
        if batch_id in self._batch_registry:
            return 0, [f"重复批次 {batch_id} 已存在，拒绝摄入"]
        self._batch_registry.add(batch_id)
        clean_rows = []
        dirty_details = []
        for i, row in enumerate(rows):
            issues = self._validate_row(row, i)
            if issues:
                dirty_details.append(f"行 {i}: {'; '.join(issues)}")
                self._dirty_log.append({"batch": batch_id, "row_index": i, "issues": issues, "row": row, "ts": time.time()})
            else:
                clean_rows.append(row)
        self._artifacts.extend(clean_rows)
        return len(clean_rows), dirty_details

    def transform(self, transform_fn, *args, **kwargs) -> list[dict[str, Any]]:
        """对当前产物执行转换函数"""
        self._save_checkpoint("transform_enter", len(self._artifacts))
        try:
            result = transform_fn(self._artifacts, *args, **kwargs)
            version = self._make_version("transform")
            self._versions.append(version)
            self._save_checkpoint("transform_exit", len(result))
            return result
        except Exception:
            self._interrupted = True
            raise

    def versioned_snapshot(self) -> dict[str, Any]:
        """生成版本化产物快照"""
        version = self._versions[-1] if self._versions else self._make_version("snapshot")
        return {
            "pipeline": self.name,
            "version": version,
            "artifact_count": len(self._artifacts),
            "artifact_sample": self._artifacts[:5],
            "dirty_count": len(self._dirty_log),
            "batch_count": len(self._batch_registry),
            "interrupted": self._interrupted,
            "checkpoint": self._last_checkpoint,
        }

    def health_report(self) -> dict[str, Any]:
        """流水线健康报告：用于外部模块消费和容量材料生成"""
        return {
            "pipeline": self.name,
            "source_modules": self.sources,
            "target_module": self.target_module,
            "total_ingested": sum(1 for a in self._artifacts),
            "dirty_rows": len(self._dirty_log),
            "versions": len(self._versions),
            "batches_processed": len(self._batch_registry),
            "interrupted": self._interrupted,
            "last_artifact_ts": max((a.get("ts", 0) for a in self._artifacts), default=0) if self._artifacts else 0,
        }

    def _validate_row(self, row: dict[str, Any], index: int) -> list[str]:
        issues = []
        for key, val in row.items():
            if val is None or (isinstance(val, str) and not val.strip()):
                issues.append(f"空值字段 '{key}'")
        if any(row.get(k) is None for k in ["id"]):
            issues.append("缺少主键 id")
        return issues

    def _make_version(self, tag: str) -> str:
        ts = str(int(time.time() * 1000))
        raw = f"{self.name}:{tag}:{ts}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _save_checkpoint(self, stage: str, count: int) -> None:
        self._last_checkpoint = {"stage": stage, "count": count, "ts": time.time()}

    def recover_checkpoint(self) -> dict[str, Any]:
        """从最近检查点恢复"""
        return {
            "pipeline": self.name,
            "interrupted": self._interrupted,
            "last_checkpoint": self._last_checkpoint,
            "artifacts_before_interrupt": len(self._artifacts),
        }


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 跨模块数据流水线
        self._pipelines: dict[str, DataPipeline] = {}
        # 全局变更日志（用于 changelog 跨模块追溯）
        self._global_changelog: list[dict[str, Any]] = []
        # Schema 注册表（cross-module schema registry）
        self._schema_registry: dict[str, list[dict[str, Any]]] = {}
        # 水位偏移记录（watermark → late_data 共享）
        self._watermark_offsets: dict[str, dict[str, Any]] = {}
        # 快照存储表（snapshot_mgr → backfill_job 共享）
        self._snapshot_store: dict[str, dict[str, Any]] = {}
        # 血缘图（lineage → table_format 共享）
        self._lineage_graph: dict[str, list[dict[str, Any]]] = {}
        # 归档清单（cold_archive → retention 共享）
        self._archive_manifest: list[dict[str, Any]] = []
        # 清理对象（表分区级数据对象，retention 清理预览的评估单元）
        self._cleanup_objects: list[dict[str, Any]] = [dict(row) for row in SEED_CLEANUP_OBJECTS]
        # 归档凭证（cold_archive 已确认归档回写，retention 按对象和版本核对）
        self._archive_credentials: list[dict[str, Any]] = [
            {**rec, "覆盖分区": list(rec["覆盖分区"])} for rec in SEED_ARCHIVE_CREDENTIALS
        ]
        # 快照覆盖详情（snapshot_mgr → retention 恢复依据计算）
        self._snapshot_details: dict[str, dict[str, Any]] = {
            sid: {**detail, "覆盖分区": list(detail["覆盖分区"])}
            for sid, detail in SEED_SNAPSHOT_DETAILS.items()
        }
        # 清理预览单（冻结生成时条件，历史预览按原条件可复核）
        self._cleanup_previews: dict[str, dict[str, Any]] = {}
        # 清理执行清单
        self._cleanup_executions: list[dict[str, Any]] = []

    # ---------- 基础 CRUD ----------
    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        # 跨模块流水线状态
        pipeline_stats = []
        for pname, pl in self._pipelines.items():
            pipeline_stats.append(pl.health_report())
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
            {"label": "流水线", "value": len(pipeline_stats)},
        ]
        return {"cards": cards, "modules": modules, "pipelines": pipeline_stats}

    # ---------- 跨模块流水线 ----------
    def create_pipeline(self, name: str, sources: list[str], target_module: str) -> DataPipeline:
        pl = DataPipeline(name, sources, target_module)
        self._pipelines[name] = pl
        return pl

    def pipeline(self, name: str) -> DataPipeline | None:
        return self._pipelines.get(name)

    def all_pipelines(self) -> dict[str, DataPipeline]:
        return dict(self._pipelines)

    # ---------- Schema 注册表 ----------
    def register_schema(self, topic: str, schema_def: dict[str, Any], version: str) -> dict[str, Any]:
        versions = self._schema_registry.setdefault(topic, [])
        entry = {"topic": topic, "definition": schema_def, "version": version, "ts": time.time()}
        versions.append(entry)
        return entry

    def schema_versions(self, topic: str) -> list[dict[str, Any]]:
        return self._schema_registry.get(topic, [])

    def check_schema_compatibility(self, topic: str, new_fields: list[str]) -> dict[str, Any]:
        versions = self.schema_versions(topic)
        if not versions:
            return {"compatible": True, "reason": "新主题，无兼容性问题"}
        latest = versions[-1]
        old_fields = set(latest.get("definition", {}).get("fields", []))
        missing_required = [f for f in old_fields if f not in new_fields]
        if missing_required:
            return {"compatible": False, "reason": f"缺少已有字段: {missing_required}"}
        return {"compatible": True, "reason": "向后兼容"}

    # ---------- 水位记录 ----------
    def update_watermark(self, partition: str, offset: dict[str, Any]) -> None:
        self._watermark_offsets[partition] = offset

    def get_watermark(self, partition: str) -> dict[str, Any] | None:
        return self._watermark_offsets.get(partition)

    # ---------- 快照存储 ----------
    def save_snapshot(self, snap_id: str, data: dict[str, Any]) -> None:
        self._snapshot_store[snap_id] = data

    def get_snapshot(self, snap_id: str) -> dict[str, Any] | None:
        return self._snapshot_store.get(snap_id)

    # ---------- 变更日志 ----------
    def append_changelog(self, entry: dict[str, Any]) -> None:
        entry["_ts"] = time.time()
        self._global_changelog.append(entry)

    def changelog_for_table(self, table_name: str) -> list[dict[str, Any]]:
        return [e for e in self._global_changelog if e.get("目标表") == table_name]

    # ---------- 血缘图 ----------
    def add_lineage_edge(self, upstream: str, downstream: str, edge_data: dict[str, Any]) -> None:
        edges = self._lineage_graph.setdefault(upstream, [])
        edges.append({"downstream": downstream, **edge_data, "ts": time.time()})

    def lineage_downstream(self, table_name: str) -> list[dict[str, Any]]:
        return self._lineage_graph.get(table_name, [])

    # ---------- 归档清单 ----------
    def add_archive_record(self, record: dict[str, Any]) -> None:
        self._archive_manifest.append(record)

    def archive_manifest_for_table(self, table_name: str) -> list[dict[str, Any]]:
        return [r for r in self._archive_manifest if r.get("源表名称") == table_name]

    # ---------- 清理对象（retention 清理预览） ----------
    def cleanup_objects_for_table(self, table_name: str, *, include_cleaned: bool = False) -> list[dict[str, Any]]:
        return [
            obj for obj in self._cleanup_objects
            if obj.get("表名") == table_name and (include_cleaned or not obj.get("已清理"))
        ]

    def mark_cleanup_object_cleaned(self, table_name: str, partition: str, exec_id: str) -> None:
        for obj in self._cleanup_objects:
            if obj.get("表名") == table_name and obj.get("分区") == partition:
                obj["已清理"] = True
                obj["清理时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
                obj["清理执行编号"] = exec_id

    # ---------- 归档凭证 ----------
    def all_archive_credentials(self) -> list[dict[str, Any]]:
        return list(self._archive_credentials)

    def credentials_for_table(self, table_name: str, *, confirmed_only: bool = True) -> list[dict[str, Any]]:
        return [
            c for c in self._archive_credentials
            if c.get("表名") == table_name and (not confirmed_only or c.get("状态") == "已确认")
        ]

    def find_credential(self, table_name: str, partition: str, version: str) -> dict[str, Any] | None:
        """按对象（表+分区）和版本核对已确认归档凭证。"""
        for cred in self._archive_credentials:
            if (
                cred.get("表名") == table_name
                and cred.get("状态") == "已确认"
                and cred.get("归档版本") == version
                and partition in cred.get("覆盖分区", [])
            ):
                return cred
        return None

    def add_archive_credential(self, record: dict[str, Any]) -> dict[str, Any]:
        seq = len(self._archive_credentials) + 1
        record = {**record, "凭证编号": record.get("凭证编号") or f"CRED-{seq:04d}"}
        record["覆盖分区"] = list(record.get("覆盖分区", []))
        self._archive_credentials.append(record)
        return record

    # ---------- 快照覆盖详情 ----------
    def snapshot_detail(self, snap_id: str) -> dict[str, Any] | None:
        return self._snapshot_details.get(snap_id)

    def ensure_snapshot_detail(self, snap_id: str, detail: dict[str, Any]) -> dict[str, Any]:
        existing = self._snapshot_details.get(snap_id)
        if existing is not None:
            return existing
        self._snapshot_details[snap_id] = {**detail, "覆盖分区": list(detail.get("覆盖分区", []))}
        return self._snapshot_details[snap_id]

    def readable_snapshots_for_table(self, table_name: str) -> list[dict[str, Any]]:
        """仍可读取的快照：snapshot_mgr 状态可读且未撤销，按快照时间倒序。"""
        readable_status = {"已完成", "已恢复"}
        result = []
        for row in self.rows("snapshot_mgr"):
            if row.get("源表名称") != table_name or row.get("status") not in readable_status:
                continue
            snap_id = str(row.get("快照编号", ""))
            detail = self._snapshot_details.get(snap_id, {})
            if detail.get("revoked"):
                continue
            result.append({
                "快照编号": snap_id,
                "版本": str(row.get("快照版本", "")),
                "快照时间": str(row.get("快照时间", "")),
                "存储路径": str(row.get("存储路径", "")),
                "覆盖分区": list(detail.get("覆盖分区", [])),
                "_ts": float(detail.get("_ts", 0)),
            })
        result.sort(key=lambda s: s["_ts"], reverse=True)
        return result

    def revoke_snapshot(self, snap_id: str) -> tuple[bool, str]:
        """撤销快照：状态置为已过期并标记 revoked，返回 (是否成功, 说明)。"""
        target = None
        for row in self.rows("snapshot_mgr"):
            if row.get("快照编号") == snap_id:
                target = row
                break
        if target is None:
            return False, f"快照 {snap_id} 不存在"
        detail = self._snapshot_details.setdefault(snap_id, {"覆盖分区": []})
        if detail.get("revoked"):
            return False, f"快照 {snap_id} 已撤销，不可重复操作"
        target["status"] = "已过期"
        target["pending"] = False
        detail["revoked"] = True
        detail["撤销时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return True, f"快照 {snap_id} 已撤销"

    # ---------- 清理预览单 ----------
    def save_cleanup_preview(self, preview: dict[str, Any]) -> None:
        self._cleanup_previews[preview["preview_id"]] = preview

    def get_cleanup_preview(self, preview_id: str) -> dict[str, Any] | None:
        return self._cleanup_previews.get(preview_id)

    def list_cleanup_previews(self) -> list[dict[str, Any]]:
        return sorted(
            self._cleanup_previews.values(),
            key=lambda p: p.get("generated_at_ts", 0),
            reverse=True,
        )

    # ---------- 清理执行清单 ----------
    def add_cleanup_execution(self, execution: dict[str, Any]) -> None:
        self._cleanup_executions.append(execution)

    def list_cleanup_executions(self) -> list[dict[str, Any]]:
        return list(self._cleanup_executions)


store = Store()