"""解析指标领域服务：按窗口和来源汇总吞吐、隔离率与回退率统计，产出指标读数数据集。

事实读取链路（重构后）：
- 手工结算（settle_reading）与管线结算（pipeline.settle_pipeline_reading）都从
  app.facts 的共享已闭合批次事实来源取数，两个入口拿到的是同一份可独立核对的事实；
- 每次结算把事实快照（闭合范围、闭合凭据、逐批闭合证据、分子分母）整体挂在读数上，
  历史读数保留当时的分母和原窗口证据，之后新闭合的行不会覆盖它；
- 跨表复核（cross_check）核对读数自身快照的内部一致性，历史读数随时可重开原范围；
- 与实时事实的差异走 reconcile_live，只报告不覆盖；
- 闭合数与逐批事实冲突时读数落为待复核，服务端结果与完整结算范围仍可查询。

原有过滤（来源渠道精确匹配、耗时纯数字过滤）、零分母归零、round(x, 4)、
均值保留两位小数、空输入归零的口径全部不变，只是取数范围由全部批次收紧为
同一组已闭合批次。
"""
from __future__ import annotations

from typing import Any

from app.facts import SettlementFacts, fact_source
from app.services.base import BaseModuleService
from app.store import store


class ParseMetricService(BaseModuleService):
    MODULE = "parse_metric"
    ENTITY = "指标读数"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["指标读数编号", "统计窗口", "来源渠道", "解析吞吐行数", "平均解析耗时", "隔离率", "回退率", "采集时间"]
    REQUIRED_FIELDS = ["指标读数编号", "统计窗口", "来源渠道"]
    STATUS_ORDER = ["统计中", "已结算", "待复核", "已作废"]
    ACTION_RULES = {"提交结算": "已结算", "复核读数": "待复核", "作废读数": "已作废"}
    NEGATIVE_ACTIONS = ["作废读数"]

    SNAPSHOT_FIELD = "结算事实快照"
    SCOPE_FIELD = "闭合批次范围"

    @staticmethod
    def _int(row: dict[str, Any], field: str) -> int:
        text = str(row.get(field) or "").strip()
        return int(text) if text.isdigit() else 0

    @staticmethod
    def _float(row: dict[str, Any], field: str) -> float:
        try:
            return float(str(row.get(field) or "").strip())
        except ValueError:
            return 0.0

    # ------------------------------------------------------------------
    # 实时事实（两个结算入口的共同读数来源）
    # ------------------------------------------------------------------
    def channel_facts(self, channel: str,
                      delivered_close_count: int | None = None) -> SettlementFacts:
        """读取渠道当前的已闭合批次事实快照，手工/管线入口共用。"""
        return fact_source.build(channel, delivered_close_count=delivered_close_count)

    def real_quarantine_rate(self, channel: str) -> tuple[float, int, int]:
        """隔离率来自共享事实来源的真实闭合批次计数，不做估算。

        返回 (隔离率, 隔离行数, 闭合批次总行数)。
        """
        facts = fact_source.build(channel)
        return facts.quarantine_rate, facts.quarantined_rows, facts.total_rows

    def real_fallback_rate(self, channel: str) -> tuple[float, int, int]:
        """回退率来自解析路由在闭合批次范围内的真实回退计数。

        返回 (回退率, 发生回退的决策数, 决策总数)。
        """
        facts = fact_source.build(channel)
        return facts.fallback_rate, facts.fallback_decisions, facts.total_decisions

    # ------------------------------------------------------------------
    # 结算：手工入口
    # ------------------------------------------------------------------
    def settle_reading(self, reading_no: str, window: str, channel: str,
                       collected_at: str,
                       delivered_close_count: int | None = None,
                       ) -> tuple[bool, str, dict[str, Any] | None]:
        """结算指标读数：全部指标取自同一组已闭合批次的共享事实。"""
        facts = fact_source.build(
            channel, delivered_close_count=delivered_close_count)
        self._persist_reading(reading_no, window, channel, collected_at, facts)
        target = next(
            (r for r in store.rows(self.MODULE)
             if r.get("指标读数编号") == reading_no), None)
        return True, self._settle_message(facts), dict(target) if target else None

    def _persist_reading(self, reading_no: str, window: str, channel: str,
                         collected_at: str, facts: SettlementFacts) -> bool:
        rows = store.rows(self.MODULE)
        target = next((r for r in rows if r.get("指标读数编号") == reading_no), None)
        if target is None:
            entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            target = {"id": entry_id, "指标读数编号": reading_no}
            rows.append(target)

        status = "待复核" if facts.needs_review else "已结算"
        target.update({
            "统计窗口": window,
            "来源渠道": channel,
            "解析吞吐行数": str(facts.throughput_rows),
            "平均解析耗时": f"{facts.avg_parse_ms:.2f}",
            "隔离率": f"{facts.quarantine_rate:.4f}",
            "回退率": f"{facts.fallback_rate:.4f}",
            "采集时间": collected_at,
            "隔离行计数": str(facts.quarantined_rows),
            "批次总行计数": str(facts.total_rows),
            "回退决策计数": str(facts.fallback_decisions),
            "路由决策计数": str(facts.total_decisions),
            "已闭合批次数": str(facts.closed_batch_count),
            self.SCOPE_FIELD: "、".join(facts.close_credentials),
            # 不可变事实快照：历史读数的分母与原窗口证据就此封存
            self.SNAPSHOT_FIELD: facts.to_json(),
            "复核原因": "；".join(facts.conflicts) if facts.conflicts else "",
            "status": status,
            "pending": facts.needs_review,
            "abnormal": facts.needs_review,
        })
        return True

    @staticmethod
    def _settle_message(facts: SettlementFacts) -> str:
        message = (
            f"指标读数已结算：闭合批次 {facts.closed_batch_count} 个，"
            f"吞吐 {facts.throughput_rows} 行，"
            f"隔离率 {facts.quarantine_rate:.2%}"
            f"（{facts.quarantined_rows}/{facts.total_rows}），"
            f"回退率 {facts.fallback_rate:.2%}"
            f"（{facts.fallback_decisions}/{facts.total_decisions}）"
        )
        if facts.needs_review:
            message = f"闭合数与逐批事实冲突，读数转待复核；{message}；" + "；".join(facts.conflicts)
        elif facts.duplicate_credentials:
            message += "；重复闭合凭据已去重：" + "、".join(
                d["闭合凭据"] for d in facts.duplicate_credentials)
        return message

    # ------------------------------------------------------------------
    # 结算：管线入口（与手工入口共用事实来源）
    # ------------------------------------------------------------------
    def settle_pipeline_reading(
        self,
        reading_no: str,
        window: str,
        channel: str,
        collected_at: str,
        parse_durations_ms: list[float] | None = None,
        delivered_close_count: int | None = None,
    ) -> tuple[bool, str, dict[str, Any], SettlementFacts]:
        """管线结算：管线先发布完成证明，再与手工入口读同一份事实重算读数。

        管线显式给出的耗时序列只在存在时覆盖事实来源里的字段解析耗时，
        其余分子分母一律以闭合批次事实为准，保证同一读数不夹两个结算范围。
        """
        facts = fact_source.build(
            channel, delivered_close_count=delivered_close_count)
        if parse_durations_ms:
            facts.avg_parse_ms = round(
                sum(parse_durations_ms) / len(parse_durations_ms), 2)
            facts.duration_samples = len(parse_durations_ms)

        self._persist_reading(reading_no, window, channel, collected_at, facts)
        target = next(
            r for r in store.rows(self.MODULE)
            if r.get("指标读数编号") == reading_no
        )
        return (not facts.needs_review,
                self._settle_message(facts), dict(target), facts)

    def needs_review(self, reading_no: str, reason: str) -> tuple[bool, str]:
        """标记待复核：结算读数与其他模块真实计数对不上时使用。"""
        for row in store.rows(self.MODULE):
            if row.get("指标读数编号") == reading_no:
                row["status"] = "待复核"
                row["pending"] = True
                row["abnormal"] = True
                row["复核原因"] = reason
                return True, f"指标读数 {reading_no} 已转入待复核：{reason}"
        return False, f"指标读数 {reading_no} 不存在"

    # ------------------------------------------------------------------
    # 复核
    # ------------------------------------------------------------------
    def stored_snapshot(self, reading_no: str) -> SettlementFacts | None:
        """取出读数封存的事实快照，历史读数凭它重开当时的完整结算范围。"""
        row = next(
            (r for r in store.rows(self.MODULE)
             if r.get("指标读数编号") == reading_no), None)
        if row is None or not row.get(self.SNAPSHOT_FIELD):
            return None
        return SettlementFacts.from_json(str(row[self.SNAPSHOT_FIELD]))

    def cross_check(self, reading_no: str) -> tuple[bool, str]:
        """核对读数与自身封存事实是否一致，防止估算值入库。

        历史读数核对的是结算当时封存的快照，因此不会被后来闭合的行覆盖。
        """
        row = next(
            (r for r in store.rows(self.MODULE)
             if r.get("指标读数编号") == reading_no), None)
        if row is None:
            return False, f"指标读数 {reading_no} 不存在"

        snapshot = self.stored_snapshot(reading_no)
        if snapshot is None:
            return False, f"读数 {reading_no} 缺少结算事实快照，无法核对来源分子"

        recorded_quarantined = self._int(row, "隔离行计数")
        if recorded_quarantined != snapshot.quarantined_rows:
            return False, (
                f"读数 {reading_no} 记录的隔离行 {recorded_quarantined} "
                f"与封存事实真实计数 {snapshot.quarantined_rows} 不一致，读数不得结算"
            )
        recorded_total = self._int(row, "批次总行计数")
        if recorded_total != snapshot.total_rows:
            return False, (
                f"读数 {reading_no} 记录的分母 {recorded_total} "
                f"与封存闭合范围分母 {snapshot.total_rows} 不一致"
            )
        recorded_rate = self._float(row, "隔离率")
        if abs(recorded_rate - snapshot.quarantine_rate) > 0.0001:
            return False, (
                f"读数 {reading_no} 隔离率 {recorded_rate:.4f} "
                f"与真实计数 {snapshot.quarantine_rate:.4f}"
                f"（{snapshot.quarantined_rows}/{snapshot.total_rows}）不一致"
            )
        recorded_throughput = self._int(row, "解析吞吐行数")
        if recorded_throughput != snapshot.throughput_rows:
            return False, (
                f"读数 {reading_no} 吞吐 {recorded_throughput} "
                f"与封存闭合范围吞吐 {snapshot.throughput_rows} 不一致"
            )
        return True, (
            f"读数 {reading_no} 与封存事实一致：闭合 {snapshot.closed_batch_count} 批，"
            f"隔离 {snapshot.quarantined_rows}/{snapshot.total_rows} "
            f"= {snapshot.quarantine_rate:.4f}，凭据 {snapshot.close_credentials}"
        )

    def reconcile_live(self, reading_no: str) -> tuple[bool, str, dict[str, Any]]:
        """把历史读数与当前实时事实对照，只报告差异，不改写历史分母与窗口证据。"""
        row = next(
            (r for r in store.rows(self.MODULE)
             if r.get("指标读数编号") == reading_no), None)
        if row is None:
            return False, f"指标读数 {reading_no} 不存在", {}
        snapshot = self.stored_snapshot(reading_no)
        if snapshot is None:
            return False, f"读数 {reading_no} 缺少结算事实快照", {}

        live = fact_source.build(str(row.get("来源渠道") or ""))
        new_credentials = sorted(
            set(live.close_credentials) - set(snapshot.close_credentials))
        retired_credentials = sorted(
            set(snapshot.close_credentials) - set(live.close_credentials))
        comparison = {
            "reading_no": reading_no,
            "snapshot_closed_count": snapshot.closed_batch_count,
            "live_closed_count": live.closed_batch_count,
            "snapshot_credentials": snapshot.close_credentials,
            "live_credentials": live.close_credentials,
            "new_credentials": new_credentials,
            "retired_credentials": retired_credentials,
            "snapshot_throughput": snapshot.throughput_rows,
            "live_throughput": live.throughput_rows,
            "snapshot_denominator": snapshot.total_rows,
            "live_denominator": live.total_rows,
            "historical_preserved": True,
            "live_conflicts": live.conflicts,
        }
        if new_credentials or retired_credentials:
            message = (
                f"读数 {reading_no} 历史范围 {snapshot.closed_batch_count} 批保持不变；"
                f"当前实时范围 {live.closed_batch_count} 批，"
                f"新增闭合凭据 {new_credentials or '无'}，历史凭据可随时重开"
            )
        else:
            message = (
                f"读数 {reading_no} 与实时事实范围一致（{live.closed_batch_count} 批），"
                f"历史分母 {snapshot.total_rows} 未被覆盖"
            )
        return True, message, comparison

    def reading_evidence(self, reading_no: str) -> dict[str, Any] | None:
        """指标复核员从同一读数操作打开每笔真实闭合证据。"""
        snapshot = self.stored_snapshot(reading_no)
        if snapshot is None:
            return None
        return {
            "reading_no": reading_no,
            "channel": snapshot.channel,
            "generated_at": snapshot.generated_at,
            "closed_batch_count": snapshot.closed_batch_count,
            "close_credentials": snapshot.close_credentials,
            "intake_batches": snapshot.intake_batches,
            "closed_batches": snapshot.closed_batches,
            "duplicate_credentials": snapshot.duplicate_credentials,
            "quarantined_rows": snapshot.quarantined_rows,
            "total_rows": snapshot.total_rows,
            "quarantine_rate": snapshot.quarantine_rate,
            "fallback_decisions": snapshot.fallback_decisions,
            "total_decisions": snapshot.total_decisions,
            "fallback_rate": snapshot.fallback_rate,
            "throughput_rows": snapshot.throughput_rows,
            "avg_parse_ms": snapshot.avg_parse_ms,
            "delivered_close_count": snapshot.delivered_close_count,
            "conflicts": snapshot.conflicts,
            "needs_review": snapshot.needs_review,
        }

    def channel_throughput(self) -> dict[str, int]:
        """按来源渠道汇总吞吐行数，供分析看板消费。"""
        totals: dict[str, int] = {}
        for row in store.rows(self.MODULE):
            if row.get("status") == "已作废":
                continue
            channel = str(row.get("来源渠道") or "")
            totals[channel] = totals.get(channel, 0) + self._int(row, "解析吞吐行数")
        return totals

    # ------------------------------------------------------------------
    # 动作分派：页面上的「提交结算」直接走共享事实来源
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action != "提交结算":
            return super().run_action(entry_id, action)
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        self.settle_reading(
            str(entry.get("指标读数编号") or ""),
            str(entry.get("统计窗口") or ""),
            str(entry.get("来源渠道") or ""),
            str(entry.get("采集时间") or ""),
        )
        return entry, f"{self.ENTITY}已按已闭合批次事实{action}"
