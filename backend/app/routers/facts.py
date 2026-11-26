"""结算事实核对接口：页面从读数直接打开闭合范围、逐笔闭合证据与实时对照。

这些接口只读 app.facts 共享事实来源与读数封存快照，不改变任何结算结果；
冲突时也能按服务端原结果查询，并可重开读数当时的完整结算范围。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.facts import fact_source
from app.services import service_for

router = APIRouter(prefix="/api/settlement-facts", tags=["结算事实核对"])


@router.get("/channels/{channel}")
def channel_facts(channel: str, delivered_close_count: int | None = None) -> dict:
    """实时读取渠道已闭合批次事实：去重后的闭合范围与逐批证据。"""
    facts = fact_source.build(
        channel, delivered_close_count=delivered_close_count)
    return facts.to_dict()


@router.get("/readings/{reading_no}/evidence")
def reading_evidence(reading_no: str) -> dict:
    """从同一读数打开每笔真实闭合证据（历史读数返回当时封存的范围）。"""
    metric_svc = service_for("parse_metric")
    evidence = metric_svc.reading_evidence(reading_no)
    if evidence is None:
        raise HTTPException(
            status_code=404,
            detail=f"指标读数 {reading_no} 不存在或缺少结算事实快照")
    return evidence


@router.get("/readings/{reading_no}/reconcile")
def reconcile_reading(reading_no: str) -> dict:
    """历史读数与当前实时事实对照：只报告差异，历史分母与窗口证据保持不变。"""
    metric_svc = service_for("parse_metric")
    ok, message, comparison = metric_svc.reconcile_live(reading_no)
    if not ok:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "comparison": comparison}


@router.get("/readings/{reading_no}/cross-check")
def cross_check_reading(reading_no: str) -> dict:
    """读数与其封存事实的内部一致性核对，来源分子可追溯。"""
    metric_svc = service_for("parse_metric")
    ok, message = metric_svc.cross_check(reading_no)
    return {"ok": ok, "message": message}
