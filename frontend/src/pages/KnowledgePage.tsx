import { useEffect, useRef, useState, type FormEvent } from 'react'

import { api } from '../api/client'
import { postSse } from '../api/sse'
import type {
  KnowledgeCitation,
  KnowledgeDeltaEvent,
  KnowledgeDoneEvent,
  KnowledgeErrorEvent,
  KnowledgeMetaEvent,
  KnowledgeRetrievalEvent,
} from '../api/types'
import { SourceDrawer } from '../components/SourceDrawer'
import { StatusBadge } from '../components/StatusBadge'

type KnowledgeStreamData = KnowledgeRetrievalEvent | KnowledgeMetaEvent | KnowledgeDeltaEvent | KnowledgeDoneEvent | KnowledgeErrorEvent
type QueryStage = 'idle' | 'retrieval' | 'hit' | 'generation' | 'completed' | 'error'
interface ObservedStages {
  retrieval: boolean
  retrievalComplete: boolean
  hit: boolean
  generation: boolean
  completed: boolean
}

const scopes = [
  { value: '', label: '全部已纳管资料' },
  { value: '核心节点A', label: '核心节点 A' },
  { value: '核心节点B', label: '核心节点 B' },
  { value: '全局', label: '全局资料' },
]

const recommendedQuestions = [
  '核心节点A的 VLAN 和网关分别是什么？',
  'Linux 与 Windows 服务器日常巡检有哪些共同项目？',
  '数据库备份需要保留多久，如何验证备份完整性？',
  '特权账号申请和季度权限复核分别要做什么？',
  '一级故障和二级故障的响应时限有什么区别？',
  '常规维护窗口是什么时间？',
  '核心节点A范围内有哪些网络与变更注意事项？',
  '如果数据库不可用，应先检查哪些信息再恢复？',
  '哪些资料共同要求变更前准备回退措施？',
  '月球节点的管理员口令是什么？',
]

