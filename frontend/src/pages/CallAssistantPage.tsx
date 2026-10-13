import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { CallScenarioResponse, CallSessionResponse, CallSuggestionDelta, CallSuggestionDone, CallSuggestionError, CallSuggestionMeta, CallSummaryResponse, CallTemplateReference } from '../api/types'
import { ErrorPanel } from '../components/ErrorPanel'
import { StatusBadge } from '../components/StatusBadge'

type StreamState = 'idle' | 'streaming' | 'completed' | 'stopped' | 'error'

const initialTranscript = '客户反馈核心业务访问缓慢，希望确认故障受理和后续响应安排。'

function stateLabel(state: StreamState) {
  return { idle: '等待生成', streaming: '正在生成', completed: '生成完成', stopped: '已停止', error: '生成失败' }[state]
}

export function CallAssistantPage() {
  const scenariosQuery = useQuery({ queryKey: ['call-scenarios'], queryFn: () => api.get<CallScenarioResponse[]>('/api/demo/call-scenarios') })
  const controllerRef = useRef<AbortController | null>(null)
  const startedAtRef = useRef<number | null>(null)
  const ttftRecordedRef = useRef(false)
  const [scenarioId, setScenarioId] = useState('')
  const [transcript, setTranscript] = useState(initialTranscript)
  const [suggestion, setSuggestion] = useState('')
  const [acceptedText, setAcceptedText] = useState('')
  const [streamState, setStreamState] = useState<StreamState>('idle')
  const [streamError, setStreamError] = useState<string | null>(null)
  const [mode, setMode] = useState<CallSuggestionMeta['mode'] | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [templateReferences, setTemplateReferences] = useState<CallTemplateReference[]>([])
  const [ttft, setTtft] = useState<number | null>(null)
  const [summary, setSummary] = useState<CallSummaryResponse | null>(null)
  const [summaryError, setSummaryError] = useState<string | null>(null)
  const [isSummarizing, setIsSummarizing] = useState(false)
  const [copyMessage, setCopyMessage] = useState<string | null>(null)

  useEffect(() => () => controllerRef.current?.abort(), [])
  useEffect(() => {
    if (scenarioId || !scenariosQuery.data?.[0]) return
    setScenarioId(scenariosQuery.data[0].id)
  }, [scenarioId, scenariosQuery.data])

  const selectedScenario = scenariosQuery.data?.find((scenario) => scenario.id === scenarioId)
  const stop = () => controllerRef.current?.abort()
  const generate = async () => {
    if (!scenarioId || !transcript.trim()) return
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    startedAtRef.current = performance.now()
    setSuggestion('')
    setAcceptedText('')
    setSummary(null)
    setSummaryError(null)
    setStreamError(null)
    setMode(null)
    setSessionId(null)
    setTemplateReferences([])
    setTtft(null)
    ttftRecordedRef.current = false
    setStreamState('streaming')
    try {
      await api.streamSse<CallSuggestionMeta | CallSuggestionDelta | CallSuggestionDone | CallSuggestionError>(
        '/api/calls/suggestions/stream', { scenario_id: scenarioId, transcript: transcript.trim() },
        ({ event, data }) => {
          if (event === 'meta') {
            const meta = data as CallSuggestionMeta
            setMode(meta.mode)
            setSessionId(meta.session_id)
            setTemplateReferences(meta.templates)
          }
          if (event === 'delta') {
            if (startedAtRef.current !== null && !ttftRecordedRef.current) {
              ttftRecordedRef.current = true
              setTtft(Math.round(performance.now() - startedAtRef.current))
            }
            setSuggestion((current) => current + (data as CallSuggestionDelta).content)
          }
          if (event === 'done') setStreamState('completed')
          if (event === 'error') { setStreamError((data as CallSuggestionError).message); setStreamState('error') }
        },
        controller.signal,
      )
    } catch (error) {
      if (controller.signal.aborted) setStreamState('stopped')
      else { setStreamError(error instanceof Error ? error.message : '生成推荐话术失败'); setStreamState('error') }
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null
    }
  }
  const accept = () => {
    setAcceptedText(suggestion)
    if (!sessionId) return
    void api.put<CallSessionResponse>(`/api/calls/sessions/${sessionId}/adoption`, { accepted_reply: suggestion })
      .catch((error) => setStreamError(error instanceof Error ? error.message : '保存采纳状态失败'))
  }
  const copy = async () => {
    try { await navigator.clipboard.writeText(suggestion); setCopyMessage('已复制推荐话术') }
    catch { setCopyMessage('浏览器未授予复制权限，请手动复制') }
  }
  const makeSummary = async () => {
    if (!acceptedText.trim() || !sessionId) return
    setIsSummarizing(true); setSummaryError(null)
    try { setSummary(await api.post<CallSummaryResponse>('/api/calls/summary', {
      session_id: sessionId,
      scenario_id: scenarioId,
      original_transcript: transcript.trim(),
      accepted_reply: acceptedText.trim(),
    })) }
    catch (error) { setSummaryError(error instanceof Error ? error.message : '生成工单摘要失败') }
    finally { setIsSummarizing(false) }
  }

  if (scenariosQuery.isPending) return <section className="page-state" aria-live="polite">正在加载来电场景…</section>
  if (scenariosQuery.isError) return <ErrorPanel detail={scenariosQuery.error.message} onRetry={() => void scenariosQuery.refetch()} />

  return (
    <section className="call-page" aria-labelledby="page-title">
      <header className="page-header">
        <div><p className="eyebrow">实时辅助</p><h1 id="page-title">坐席辅助</h1><p>根据来电场景和通话记录生成可编辑的建议话术，采纳后再生成工单摘要。</p></div>
        <div className="page-actions"><StatusBadge tone={streamState === 'error' ? 'danger' : streamState === 'streaming' ? 'warning' : 'neutral'}>{stateLabel(streamState)}</StatusBadge>{mode && <StatusBadge tone={mode === 'real' ? 'warning' : 'neutral'}>{mode === 'real' ? '真实模型' : '演示数据'}</StatusBadge>}</div>
      </header>

      <div className="call-workspace">
        <section className="workspace-card call-context" aria-labelledby="call-context-title">
          <div className="section-heading"><h2 id="call-context-title">来电上下文</h2><p>只发送当前对话所需的场景和记录。</p></div>
          <label htmlFor="call-scenario">来电场景</label>
          <select id="call-scenario" value={scenarioId} onChange={(event) => setScenarioId(event.target.value)} disabled={streamState === 'streaming'}>{scenariosQuery.data.map((scenario) => <option value={scenario.id} key={scenario.id}>{scenario.name}</option>)}</select>
          {selectedScenario && <div className="scenario-details"><strong>{selectedScenario.name}</strong><span>{selectedScenario.description}</span><small>{selectedScenario.category} · {selectedScenario.priority}</small></div>}
          <label htmlFor="call-transcript">通话记录</label>
          <textarea id="call-transcript" value={transcript} onChange={(event) => setTranscript(event.target.value)} rows={11} disabled={streamState === 'streaming'} />
          <div className="form-actions"><button className="primary-button" type="button" onClick={() => void generate()} disabled={streamState === 'streaming' || !scenarioId || !transcript.trim()}>生成推荐话术</button>{streamState === 'streaming' && <button className="secondary-button" type="button" onClick={stop}>停止生成</button>}</div>
        </section>

        <section className="workspace-card call-suggestion" aria-labelledby="suggestion-title">
          <div className="section-heading"><h2 id="suggestion-title">推荐话术</h2><p>{ttft === null ? '等待首个输出' : `首字响应 ${ttft} ms`}</p></div>
          <div className="suggestion-content" aria-live="polite">{suggestion || (streamState === 'streaming' ? '正在组织建议话术…' : '选择场景后生成建议话术。')}</div>
          {templateReferences.length > 0 && <aside className="template-sources" aria-label="已审核话术来源"><strong>已审核话术来源</strong>{templateReferences.map((template) => <article key={template.id}><b>{template.title}</b><p>{template.content}</p></article>)}</aside>}
          {streamError && <p role="alert" className="danger-text">{streamError}</p>}
          <div className="form-actions"><button className="secondary-button" type="button" onClick={() => void copy()} disabled={!suggestion}>复制话术</button><button className="primary-button" type="button" onClick={accept} disabled={!suggestion || streamState === 'streaming'}>采纳话术</button></div>
          {copyMessage && <p role="status" className="form-status">{copyMessage}</p>}
        </section>

        <section className="workspace-card call-summary" aria-labelledby="summary-title">
          <div className="section-heading"><h2 id="summary-title">工单摘要</h2><p>采纳的话术可在生成摘要前继续编辑。</p></div>
          <label htmlFor="accepted-text">已采纳话术</label>
          <textarea id="accepted-text" value={acceptedText} onChange={(event) => setAcceptedText(event.target.value)} rows={8} placeholder="采纳推荐话术后可编辑" />
          <button className="primary-button" type="button" onClick={() => void makeSummary()} disabled={!acceptedText.trim() || !sessionId || isSummarizing}>{isSummarizing ? '正在生成摘要…' : '生成工单摘要'}</button>
          {summaryError && <p role="alert" className="danger-text">{summaryError}</p>}
          {summary && <dl className="call-summary-result"><div><dt>诉求</dt><dd>{summary.appeal}</dd></div><div><dt>处理过程</dt><dd>{summary.process}</dd></div><div><dt>处理结果</dt><dd>{summary.result}</dd></div><div><dt>后续跟进</dt><dd>{summary.follow_up}</dd></div></dl>}
        </section>
      </div>
    </section>
  )
}
