import { useEffect, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type {
  AuditEventResponse,
  FeatureKey,
  FeatureProviderMode,
  FeatureProviderSettings,
  FeatureProviderSettingsUpdate,
  ProviderHealth,
} from '../api/types'
import { ErrorPanel } from '../components/ErrorPanel'
import { StatusBadge } from '../components/StatusBadge'

type FeatureDefinition = {
  feature: FeatureKey
  label: string
  description: string
}

type FeatureForms = Partial<Record<FeatureKey, FeatureProviderMode>>

type ConnectionAttempt = {
  mode: FeatureProviderMode
  fingerprint: string
}

const featureDefinitions: FeatureDefinition[] = [
  { feature: 'call_assistant', label: '坐席辅助', description: '生成推荐话术与通话摘要。' },
  { feature: 'knowledge', label: '运维知识库', description: '基于已审核资料生成带引用的回答。' },
  { feature: 'document_review', label: '文档审核', description: '按审核规则生成风险发现与修改建议。' },
]

function modeLabel(mode: FeatureProviderMode) {
  return mode === 'local' ? 'Local' : 'Mock'
}

function candidateFingerprint(feature: FeatureKey, mode: FeatureProviderMode) {
  return JSON.stringify({ feature, mode })
}

function formatTimestamp(value: string) {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium', hour12: false }).format(new Date(value))
}

function FeatureModeCard({
  definition,
  persisted,
  candidate,
  onCandidateChange,
  onSaved,
}: {
  definition: FeatureDefinition
  persisted: FeatureProviderSettings
  candidate: FeatureProviderMode
  onCandidateChange: (mode: FeatureProviderMode) => void
  onSaved: (setting: FeatureProviderSettings) => void
}) {
  const queryClient = useQueryClient()
  const [verifiedLocalFingerprint, setVerifiedLocalFingerprint] = useState<string | null>(null)
  const [connectionStatus, setConnectionStatus] = useState<ProviderHealth | null>(null)
  const [saveMessage, setSaveMessage] = useState<string | null>(null)
  const fingerprint = candidateFingerprint(definition.feature, candidate)

  const connectionTest = useMutation({
    mutationFn: (attempt: ConnectionAttempt) => api.post<ProviderHealth>(
      `/api/settings/features/${definition.feature}/test`,
      { mode: attempt.mode } satisfies FeatureProviderSettingsUpdate,
    ),
    onSuccess: (result, attempt) => {
      setConnectionStatus(result)
      setVerifiedLocalFingerprint(result.available ? attempt.fingerprint : null)
    },
    onError: (error: Error) => {
      setConnectionStatus({ available: false, detail: error.message })
      setVerifiedLocalFingerprint(null)
    },
  })

  const saveSettings = useMutation({
    mutationFn: (mode: FeatureProviderMode) => api.put<FeatureProviderSettings>(
      `/api/settings/features/${definition.feature}`,
      { mode } satisfies FeatureProviderSettingsUpdate,
    ),
    onSuccess: (result) => {
      onSaved(result)
      setSaveMessage(`${definition.label}已切换为 ${modeLabel(result.mode)}`)
      queryClient.setQueryData<FeatureProviderSettings[]>(['feature-settings'], (current) => (
        current?.map((item) => item.feature === result.feature ? result : item) ?? [result]
      ))
      void queryClient.invalidateQueries({ queryKey: ['audit-events'] })
    },
  })

  const updateCandidate = (mode: FeatureProviderMode) => {
    onCandidateChange(mode)
    setVerifiedLocalFingerprint(null)
    setConnectionStatus(null)
    setSaveMessage(null)
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (candidate === 'local' && verifiedLocalFingerprint !== fingerprint) return
    saveSettings.mutate(candidate)
  }

  const testLocalConnection = () => {
    setSaveMessage(null)
    connectionTest.mutate({ mode: candidate, fingerprint })
  }

  const isLocalCandidate = candidate === 'local'
  const differsFromPersisted = candidate !== persisted.mode
  const isBusy = connectionTest.isPending || saveSettings.isPending
  const saveDisabled = isBusy || (isLocalCandidate && verifiedLocalFingerprint !== fingerprint)

  return (
    <form className="settings-card feature-settings-card" onSubmit={submit}>
      <div className="feature-settings-heading">
        <div>
          <h2>{definition.label}</h2>
          <p>{definition.description}</p>
        </div>
        <StatusBadge tone={persisted.mode === 'local' ? 'warning' : 'neutral'}>
          {`${definition.label}：当前 ${modeLabel(persisted.mode)}`}
        </StatusBadge>
      </div>

      <label htmlFor={`${definition.feature}-mode`}>{definition.label}模式</label>
      <select
        id={`${definition.feature}-mode`}
        value={candidate}
        onChange={(event) => updateCandidate(event.target.value as FeatureProviderMode)}
        disabled={isBusy}
      >
        <option value="mock">Mock（内置稳定演示）</option>
        <option value="local">Local（服务器本地模型）</option>
      </select>

      {differsFromPersisted && <p className="form-status">待保存：{modeLabel(candidate)}</p>}
      {isLocalCandidate && (
        <p className="settings-note">
          启用 Local 前必须测试本业务的本地模型连接；测试结果仅对当前候选设置有效。
        </p>
      )}

      <div className="form-actions">
        {isLocalCandidate && (
          <button
            type="button"
            className="secondary-button"
            onClick={testLocalConnection}
            disabled={isBusy}
          >
            测试{definition.label}本地模型
          </button>
        )}
        <button type="submit" className="primary-button" disabled={saveDisabled}>
          保存{definition.label}设置
        </button>
      </div>

      {connectionStatus && (
        <p
          className={connectionStatus.available ? 'form-status success-text' : 'form-status danger-text'}
          role="status"
        >
          {connectionStatus.detail}
        </p>
      )}
      {saveMessage && <p className="form-status success-text" role="status">{saveMessage}</p>}
      {saveSettings.isError && <p className="form-status danger-text" role="alert">{saveSettings.error.message}</p>}
    </form>
  )
}

export function SettingsPage() {
  const settingsQuery = useQuery({
    queryKey: ['feature-settings'],
    queryFn: () => api.get<FeatureProviderSettings[]>('/api/settings/features'),
  })
  const auditQuery = useQuery({
    queryKey: ['audit-events'],
    queryFn: () => api.get<AuditEventResponse[]>('/api/audit-events?limit=50'),
  })
  const [featureForms, setFeatureForms] = useState<FeatureForms>({})

  useEffect(() => {
    if (!settingsQuery.data) return
    setFeatureForms((current) => {
      const next = { ...current }
      for (const setting of settingsQuery.data) {
        next[setting.feature] ??= setting.mode
      }
      return next
    })
  }, [settingsQuery.data])

  if (settingsQuery.isPending) return <section className="page-state" aria-live="polite">正在加载运行设置…</section>
  if (settingsQuery.isError) return <ErrorPanel detail={settingsQuery.error.message} onRetry={() => void settingsQuery.refetch()} />

  const settingsByFeature = Object.fromEntries(
    settingsQuery.data.map((setting) => [setting.feature, setting]),
  ) as Record<FeatureKey, FeatureProviderSettings>

  return (
    <section className="settings-page" aria-labelledby="page-title">
      <header className="page-header">
        <div>
          <p className="eyebrow">运行配置</p>
          <h1 id="page-title">运行与设置</h1>
          <p>分别配置三个业务工作流；候选设置保存后才会成为当前激活模式。</p>
        </div>
      </header>

      <section className="feature-settings-section" aria-labelledby="feature-settings-title">
        <div className="section-heading">
          <h2 id="feature-settings-title">业务模型模式</h2>
          <p>Mock 可直接保存；Local 必须先通过当前业务的连接测试。</p>
        </div>
        <div className="feature-settings-grid">
          {featureDefinitions.map((definition) => {
            const persisted = settingsByFeature[definition.feature]
            const candidate = featureForms[definition.feature]
            if (!persisted || !candidate) return null
            return (
              <FeatureModeCard
                key={definition.feature}
                definition={definition}
                persisted={persisted}
                candidate={candidate}
                onCandidateChange={(mode) => setFeatureForms((current) => ({
                  ...current,
                  [definition.feature]: mode,
                }))}
                onSaved={(result) => setFeatureForms((current) => ({
                  ...current,
                  [result.feature]: result.mode,
                }))}
              />
            )
          })}
        </div>
      </section>

      <aside className="runtime-card feature-runtime-card" aria-label="本地模型运行说明">
        <h2>本地模型运行说明</h2>
        <dl>
          <div><dt>Ollama 服务</dt><dd>Docker 内网服务</dd></div>
          <div><dt>模型配置</dt><dd>由服务器统一管理，浏览器不接收内部服务地址</dd></div>
          <div><dt>故障处理</dt><dd>明确提示错误，由用户手动切换 Mock，不会静默降级</dd></div>
        </dl>
      </aside>

      <section className="audit-section" aria-labelledby="audit-title">
        <div className="section-heading"><h2 id="audit-title">最近审计记录</h2><p>仅展示最近 50 条已脱敏操作摘要。</p></div>
        {auditQuery.isPending && <p aria-live="polite">正在加载审计记录…</p>}
        {auditQuery.isError && <ErrorPanel title="审计记录加载失败" detail={auditQuery.error.message} onRetry={() => void auditQuery.refetch()} />}
        {auditQuery.data && (
          <div className="audit-table-wrap">
            <table>
              <thead><tr><th>事件类型</th><th>结果</th><th>模式</th><th>耗时</th><th>时间</th><th>操作摘要</th></tr></thead>
              <tbody>
                {auditQuery.data.map((event) => (
                  <tr key={event.id}>
                    <td>{event.event_type}</td>
                    <td>{event.result_status}</td>
                    <td>{event.mode === 'real' || event.mode === 'local' ? 'Local' : 'Mock'}</td>
                    <td>{event.elapsed_ms === null ? '—' : `${event.elapsed_ms} ms`}</td>
                    <td>{formatTimestamp(event.created_at)}</td>
                    <td>{event.request_summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {auditQuery.data.length === 0 && <p>暂无审计记录。</p>}
          </div>
        )}
      </section>
    </section>
  )
}
