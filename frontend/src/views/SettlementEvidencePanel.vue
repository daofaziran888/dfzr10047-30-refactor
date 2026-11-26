<template>
  <div v-if="visible" class="modal-backdrop" data-testid="evidence-modal" @click.self="close">
    <section class="modal evidence-modal" role="dialog" aria-modal="true">
      <header class="evidence-head">
        <h3>结算事实核对 · {{ row?.['指标读数编号'] }}</h3>
        <button class="btn ghost" type="button" data-testid="evidence-close" @click="close">关闭</button>
      </header>

      <p v-if="loading" class="evidence-loading">正在读取已闭合批次事实…</p>
      <p v-else-if="errorMessage" class="error-text">{{ errorMessage }}</p>

      <template v-else-if="evidence">
        <div class="evidence-banner" :class="{ review: evidence.needs_review }">
          <span>
            已闭合 <strong>{{ evidence.closed_batch_count }}</strong> 批 ·
            吞吐 <strong>{{ evidence.throughput_rows }}</strong> 行 ·
            隔离 <strong>{{ evidence.quarantined_rows }}/{{ evidence.total_rows }}</strong>
            （{{ formatRate(evidence.quarantine_rate) }}）·
            回退 <strong>{{ evidence.fallback_decisions }}/{{ evidence.total_decisions }}</strong>
            （{{ formatRate(evidence.fallback_rate) }}）·
            平均耗时 {{ evidence.avg_parse_ms.toFixed(2) }}
          </span>
          <span v-if="evidence.needs_review" class="review-tag" data-testid="review-tag">待复核</span>
        </div>

        <div class="evidence-scope">
          <h4>计入的闭合范围（凭据）</h4>
          <ul class="credential-list">
            <li v-for="credential in evidence.close_credentials" :key="credential" class="credential-item">
              <code>{{ credential }}</code>
            </li>
          </ul>
          <p class="evidence-meta">
            事实生成时间 {{ evidence.generated_at }} · 服务端登记闭合证明
            {{ evidence.delivered_close_count }} 份
          </p>
        </div>

        <div class="evidence-batches">
          <h4>逐笔真实闭合证据</h4>
          <table class="data-table evidence-table">
            <thead>
              <tr>
                <th>批次记录</th>
                <th>投递批次</th>
                <th>闭合凭据</th>
                <th>完成时间</th>
                <th>总/成功/隔离行</th>
                <th>隔离凭据</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="batch in evidence.closed_batches" :key="batch.batch_record_no">
                <td>{{ batch.batch_record_no }}</td>
                <td>{{ batch.intake_batch }}</td>
                <td><code>{{ batch.close_credential }}</code></td>
                <td>{{ batch.close_time || '—' }}</td>
                <td>{{ batch.total_rows }} / {{ batch.success_rows }} / {{ batch.quarantine_rows }}</td>
                <td>
                  <span v-if="!batch.quarantine_credentials.length">—</span>
                  <code v-for="q in batch.quarantine_credentials" :key="q" class="quarantine-credential">{{ q }}</code>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="evidence.duplicate_credentials.length" class="evidence-duplicates">
          <h4>重复闭合凭据（只采一次）</h4>
          <ul>
            <li v-for="dup in evidence.duplicate_credentials" :key="dup['闭合凭据'] + dup['批次记录编号']">
              <code>{{ dup['闭合凭据'] }}</code>（{{ dup['关联投递批次'] }} / {{ dup['批次记录编号'] }}）：{{ dup['重复原因'] }}
            </li>
          </ul>
        </div>

        <div v-if="evidence.conflicts.length" class="evidence-conflicts">
          <h4>冲突明细（界面按服务端结果继续核对）</h4>
          <ul>
            <li v-for="conflict in evidence.conflicts" :key="conflict">{{ conflict }}</li>
          </ul>
        </div>

        <footer class="evidence-actions">
          <button class="btn" type="button" data-testid="cross-check" @click="runCrossCheck">内部一致性核对</button>
          <button class="btn" type="button" data-testid="reconcile-live" @click="runReconcile">与实时事实对照</button>
        </footer>

        <p v-if="actionMessage" class="evidence-result" :class="{ ok: actionOk }">{{ actionMessage }}</p>

        <div v-if="comparison" class="evidence-reconcile">
          <h4>历史读数对照（历史分母与原窗口证据保留）</h4>
          <table class="data-table evidence-table">
            <tbody>
              <tr>
                <th>历史闭合批次</th>
                <td>{{ comparison.snapshot_closed_count }} 批（{{ comparison.snapshot_denominator }} 行）</td>
                <th>实时闭合批次</th>
                <td>{{ comparison.live_closed_count }} 批（{{ comparison.live_denominator }} 行）</td>
              </tr>
              <tr>
                <th>新增闭合凭据</th>
                <td>{{ comparison.new_credentials.join('、') || '无' }}</td>
                <th>历史是否封存</th>
                <td>{{ comparison.historical_preserved ? '是，未被后来闭合覆盖' : '否' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

import {
  crossCheckReading,
  loadReadingEvidence,
  reconcileReading,
  type ReadingEvidence,
  type ReconcileComparison,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ visible: boolean; row: EntryRow | null }>()
const emit = defineEmits<{ close: [] }>()

const loading = ref(false)
const errorMessage = ref('')
const evidence = ref<ReadingEvidence | null>(null)
const comparison = ref<ReconcileComparison | null>(null)
const actionMessage = ref('')
const actionOk = ref(false)

function formatRate(value: number): string {
  return `${(value * 100).toFixed(2)}%`
}

function close() {
  emit('close')
}

async function openEvidence() {
  if (!props.row) return
  const readingNo = String(props.row['指标读数编号'] ?? '')
  loading.value = true
  errorMessage.value = ''
  evidence.value = null
  comparison.value = null
  actionMessage.value = ''
  try {
    evidence.value = await loadReadingEvidence(readingNo)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '闭合证据读取失败'
  } finally {
    loading.value = false
  }
}

async function runCrossCheck() {
  if (!props.row) return
  actionMessage.value = ''
  try {
    const result = await crossCheckReading(String(props.row['指标读数编号'] ?? ''))
    actionOk.value = result.ok
    actionMessage.value = result.message
  } catch (error) {
    actionOk.value = false
    actionMessage.value = error instanceof Error ? error.message : '核对失败'
  }
}

async function runReconcile() {
  if (!props.row) return
  actionMessage.value = ''
  comparison.value = null
  try {
    const result = await reconcileReading(String(props.row['指标读数编号'] ?? ''))
    actionOk.value = result.ok
    actionMessage.value = result.message
    comparison.value = result.comparison
  } catch (error) {
    actionOk.value = false
    actionMessage.value = error instanceof Error ? error.message : '对照失败'
  }
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) void openEvidence()
  },
)
</script>

