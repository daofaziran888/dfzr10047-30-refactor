import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 结算事实核对相关类型，与后端 app/facts 的快照结构对应。 */
export type ClosedBatchEvidenceItem = {
  batch_record_no: string
  intake_batch: string
  parser: string
  close_credential: string
  close_time: string
  total_rows: number
  success_rows: number
  quarantine_rows: number
  quarantine_credentials: string[]
}

export type DuplicateCredential = {
  批次记录编号: string
  关联投递批次: string
  闭合凭据: string
  重复原因: string
}

export type ReadingEvidence = {
  reading_no: string
  channel: string
  generated_at: string
  closed_batch_count: number
  close_credentials: string[]
  intake_batches: string[]
  closed_batches: ClosedBatchEvidenceItem[]
  duplicate_credentials: DuplicateCredential[]
  quarantined_rows: number
  total_rows: number
  quarantine_rate: number
  fallback_decisions: number
  total_decisions: number
  fallback_rate: number
  throughput_rows: number
  avg_parse_ms: number
  delivered_close_count: number
  conflicts: string[]
  needs_review: boolean
}

export type ReconcileComparison = {
  reading_no: string
  snapshot_closed_count: number
  live_closed_count: number
  snapshot_credentials: string[]
  live_credentials: string[]
  new_credentials: string[]
  retired_credentials: string[]
  snapshot_throughput: number
  live_throughput: number
  snapshot_denominator: number
  live_denominator: number
  historical_preserved: boolean
  live_conflicts: string[]
}

async function fetchFact<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: '事实核对接口不可用' }))
    throw new Error(detail.detail ?? '事实核对接口不可用')
  }
  return (await response.json()) as T
}

/** 从同一读数打开每笔真实闭合证据（历史读数返回当时封存范围）。 */
export function loadReadingEvidence(readingNo: string): Promise<ReadingEvidence> {
  return fetchFact<ReadingEvidence>(
    `/api/settlement-facts/readings/${encodeURIComponent(readingNo)}/evidence`,
  )
}

/** 历史读数与当前实时事实对照，只报告差异，不改写历史分母与窗口证据。 */
export function reconcileReading(
  readingNo: string,
): Promise<{ ok: boolean; message: string; comparison: ReconcileComparison }> {
  return fetchFact(`/api/settlement-facts/readings/${encodeURIComponent(readingNo)}/reconcile`)
}

/** 读数与其封存事实的内部一致性核对。 */
export function crossCheckReading(
  readingNo: string,
): Promise<{ ok: boolean; message: string }> {
  return fetchFact(`/api/settlement-facts/readings/${encodeURIComponent(readingNo)}/cross-check`)
}