export function KnowledgePage() {
  const [question, setQuestion] = useState('')
  const [scope, setScope] = useState('')
  const [answer, setAnswer] = useState('')
  const [answerStatus, setAnswerStatus] = useState<'answered' | 'no_evidence' | null>(null)
  const [citations, setCitations] = useState<KnowledgeCitation[]>([])
  const [retrievalEngine, setRetrievalEngine] = useState<string | null>(null)
  const [mode, setMode] = useState<KnowledgeMetaEvent['mode'] | null>(null)
  const [model, setModel] = useState<string | null>(null)
  const [elapsedMs, setElapsedMs] = useState<number | null>(null)
  const [disclaimer, setDisclaimer] = useState<string | null>(null)
  const [stage, setStage] = useState<QueryStage>('idle')
  const [observedStages, setObservedStages] = useState<ObservedStages>({
    retrieval: false,
    retrievalComplete: false,
    hit: false,
    generation: false,
    completed: false,
  })
  const [selectedCitation, setSelectedCitation] = useState<KnowledgeCitation | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isSwitching, setIsSwitching] = useState(false)
  const controllerRef = useRef<AbortController | null>(null)
  const pageRef = useRef<HTMLElement>(null)
  const invokingCitationRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => () => controllerRef.current?.abort(), [])

  const isStreaming = ['retrieval', 'hit', 'generation'].includes(stage)
  const stop = () => controllerRef.current?.abort()
  const runQuery = async () => {
    if (!question.trim()) return
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    let terminalReceived = false
    setAnswer('')
    setAnswerStatus(null)
    setCitations([])
    setRetrievalEngine(null)
    setMode(null)
    setModel(null)
    setElapsedMs(null)
    setDisclaimer(null)
    setSelectedCitation(null)
    setError(null)
    setStage('retrieval')
    setObservedStages({
      retrieval: true,
      retrievalComplete: false,
      hit: false,
      generation: false,
      completed: false,
    })
    try {
      await postSse<KnowledgeStreamData>(
        '/api/knowledge/query/stream',
        { question: question.trim(), scope: scope || null },
        ({ event, data }) => {
          if (controllerRef.current !== controller || controller.signal.aborted || terminalReceived) return
          if (event === 'retrieval') {
            const retrieval = data as KnowledgeRetrievalEvent
            setCitations(retrieval.citations)
            setRetrievalEngine(retrieval.retrieval_engine)
            setStage(retrieval.citations.length ? 'hit' : 'retrieval')
            setObservedStages((current) => ({
              ...current,
              retrieval: true,
              retrievalComplete: true,
              hit: retrieval.citations.length > 0,
            }))
          } else if (event === 'meta') {
            const metadata = data as KnowledgeMetaEvent
            setMode(metadata.mode)
            setModel(metadata.model)
            setStage('generation')
            setObservedStages((current) => ({ ...current, generation: true }))
          } else if (event === 'delta') {
            setAnswer((current) => current + (data as KnowledgeDeltaEvent).content)
            setStage('generation')
            setObservedStages((current) => ({ ...current, generation: true }))
          } else if (event === 'done') {
            const done = data as KnowledgeDoneEvent
            terminalReceived = true
            setAnswerStatus(done.status)
            if (done.answer !== undefined) setAnswer(done.answer)
            setDisclaimer(done.disclaimer ?? null)
            setElapsedMs(done.elapsed_ms)
            setStage('completed')
            setObservedStages((current) => ({ ...current, completed: true }))
          } else if (event === 'error') {
            terminalReceived = true
            setError((data as KnowledgeErrorEvent).message)
            setStage('error')
          }
        },
        controller.signal,
      )
      if (controllerRef.current !== controller) return
      if (!terminalReceived) {
        setError('知识问答流意外结束，请重试。')
        setStage('error')
      }
    } catch (requestError) {
      if (controllerRef.current !== controller) return
      if (controller.signal.aborted) setError('已停止本次查询，请按需重试。')
      else setError(requestError instanceof Error ? requestError.message : '知识查询失败')
      setStage('error')
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null
    }
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    void runQuery()
  }
  const switchToMock = async () => {
    setIsSwitching(true)
    setError(null)
    try {
      await api.put('/api/settings/features/knowledge', { mode: 'mock' })
      await runQuery()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '切换 Mock 模式失败')
      setStage('error')
    } finally {
      setIsSwitching(false)
    }
  }

  return (
    <section ref={pageRef} className="knowledge-page" aria-labelledby="page-title">
      <header className="page-header">
        <div><p className="eyebrow">可追溯问答</p><h1 id="page-title">运维知识库</h1><p>仅基于已纳管资料回答；每个结论均可追溯至对应的资料片段。</p></div>
        <div className="page-actions"><a className="secondary-button" href="/knowledge/documents">管理知识资料</a></div>
      </header>
      <div className="knowledge-layout">
        <form className="workspace-card knowledge-query" onSubmit={submit}>
          <div className="section-heading"><h2>检索范围</h2><p>限定范围可以提升引用的针对性。</p></div>
          <label htmlFor="knowledge-scope">资料范围</label>
          <select id="knowledge-scope" value={scope} onChange={(event) => setScope(event.target.value)} disabled={isStreaming}>{scopes.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select>
          <label htmlFor="knowledge-question">运维问题</label>
          <textarea id="knowledge-question" value={question} onChange={(event) => setQuestion(event.target.value)} rows={5} placeholder="例如：核心交换机 VLAN 如何划分？" disabled={isStreaming} />
          <div className="form-actions knowledge-submit-actions">
            <button className="primary-button" type="submit" disabled={!question.trim() || isStreaming}>{isStreaming ? '正在查询…' : '查询'}</button>
            {isStreaming && <button className="secondary-button" type="button" onClick={stop}>停止生成</button>}
          </div>
          <div className="recommended-questions" aria-label="推荐问题">
            <strong>推荐问题</strong>
            {recommendedQuestions.map((item) => <button type="button" key={item} onClick={() => setQuestion(item)} disabled={isStreaming}>{item}</button>)}
          </div>
          <aside className="knowledge-safety-note" aria-label="操作边界"><strong>操作边界</strong><span>配置建议仅供人工评估，不会直接下发到任何设备；实际变更须按既有审批流程执行。</span></aside>
        </form>
        <section className="workspace-card knowledge-answer" aria-labelledby="answer-title">
          <div className="section-heading">
            <h2 id="answer-title">检索结果</h2>
            <div className="answer-metadata">
              {mode && <StatusBadge tone={mode === 'local' ? 'warning' : 'neutral'}>{mode === 'local' ? '本地模型' : 'Mock'}</StatusBadge>}
              {model && <StatusBadge>{model}</StatusBadge>}
              {stage === 'completed' && <span>总耗时 {elapsedMs} ms</span>}
              {retrievalEngine && <span>检索引擎：{retrievalEngine}</span>}
            </div>
          </div>
          {stage === 'idle' && <p className="empty-state">提交运维问题后，这里会显示带来源引用的回答。</p>}
          {stage !== 'idle' && (
            <ol className="knowledge-stages" aria-label="知识问答阶段" aria-live="polite">
              <li className={observedStages.retrieval ? 'is-reached' : ''}><span aria-hidden="true" /><div><strong>正在检索本地资料</strong></div></li>
              <li className={observedStages.hit ? 'is-reached' : ''}>
                <span aria-hidden="true" />
                <div>
                  <strong>{citations.length > 0 ? '已命中资料' : observedStages.retrievalComplete ? '未命中可靠资料' : '等待检索结果'}</strong>
                  {citations.length > 0 && <ul>{Array.from(new Set(citations.map((item) => item.source_title))).map((title) => <li key={title}>{title}</li>)}</ul>}
                </div>
              </li>
              <li className={observedStages.generation ? 'is-reached' : ''}><span aria-hidden="true" /><div><strong>{model ? `${model} 正在${mode === 'mock' ? '以 Mock 模式' : '本地'}生成` : '等待模型生成'}</strong></div></li>
              <li className={observedStages.completed || stage === 'error' ? `is-reached${stage === 'error' ? ' is-error' : ''}` : ''}><span aria-hidden="true" /><div><strong>{stage === 'error' ? '回答生成失败' : '流式回答完成'}</strong></div></li>
            </ol>
          )}
          {error && <section className="knowledge-error"><p role="alert" className="danger-text">{error}</p><div className="form-actions"><button type="button" className="primary-button" onClick={() => void runQuery()} disabled={isSwitching}>重试</button><a className="secondary-button" href="/settings">打开运行设置</a><button type="button" className="secondary-button" onClick={() => void switchToMock()} disabled={isSwitching}>{isSwitching ? '正在切换…' : '切换此功能至 Mock'}</button></div></section>}
          {answerStatus === 'no_evidence' && <section className="no-evidence" aria-live="polite"><h3>未找到可靠依据</h3><p>{answer}</p><span>请调整关键词或缩小资料范围后重试。</span></section>}
          {answer && answerStatus !== 'no_evidence' && <article className="answer-card" aria-live="polite"><h3>依据资料的回答</h3><p>{answer}</p></article>}
          {answerStatus === 'answered' && (
            <>
              <div className="citation-list" aria-label="引用来源">{citations.map((citation, index) => <button type="button" key={citation.chunk_id} className="citation-button" aria-label={`来源 ${index + 1}`} onClick={(event) => { invokingCitationRef.current = event.currentTarget; setSelectedCitation(citation) }}>来源 {index + 1}<small>{citation.location}</small></button>)}</div>
              {disclaimer && <aside className="knowledge-disclaimer" aria-label="引用说明">{disclaimer}</aside>}
            </>
          )}
        </section>
      </div>
      <SourceDrawer citation={selectedCitation} onClose={() => setSelectedCitation(null)} returnFocusTo={invokingCitationRef.current} backgroundElement={pageRef.current} />
    </section>
  )
}
