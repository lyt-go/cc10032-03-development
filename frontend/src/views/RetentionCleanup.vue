<template>
  <section class="cleanup-panel" data-testid="cleanup-panel">
    <header class="cleanup-head">
      <div>
        <h3>清理预览</h3>
        <p class="page-desc">
          按表列出各对象的清理依据：归档凭证按对象和版本核对，恢复依据取自仍可读取的快照；
          暂缓项按保护期、例外项、缺凭证、最少快照数四组展示。
        </p>
      </div>
      <div class="page-actions">
        <label class="cleanup-policy">
          <span>保留策略</span>
          <select v-model="selectedPolicyId" data-testid="cleanup-policy">
            <option v-for="policy in activePolicies" :key="policy.id" :value="Number(policy.id)">
              {{ policy['策略编号'] }} · {{ policy['目标表'] }}（保留{{ policy['保留天数'] }}天/最少快照{{ policy['最小快照数'] }}份）
            </option>
          </select>
        </label>
        <button class="btn primary" data-testid="cleanup-generate" type="button" :disabled="busy" @click="generate">
          生成清理预览
        </button>
      </div>
    </header>

    <div v-if="preview" class="cleanup-toolbar">
      <button class="btn" data-testid="toggle-credential-form" type="button" @click="showCredForm = !showCredForm">
        补归档凭证
      </button>
      <button class="btn" data-testid="toggle-revoke-form" type="button" @click="showRevokeForm = !showRevokeForm">
        撤销快照
      </button>
      <button
        class="btn primary"
        data-testid="cleanup-execute"
        type="button"
        :disabled="busy || preview.已执行"
        @click="submitExecute"
      >
        {{ preview.已执行 ? `已执行 ${preview.执行编号}` : '提交执行' }}
      </button>
    </div>

    <form v-if="preview && showCredForm" class="cleanup-form" data-testid="credential-form" @submit.prevent="submitCredential">
      <span class="cleanup-form-title">补归档凭证（回写清理清单）</span>
      <label>
        <span>表名</span>
        <input v-model="credForm.表名" data-testid="cred-table" readonly />
      </label>
      <label>
        <span>分区</span>
        <input v-model="credForm.分区" data-testid="cred-partition" placeholder="如 dt=2026-04-01" required />
      </label>
      <label>
        <span>归档版本</span>
        <input v-model="credForm.归档版本" data-testid="cred-version" placeholder="如 v5" required />
      </label>
      <button class="btn primary" data-testid="cred-submit" type="submit" :disabled="busy">确认补录</button>
    </form>

    <form v-if="preview && showRevokeForm" class="cleanup-form" data-testid="revoke-form" @submit.prevent="submitRevoke">
      <span class="cleanup-form-title">撤销快照（撤销后不再作为恢复依据）</span>
      <label>
        <span>快照编号</span>
        <select v-model="revokeSnapId" data-testid="revoke-snap">
          <option v-for="sid in revocableSnapshots" :key="sid" :value="sid">{{ sid }}</option>
        </select>
      </label>
      <button class="btn primary" data-testid="revoke-submit" type="submit" :disabled="busy || !revokeSnapId">
        确认撤销
      </button>
    </form>

    <p v-if="actionMessage" class="cleanup-message" data-testid="cleanup-message">{{ actionMessage }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="cleanup-error">{{ errorMessage }}</p>

    <div v-if="preview" class="cleanup-preview" :data-preview-id="preview.preview_id">
      <header class="cleanup-preview-head">
        <strong>预览单 {{ preview.preview_id }}</strong>
        <span>生成时间 {{ preview.generated_at }}</span>
        <span>目标表 {{ preview.目标表 }}</span>
        <span>
          保留 {{ preview.policy_params['保留天数'] }} 天 · 最少快照 {{ preview.policy_params['最小快照数'] }} 份 ·
          生成时有效快照 {{ preview.conditions.valid_snapshot_count }} 份
        </span>
        <span v-if="historicalMode" class="cleanup-tag" data-testid="historical-tag">历史预览（按原条件复核）</span>
        <span v-if="preview.已执行" class="cleanup-tag executed">已执行 {{ preview.执行编号 }}</span>
      </header>

      <div class="stat-row">
        <article v-for="group in groupOrder" :key="group" class="stat-card" :data-testid="`cleanup-count-${group}`">
          <span class="stat-label">{{ preview.group_labels[group] }}</span>
          <strong class="stat-value">{{ preview.counts[group] }}</strong>
        </article>
      </div>

      <div v-for="group in groupOrder" :key="group" class="cleanup-group" :data-testid="`cleanup-group-${group}`">
        <h4 class="cleanup-group-title">{{ preview.group_labels[group] }}（{{ preview.groups[group].length }}）</h4>
        <table v-if="preview.groups[group].length" class="data-table">
          <thead>
            <tr>
              <th>分区</th>
              <th>数据版本</th>
              <th>数据时间</th>
              <th>行数</th>
              <th>{{ group === 'eligible' ? '清理依据' : '暂缓原因' }}</th>
              <th>{{ group === 'eligible' ? '恢复依据' : '解除条件' }}</th>
              <th>详情</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="item in preview.groups[group]" :key="item.分区">
              <tr :data-testid="`cleanup-item-${item.分区}`">
                <td>{{ item.分区 }}</td>
                <td>{{ item.数据版本 }}</td>
                <td>{{ item.数据时间 }}</td>
                <td>{{ item.行数 }}</td>
                <td>{{ group === 'eligible' ? item.清理依据 : item.暂缓原因 }}</td>
                <td>{{ group === 'eligible' ? item.恢复依据 : item.解除条件 }}</td>
                <td>
                  <button
                    class="link"
                    type="button"
                    :data-testid="`cleanup-detail-${item.分区}`"
                    @click="toggleDetail(item.分区)"
                  >
                    {{ expandedPartition === item.分区 ? '收起' : '展开' }}
                  </button>
                </td>
              </tr>
              <tr v-if="expandedPartition === item.分区 && objectDetail" class="cleanup-detail-row" data-testid="cleanup-detail-row">
                <td colspan="7">
                  <div class="cleanup-detail">
                    <div>
                      <h5>最近可用快照</h5>
                      <p v-if="objectDetail.latest_snapshot">
                        {{ objectDetail.latest_snapshot.快照编号 }}（{{ objectDetail.latest_snapshot.版本 }}）·
                        {{ objectDetail.latest_snapshot.存储路径 }} ·
                        覆盖 {{ objectDetail.latest_snapshot.覆盖分区.join('、') }}
                      </p>
                      <p v-else>无可读快照覆盖该分区</p>
                    </div>
                    <div>
                      <h5>当前凭证覆盖的分区</h5>
                      <ul>
                        <li v-for="cred in objectDetail.credential_coverage" :key="cred.凭证编号">
                          {{ cred.凭证编号 }}（{{ cred.归档版本 }} · {{ cred.状态 }}）：
                          {{ cred.覆盖分区.join('、') || '（无分区）' }}
                        </li>
                      </ul>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <p v-else class="cleanup-empty">本组暂无对象</p>
      </div>
    </div>

    <div v-if="execution" class="cleanup-execution" data-testid="cleanup-execution">
      <h4>执行清单 {{ execution.exec_id }}</h4>
      <p class="cleanup-exec-summary">
        {{ execution.summary }} · 有效快照 {{ execution.valid_snapshot_count }} 份 / 最少 {{ execution.最小快照数 }} 份 ·
        清理方式 {{ execution.清理方式 }}
      </p>

      <h5>实际清理范围（{{ execution.cleaned.length }}）</h5>
      <table v-if="execution.cleaned.length" class="data-table">
        <thead>
          <tr><th>分区</th><th>数据版本</th><th>清理依据</th><th>恢复依据</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in execution.cleaned" :key="item.分区" data-testid="exec-cleaned-item">
            <td>{{ item.分区 }}</td>
            <td>{{ item.数据版本 }}</td>
            <td>{{ item.清理依据 }}</td>
            <td>{{ item.恢复依据 }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="cleanup-empty">本次无实际清理对象</p>

      <h5>被暂缓的项（{{ execution.deferred.length }}）</h5>
      <table v-if="execution.deferred.length" class="data-table">
        <thead>
          <tr><th>分区</th><th>数据版本</th><th>暂缓原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in execution.deferred" :key="item.分区" data-testid="exec-deferred-item">
            <td>{{ item.分区 }}</td>
            <td>{{ item.数据版本 }}</td>
            <td>{{ item.暂缓原因 }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="cleanup-empty">无暂缓项</p>

      <h5>下一次可确认的分区（{{ execution.next_confirmable.length }}）</h5>
      <table v-if="execution.next_confirmable.length" class="data-table">
        <thead>
          <tr><th>分区</th><th>数据版本</th><th>确认条件</th><th>恢复依据</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in execution.next_confirmable" :key="item.分区" data-testid="exec-next-item">
            <td>{{ item.分区 }}</td>
            <td>{{ item.数据版本 }}</td>
            <td>{{ item.条件 }}</td>
            <td>{{ item.恢复依据 ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <h5>冲突注明（{{ execution.conflicts.length }}）</h5>
      <ul v-if="execution.conflicts.length" class="cleanup-conflicts">
        <li v-for="(conflict, index) in execution.conflicts" :key="index" data-testid="exec-conflict-item">
          <strong>[{{ conflict.类型 }}]</strong> {{ conflict.说明 }}<br />
          <span class="cleanup-basis">资格依据：{{ conflict.资格依据 }}</span>
        </li>
      </ul>
      <p v-else class="cleanup-empty">预览后无补归档 / 撤销快照变更</p>
    </div>

    <div class="cleanup-history" data-testid="cleanup-history">
      <h4>历史预览</h4>
      <table v-if="history.length" class="data-table">
        <thead>
          <tr>
            <th>预览单</th><th>生成时间</th><th>策略</th><th>目标表</th>
            <th>候选</th><th>保护期</th><th>例外项</th><th>缺凭证</th><th>最少快照数</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in history" :key="item.preview_id" :data-testid="`history-${item.preview_id}`">
            <td>{{ item.preview_id }}</td>
            <td>{{ item.generated_at }}</td>
            <td>{{ item.策略编号 }}</td>
            <td>{{ item.目标表 }}</td>
            <td>{{ item.counts.eligible }}</td>
            <td>{{ item.counts.protection }}</td>
            <td>{{ item.counts.exception }}</td>
            <td>{{ item.counts.missing_credential }}</td>
            <td>{{ item.counts.min_snapshot }}</td>
            <td>{{ item.已执行 ? `已执行 ${item.执行编号}` : '待执行' }}</td>
            <td>
              <button class="link" type="button" :data-testid="`review-${item.preview_id}`" @click="review(item.preview_id)">
                回查
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="cleanup-empty">暂无历史预览，点击“生成清理预览”开始</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  executeCleanup,
  generateCleanupPreview,
  listCleanupPreviews,
  listEntries,
  loadCleanupObjectDetail,
  loadCleanupPreview,
  revokeSnapshot,
  supplementCredential,
} from '@/api/backend-service'
import type {
  CleanupExecution,
  CleanupGroupKey,
  CleanupObjectDetail,
  CleanupPreview,
  CleanupPreviewSummary,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const groupOrder: CleanupGroupKey[] = ['eligible', 'protection', 'exception', 'missing_credential', 'min_snapshot']

const activePolicies = ref<EntryRow[]>([])
const selectedPolicyId = ref<number>(0)
const preview = ref<CleanupPreview | null>(null)
const historicalMode = ref(false)
const history = ref<CleanupPreviewSummary[]>([])
const execution = ref<CleanupExecution | null>(null)
const expandedPartition = ref('')
const objectDetail = ref<CleanupObjectDetail | null>(null)
const showCredForm = ref(false)
const showRevokeForm = ref(false)
const credForm = ref({ 表名: '', 分区: '', 归档版本: '' })
const revokeSnapId = ref('')
const actionMessage = ref('')
const errorMessage = ref('')
const busy = ref(false)

const revocableSnapshots = computed(() => {
  if (!preview.value) return []
  return Object.keys(preview.value.conditions.snapshots).sort()
})

async function generate() {
  if (!selectedPolicyId.value) return
  busy.value = true
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const result = await generateCleanupPreview(selectedPolicyId.value)
    preview.value = result.preview
    historicalMode.value = false
    execution.value = null
    expandedPartition.value = ''
    objectDetail.value = null
    credForm.value = { 表名: result.preview.目标表, 分区: '', 归档版本: '' }
    revokeSnapId.value = revocableSnapshots.value[0] ?? ''
    actionMessage.value = `预览单 ${result.preview.preview_id} 已生成，条件已冻结，可复核`
    await refreshHistory()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预览生成失败'
  } finally {
    busy.value = false
  }
}

async function toggleDetail(partition: string) {
  if (!preview.value) return
  if (expandedPartition.value === partition) {
    expandedPartition.value = ''
    objectDetail.value = null
    return
  }
  errorMessage.value = ''
  try {
    objectDetail.value = await loadCleanupObjectDetail(preview.value.preview_id, partition)
    expandedPartition.value = partition
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对象详情读取失败'
  }
}

async function submitCredential() {
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await supplementCredential(credForm.value)
    actionMessage.value = `${result.message}；提交执行时将按执行时刻完整凭证核对`
    credForm.value = { ...credForm.value, 分区: '', 归档版本: '' }
    showCredForm.value = false
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补录失败'
  } finally {
    busy.value = false
  }
}

async function submitRevoke() {
  if (!revokeSnapId.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await revokeSnapshot(revokeSnapId.value)
    actionMessage.value = `${result.message}；提交执行时将按执行时刻可读快照重新计算恢复依据`
    showRevokeForm.value = false
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '撤销失败'
  } finally {
    busy.value = false
  }
}

async function submitExecute() {
  if (!preview.value) return
  busy.value = true
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const result = await executeCleanup(preview.value.preview_id)
    execution.value = result.execution
    // 执行后回读预览单，刷新“已执行”标记（预览内容仍按原条件）
    const refreshed = await loadCleanupPreview(preview.value.preview_id)
    preview.value = refreshed.preview
    await refreshHistory()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '执行失败'
  } finally {
    busy.value = false
  }
}

async function review(previewId: string) {
  errorMessage.value = ''
  execution.value = null
  expandedPartition.value = ''
  objectDetail.value = null
  try {
    const result = await loadCleanupPreview(previewId)
    preview.value = result.preview
    historicalMode.value = true
    credForm.value = { 表名: result.preview.目标表, 分区: '', 归档版本: '' }
    revokeSnapId.value = ''
    actionMessage.value = `正在回查历史预览 ${previewId}，展示生成时冻结的条件与分组`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史预览回查失败'
  }
}

async function refreshHistory() {
  try {
    const result = await listCleanupPreviews()
    history.value = result.items
  } catch {
    history.value = []
  }
}

onMounted(async () => {
  try {
    const payload = await listEntries('retention')
    activePolicies.value = payload.items.filter((row) => row.status === '已生效')
    selectedPolicyId.value = Number(activePolicies.value[0]?.id ?? 0)
  } catch {
    activePolicies.value = []
  }
  await refreshHistory()
})
</script>
