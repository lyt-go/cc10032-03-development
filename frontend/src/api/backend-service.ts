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

// ---------------------------------------------------------------------------
// 保留管理 · 清理预览
// ---------------------------------------------------------------------------

export type CleanupItem = {
  表名: string
  分区: string
  数据版本: string
  数据时间: string
  行数: number
  清理依据?: string
  恢复依据?: string
  暂缓原因?: string
  解除条件?: string
}

export type CleanupPreview = {
  preview_id: string
  generated_at: string
  policy_id: number
  策略编号: string
  目标表: string
  policy_params: Record<string, string | number>
  groups: Record<CleanupGroupKey, CleanupItem[]>
  group_labels: Record<CleanupGroupKey, string>
  counts: Record<CleanupGroupKey, number>
  conditions: {
    credentials: Record<string, { 表名: string; 归档版本: string; 覆盖分区: string[]; 状态: string }>
    snapshots: Record<string, { 版本: string; 覆盖分区: string[]; 可读: boolean }>
    valid_snapshot_count: number
  }
  已执行: boolean
  执行编号: string | null
}

export type CleanupGroupKey =
  | 'eligible'
  | 'protection'
  | 'exception'
  | 'missing_credential'
  | 'min_snapshot'

export type CleanupPreviewSummary = {
  preview_id: string
  generated_at: string
  policy_id: number
  策略编号: string
  目标表: string
  counts: Record<CleanupGroupKey, number>
  已执行: boolean
  执行编号: string | null
}

export type CleanupObjectDetail = {
  ok: boolean
  preview_id: string
  group: CleanupGroupKey
  group_label: string
  object: CleanupItem
  latest_snapshot: {
    快照编号: string
    版本: string
    存储路径: string
    覆盖分区: string[]
  } | null
  credential_coverage: {
    凭证编号: string
    归档版本: string
    状态: string
    覆盖分区: string[]
    来源策略: string
  }[]
}

export type CleanupExecution = {
  exec_id: string
  preview_id: string
  executed_at: string
  目标表: string
  清理方式: string
  cleaned: CleanupItem[]
  deferred: CleanupItem[]
  next_confirmable: { 表名: string; 分区: string; 数据版本: string; 条件: string; 清理依据?: string; 恢复依据?: string }[]
  conflicts: { 类型: string; 说明: string; 资格依据: string }[]
  valid_snapshot_count: number
  最小快照数: number
  summary: string
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(typeof payload?.detail === 'string' ? payload.detail : `接口返回 ${response.status}`)
  }
  return payload as T
}

export async function generateCleanupPreview(policyId: number): Promise<{ ok: boolean; preview: CleanupPreview }> {
  return postJson('/api/retention/cleanup/preview', { policy_id: policyId })
}

export async function listCleanupPreviews(): Promise<{ ok: boolean; items: CleanupPreviewSummary[]; total: number }> {
  const response = await request('/api/retention/cleanup/previews')
  if (!response.ok) throw new Error('历史预览读取失败')
  return await response.json()
}

export async function loadCleanupPreview(previewId: string): Promise<{ ok: boolean; preview: CleanupPreview }> {
  const response = await request(`/api/retention/cleanup/previews/${previewId}`)
  if (!response.ok) throw new Error('预览单不存在或已过期')
  return await response.json()
}

export async function loadCleanupObjectDetail(previewId: string, partition: string): Promise<CleanupObjectDetail> {
  const response = await request(
    `/api/retention/cleanup/previews/${previewId}/objects/${encodeURIComponent(partition)}`,
  )
  if (!response.ok) throw new Error('对象详情读取失败')
  return await response.json()
}

export async function supplementCredential(values: {
  表名: string
  分区: string
  归档版本: string
}): Promise<{ ok: boolean; message: string }> {
  return postJson('/api/retention/cleanup/credentials', { ...values, 来源策略: '人工补录' })
}

export async function revokeSnapshot(snapId: string): Promise<{ ok: boolean; message: string }> {
  return postJson('/api/retention/cleanup/snapshots/revoke', { 快照编号: snapId })
}

export async function executeCleanup(previewId: string): Promise<{ ok: boolean; execution: CleanupExecution }> {
  return postJson('/api/retention/cleanup/execute', { preview_id: previewId })
}
