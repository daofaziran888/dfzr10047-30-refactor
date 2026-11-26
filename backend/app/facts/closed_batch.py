"""结算事实来源：手工结算与管线结算共用的已闭合批次事实读取链路。

设计目标：
- 「已闭合」判定只有这一处实现，解析批次服务、解析水位服务、解析指标服务都从这里读；
- 每个闭合批次都带一笔可独立核对的闭合凭据（``闭合凭据``），同一凭据被上报两次只采一次；
- 同一次读数的吞吐、隔离分子/分母、回退分子/分母、耗时均值都取自同一组闭合批次，
  不会夹在两个不同结算范围里；
- 事实快照可序列化后挂在指标读数上，历史读数保留当时的分母与原窗口证据，
  之后新闭合的行不会覆盖历史读数；
- 交付的闭合数与逐批事实冲突时只标待复核，服务端算出的结果与完整结算范围仍可查询。

原行为保持不变：渠道过滤、零分母归零、``round(x, 4)`` 与 ``f"{x:.2f}"`` 舍入、
空输入返回 0、整数仅认纯数字字符串。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from app.store import store

# 闭合状态在不同历史数据里出现过两种写法，统一在这里收口。
CLOSED_BATCH_STATES = ("已闭合", "闭合")


def is_closed_batch(row: dict[str, Any]) -> bool:
    """唯一的已闭合判定：解析批次行处于闭合状态即计入，作废/在途一律不计。"""
    return str(row.get("批次状态") or "").strip() in CLOSED_BATCH_STATES


def _int(row: dict[str, Any], name: str) -> int:
    text = str(row.get(name) or "").strip()
    return int(text) if text.isdigit() else 0


def _float(value: Any) -> float:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return 0.0


@dataclass
class ClosedBatchEvidence:
    """单笔闭合证据：指标复核员从读数里能逐笔打开的真实闭合事实。"""

    batch_record_no: str
    intake_batch: str
    parser: str
    close_credential: str
    close_time: str
    total_rows: int
    success_rows: int
    quarantine_rows: int
    quarantine_credentials: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SettlementFacts:
    """一次结算读数对应的完整事实快照（手工/管线两个入口共用同一份结构）。"""

    channel: str
    generated_at: str
    # 去重后真正计入的闭合范围
    closed_batch_count: int
    throughput_rows: int
    close_credentials: list[str]
    intake_batches: list[str]
    closed_batches: list[dict[str, Any]]
    # 被上报多次、只采一次的闭合凭据
    duplicate_credentials: list[dict[str, Any]]
    # 隔离率：分子分母来自同一组闭合批次
    quarantined_rows: int
    total_rows: int
    quarantine_rate: float
    # 回退率：决策只取闭合批次对应的投递批次
    fallback_decisions: int
    total_decisions: int
    fallback_rate: float
    # 耗时均值与吞吐同范围
    avg_parse_ms: float
    duration_samples: int
    # 服务端登记收到的闭合证明份数（含重复证明）
    delivered_close_count: int
    conflicts: list[str]
    needs_review: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> "SettlementFacts":
        data = json.loads(payload)
        return cls(**data)


class ClosedBatchFactSource:
    """已闭合批次事实来源：所有结算读数只允许从这里取数。"""

    # ------------------------------------------------------------------
    # 闭合范围与凭据
    # ------------------------------------------------------------------
    def channel_intake_batches(self, channel: str) -> set[str]:
        """渠道过滤沿用原口径：file_intake.来源渠道 精确相等。"""
        return {
            str(r.get("批次编号"))
            for r in store.rows("file_intake")
            if r.get("来源渠道") == channel
        }

    def closed_batch_rows(self, channel: str) -> list[dict[str, Any]]:
        """读取渠道下全部已闭合解析批次行（含重复证明，尚未去重）。"""
        intake_batches = self.channel_intake_batches(channel)
        return [
            row for row in store.rows("parse_batch")
            if is_closed_batch(row)
            and str(row.get("关联投递批次")) in intake_batches
        ]

    @staticmethod
    def credential_of(row: dict[str, Any]) -> str:
        """闭合凭据优先取批次登记值；历史数据缺字段时回退到批次记录编号，

        回退值天然唯一，保证老数据行为不变。
        """
        credential = str(row.get("闭合凭据") or "").strip()
        return credential or f"LEGACY-{row.get('批次记录编号')}"

    def dedup_closed_batches(
        self, rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """闭合证明去重：同一闭合凭据、或同一投递批次的多份证明都只采一次。

        返回 (唯一闭合批次行, 重复证明明细)。保留首次出现的证明，后续重复只登记不计数。
        """
        unique: list[dict[str, Any]] = []
        seen_credentials: set[str] = set()
        seen_intake: set[str] = set()
        duplicates: list[dict[str, Any]] = []
        for row in rows:
            credential = self.credential_of(row)
            intake_batch = str(row.get("关联投递批次"))
            reasons = []
            if credential in seen_credentials:
                reasons.append("闭合凭据重复")
            if intake_batch in seen_intake:
                reasons.append("同批次重复闭合")
            if reasons:
                duplicates.append({
                    "批次记录编号": row.get("批次记录编号"),
                    "关联投递批次": intake_batch,
                    "闭合凭据": credential,
                    "重复原因": "、".join(reasons),
                })
                continue
            seen_credentials.add(credential)
            seen_intake.add(intake_batch)
            unique.append(row)
        return unique, duplicates

    # ------------------------------------------------------------------
    # 逐批事实
    # ------------------------------------------------------------------
    def _evidence(self, row: dict[str, Any]) -> ClosedBatchEvidence:
        intake_batch = str(row.get("关联投递批次"))
        quarantine_rows = [
            q for q in store.rows("row_quarantine")
            if str(q.get("关联批次")) == intake_batch
        ]
        return ClosedBatchEvidence(
            batch_record_no=str(row.get("批次记录编号") or ""),
            intake_batch=intake_batch,
            parser=str(row.get("解析器标识") or ""),
            close_credential=self.credential_of(row),
            close_time=str(row.get("完成时间") or ""),
            total_rows=_int(row, "总行数"),
            success_rows=_int(row, "成功行数"),
            quarantine_rows=len(quarantine_rows),
            quarantine_credentials=[
                str(q.get("隔离编号") or "") for q in quarantine_rows
            ],
        )

    # ------------------------------------------------------------------
    # 主入口：构建一次读数的完整事实快照
    # ------------------------------------------------------------------
    def build(
        self,
        channel: str,
        *,
        delivered_close_count: int | None = None,
    ) -> SettlementFacts:
        raw_closed = self.closed_batch_rows(channel)
        unique_rows, duplicates = self.dedup_closed_batches(raw_closed)
        evidence = [self._evidence(row) for row in unique_rows]

        intake_batches = sorted(e.intake_batch for e in evidence)
        credentials = sorted(e.close_credential for e in evidence)
        throughput = sum(e.total_rows for e in evidence)
        quarantined = sum(e.quarantine_rows for e in evidence)
        # 隔离率分母与吞吐同源：闭合批次总行数；零分母维持原归零口径。
        quarantine_rate = (
            round(quarantined / throughput, 4) if throughput > 0 else 0.0
        )

        # 回退决策同样只取闭合批次对应的投递批次，分子分母同范围。
        decisions = [
            d for d in store.rows("parser_route")
            if str(d.get("关联批次")) in set(intake_batches)
        ]
        fallbacks = sum(1 for d in decisions if _int(d, "回退次数") > 0)
        fallback_rate = (
            round(fallbacks / len(decisions), 4) if decisions else 0.0
        )

        # 耗时过滤与原实现一致：去掉小数点后必须是纯数字，空输入均值为 0。
        durations = [
            _float(p.get("解析耗时"))
            for p in store.rows("field_parse")
            if str(p.get("关联批次")) in set(intake_batches)
            and str(p.get("解析耗时") or "").replace(".", "").isdigit()
        ]
        avg_ms = round(sum(durations) / len(durations), 2) if durations else 0.0

        # 服务端收到的证明份数：显式传入时以传入值为准（模拟外部交付登记），
        # 否则按库里实际闭合证明行数统计。
        delivered = (
            delivered_close_count
            if delivered_close_count is not None
            else len(raw_closed)
        )
        conflicts: list[str] = []
        # 重复闭合凭据属于去重场景：只采一次、登记明细，不升级为待复核。
        for dup in duplicates:
            conflicts.append(
                f"闭合凭据 {dup['闭合凭据']} 被重复上报，只采一次"
            )
        # 只有外部交付的闭合数与逐批事实（去重后）对不上才是真正冲突。
        delivered_conflict = (
            delivered_close_count is not None
            and delivered_close_count != len(unique_rows)
        )
        if delivered_conflict:
            conflicts.append(
                f"交付闭合数 {delivered_close_count} 与逐批事实闭合数 "
                f"{len(unique_rows)} 冲突"
            )

        return SettlementFacts(
            channel=channel,
            generated_at=datetime.now().isoformat(timespec="seconds"),
            closed_batch_count=len(unique_rows),
            throughput_rows=throughput,
            close_credentials=credentials,
            intake_batches=intake_batches,
            closed_batches=[e.to_dict() for e in evidence],
            duplicate_credentials=duplicates,
            quarantined_rows=quarantined,
            total_rows=throughput,
            quarantine_rate=quarantine_rate,
            fallback_decisions=fallbacks,
            total_decisions=len(decisions),
            fallback_rate=fallback_rate,
            avg_parse_ms=avg_ms,
            duration_samples=len(durations),
            delivered_close_count=delivered,
            conflicts=conflicts,
            # 闭合数与逐批事实冲突才标待复核；凭据重复已静默去重，不算冲突阻断。
            needs_review=delivered_conflict,
        )

    # ------------------------------------------------------------------
    # 历史快照
    # ------------------------------------------------------------------
    @staticmethod
    def snapshot_from_json(payload: str) -> SettlementFacts:
        """从指标读数上挂的事实快照恢复历史结算范围。"""
        return SettlementFacts.from_json(payload)


fact_source = ClosedBatchFactSource()