<style scoped>
.evidence-modal {
  width: min(960px, 92vw);
  max-height: 86vh;
  overflow-y: auto;
}
.evidence-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.evidence-head h3 {
  margin: 0;
}
.evidence-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--border-color, #d8dde6);
  border-radius: 8px;
  background: #f6f8fb;
  margin-bottom: 14px;
}
.evidence-banner.review {
  border-color: #d97706;
  background: #fff7ed;
}
.review-tag {
  padding: 2px 8px;
  border-radius: 999px;
  background: #d97706;
  color: #fff;
  font-size: 12px;
}
.evidence-scope h4,
.evidence-batches h4,
.evidence-duplicates h4,
.evidence-conflicts h4,
.evidence-reconcile h4 {
  margin: 12px 0 6px;
}
.credential-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.credential-item code,
.credential-list code {
  padding: 2px 6px;
  border-radius: 4px;
  background: #eef2ff;
}
.evidence-meta {
  color: #6b7280;
  font-size: 12px;
}
.evidence-table {
  font-size: 13px;
}
.quarantine-credential {
  margin-right: 4px;
  padding: 1px 5px;
  border-radius: 4px;
  background: #fef2f2;
}
.evidence-duplicates,
.evidence-conflicts {
  margin-top: 12px;
  padding: 8px 12px;
  border-radius: 8px;
}
.evidence-duplicates {
  background: #f0fdf4;
}
.evidence-conflicts {
  background: #fff7ed;
}
.evidence-actions {
  display: flex;
  gap: 10px;
  margin-top: 16px;
}
.evidence-result {
  margin-top: 10px;
  font-weight: 600;
}
.evidence-result.ok {
  color: #047857;
}
.evidence-reconcile {
  margin-top: 14px;
}
</style>
