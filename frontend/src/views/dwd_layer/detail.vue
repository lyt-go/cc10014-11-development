<template>
  <section class="page" data-module="dwd_layer_detail">
    <header class="page-head">
      <div>
        <h2>DWD增量明细表详情</h2>
        <p class="page-desc">
          {{ entry?.表名称 || '—' }}（{{ entry?.表编号 || '—' }}）· 按刷新水位增量构建，只含未处理批次，水位可回查。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn ghost" to="/dwd_layer" data-testid="back-to-list">返回明细层管理</RouterLink>
        <button class="btn" type="button" data-testid="refresh" @click="reload">刷新</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">刷新水位</span>
        <strong class="stat-value" data-testid="watermark">{{ watermark || '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">批次水位</span>
        <strong class="stat-value" data-testid="batch-watermark">{{ batchWatermark || '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">有效明细行</span>
        <strong class="stat-value" data-testid="valid-rows">{{ totalRows }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已处理批次</span>
        <strong class="stat-value" data-testid="processed-batches">{{ processedBatches.length }}</strong>
      </article>
    </div>

    <div class="action-bar">
      <button class="btn primary" type="button" data-testid="build-incremental" :disabled="building" @click="buildIncremental(false)">
        {{ building ? '构建中…' : '增量构建' }}
      </button>
      <button class="btn" type="button" data-testid="replay-request" :disabled="!lastRequestId || building" @click="buildIncremental(true)">
        重放上次请求
      </button>
      <button class="btn" type="button" data-testid="simulate-batch" @click="simulateBatch(false)">模拟新批次</button>
      <button class="btn" type="button" data-testid="simulate-late-batch" @click="simulateBatch(true)">模拟迟到批次</button>
      <span v-if="lastRequestId" class="hint-text">最近请求：{{ lastRequestId }}</span>
    </div>

    <p v-if="buildMessage" class="build-message" data-testid="build-message">{{ buildMessage }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="detail-error">{{ errorMessage }}（可点击「增量构建」重试）</p>

    <h3 class="section-title">构建日志（水位可回查）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>构建编号</th><th>状态</th><th>处理批次</th><th>迟到窗口</th><th>写入行数</th>
          <th>水位变化</th><th>原因说明</th><th>时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="log in logs" :key="log.build_id" :data-testid="`log-${log.build_id}`">
          <td>{{ log.build_id }}</td>
          <td>{{ log.status }}</td>
          <td>{{ (log.batches || []).join('、') || '—' }}</td>
          <td>{{ (log.late_windows || []).join('、') || '—' }}</td>
          <td>{{ log.rows_written }}</td>
          <td>{{ log.watermark_before || '—' }} → {{ log.watermark_after || '—' }}</td>
          <td>{{ log.reason || '—' }}</td>
          <td>{{ log.created_at }}</td>
        </tr>
        <tr v-if="!logs.length">
          <td colspan="8" class="empty-state">暂无构建记录，可先点击「增量构建」或「模拟新批次」</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">窗口版本（迟到分区保留不同片段）</h3>
    <table class="data-table">
      <thead>
        <tr><th>业务窗口</th><th>版本序列</th><th>当前版本</th><th>有效行</th><th>已失效行</th></tr>
      </thead>
      <tbody>
        <tr v-for="w in windows" :key="w.window" :data-testid="`window-${w.window}`">
          <td>{{ w.window }}</td>
          <td>{{ w.versions.join(' → ') }}</td>
          <td>v{{ w.current_version }}</td>
          <td>{{ w.valid_rows }}</td>
          <td>{{ w.expired_rows }}</td>
        </tr>
        <tr v-if="!windows.length">
          <td colspan="5" class="empty-state">暂无窗口数据</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">增量明细数据（用户只看到一份完整DWD增量明细表）</h3>
    <table class="data-table">
      <thead>
        <tr><th v-for="col in rowColumns" :key="col">{{ col }}</th></tr>
      </thead>
      <tbody>
        <tr v-for="(row, idx) in rows" :key="idx" data-testid="detail-row">
          <td v-for="col in rowColumns" :key="col">{{ row[col] ?? '—' }}</td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="rowColumns.length" class="empty-state">暂无明细记录，等待增量构建写入</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span data-testid="conflict-policy">{{ conflictPolicy }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type BuildLog = Record<string, any>
type WindowInfo = Record<string, any>

const route = useRoute()
const entryId = String(route.params.id)
const ENDPOINT = `/api/dwd_layer/${entryId}`

const entry = ref<Row | null>(null)
const watermark = ref('')
const batchWatermark = ref('')
const processedBatches = ref<string[]>([])
const rows = ref<Row[]>([])
const totalRows = ref(0)
const logs = ref<BuildLog[]>([])
const windows = ref<WindowInfo[]>([])
const conflictPolicy = ref('')
const buildMessage = ref('')
const errorMessage = ref('')
const building = ref(false)
const lastRequestId = ref('')

const rowColumns = computed(() => {
  const cols: string[] = []
  for (const row of rows.value) {
    for (const key of Object.keys(row)) {
      if (!cols.includes(key)) cols.push(key)
    }
  }
  return cols.length ? cols : ['暂无字段']
})

function newRequestId(): string {
  return `dwd-${entryId}-${crypto.randomUUID()}`
}

async function reload() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/incremental`)
    if (!response.ok) throw new Error('增量明细读取失败')
    const payload = await response.json()
    entry.value = payload.entry ?? null
    watermark.value = payload['刷新水位'] ?? ''
    batchWatermark.value = payload['批次水位'] ?? ''
    processedBatches.value = payload['已处理批次'] ?? []
    rows.value = payload.rows ?? []
    totalRows.value = payload.total_rows ?? rows.value.length
    logs.value = payload.logs ?? []
    windows.value = payload.windows ?? []
    conflictPolicy.value = payload.conflict_policy ?? ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '增量明细读取失败'
  }
}

async function buildIncremental(replay: boolean) {
  building.value = true
  buildMessage.value = ''
  errorMessage.value = ''
  const requestId = replay ? lastRequestId.value : newRequestId()
  try {
    const response = await request(`${ENDPOINT}/build_incremental`, {
      method: 'POST',
      body: JSON.stringify({ request_id: requestId }),
    })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.reason || payload.message || '增量构建失败')
    }
    lastRequestId.value = requestId
    buildMessage.value = payload.reason || payload.message || '增量构建完成'
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '增量构建失败'
  } finally {
    building.value = false
  }
}

async function simulateBatch(late: boolean) {
  buildMessage.value = ''
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/simulate_ods_batch`, {
      method: 'POST',
      body: JSON.stringify({ late }),
    })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.detail || payload.message || '模拟批次失败')
    }
    buildMessage.value = payload.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '模拟批次失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.action-bar { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
.hint-text { color: var(--muted); font-size: 12px; }
.build-message { color: #067647; font-size: 13px; margin: 4px 0 12px; }
.section-title { font-size: 14px; margin: 18px 0 8px; }
</style>
