import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from 'react'

import { ApiError, api } from '../api/client'
import type { ReviewDocumentType, ReviewRisk, ReviewTaskResponse, ReviewTaskStatus } from '../api/types'
import { StatusBadge } from '../components/StatusBadge'

const MAX_UPLOAD_BYTES = 20 * 1024 * 1024
const activeStatuses = new Set<ReviewTaskStatus>(['queued', 'parsing', 'matching_rules', 'reviewing', 'summarizing'])
const retryableStatuses = new Set<ReviewTaskStatus>(['failed', 'cancelled', 'interrupted'])
const stageLabels: Array<[ReviewTaskStatus, string]> = [
  ['queued', '等待审核'], ['parsing', '文档解析'], ['matching_rules', '规则匹配'], ['reviewing', '内容审核'], ['summarizing', '结果汇总'], ['completed', '审核完成'],
]
const documentTypes: Array<{ value: ReviewDocumentType; label: string }> = [
  { value: 'design_document', label: '设计文档' }, { value: 'technical_proposal', label: '技术方案' }, { value: 'tender_document', label: '招标文件' },
]
const acceptedFiles: Record<string, string> = {
  '.pdf': 'application/pdf',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.txt': 'text/plain',
}

function fileExtension(file: File) { return `.${file.name.split('.').pop()?.toLowerCase() ?? ''}` }

async function validateFile(file: File): Promise<string | null> {
  const extension = fileExtension(file)
  if (!(extension in acceptedFiles)) return '仅支持 PDF、DOCX 和 UTF-8 TXT 文件。'
  if (file.type !== acceptedFiles[extension]) return '文件格式与其内容类型不一致，请重新选择文件。'
  if (file.size === 0) return '上传文件不能为空。'
  if (file.size > MAX_UPLOAD_BYTES) return '上传文件不能超过 20 MB。'
  if (extension === '.txt') {
    try { new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer()) }
    catch { return 'TXT 文件必须使用 UTF-8 编码。' }
  }
  return null
}

function statusLabel(status: ReviewTaskStatus) {
  return { queued: '等待审核', parsing: '文档解析', matching_rules: '规则匹配', reviewing: '内容审核', summarizing: '结果汇总', completed: '已完成', failed: '审核失败', cancelled: '已取消', interrupted: '已中断' }[status]
}

function riskLabel(risk: ReviewRisk) { return { high: '高风险', medium: '中风险', low: '低风险' }[risk] }
function humanReviewLabel(status: string) { return { pending: '待复核', confirmed: '已确认', dismissed: '已忽略', approved: '已通过', rejected: '已驳回', resolved: '已处理' }[status] ?? status }
function taskTone(status: ReviewTaskStatus) { return status === 'completed' ? 'success' : status === 'failed' || status === 'interrupted' ? 'danger' : status === 'cancelled' || activeStatuses.has(status) ? 'warning' : 'neutral' }
function stageLabel(stage: string) { return stageLabels.find(([value]) => value === stage)?.[1] ?? '当前阶段' }

export function ReviewPage() {
  const [tasks, setTasks] = useState<ReviewTaskResponse[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [documentType, setDocumentType] = useState<ReviewDocumentType>('design_document')
  const [validationError, setValidationError] = useState<string | null>(null)
  const [requestError, setRequestError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [riskFilter, setRiskFilter] = useState<'all' | ReviewRisk>('all')
  const [humanReviewFilter, setHumanReviewFilter] = useState('all')
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null)
  const mountedRef = useRef(true)
  const pollingRef = useRef(false)

  const selectedTask = tasks.find((task) => task.id === selectedId) ?? null
  const findings = useMemo(() => (selectedTask?.findings ?? []).filter((finding) =>
    (riskFilter === 'all' || finding.risk === riskFilter) && (humanReviewFilter === 'all' || finding.human_review_status === humanReviewFilter),
  ), [selectedTask, riskFilter, humanReviewFilter])
  const selectedFinding = findings.find((finding) => finding.id === selectedFindingId) ?? null
  const activeTaskIds = useMemo(() => tasks.filter((task) => activeStatuses.has(task.status)).map((task) => task.id), [tasks])

  const selectTask = useCallback((task: ReviewTaskResponse) => {
    setSelectedId(task.id)
    setSelectedFindingId(task.findings[0]?.id ?? null)
  }, [])
  const mergeTask = useCallback((task: ReviewTaskResponse, { select = false }: { select?: boolean } = {}) => {
    setTasks((current) => current.some((candidate) => candidate.id === task.id)
      ? current.map((candidate) => candidate.id === task.id ? task : candidate)
      : [task, ...current])
    if (select) selectTask(task)
  }, [selectTask])
  const loadTasks = useCallback(async () => {
    try {
      const nextTasks = await api.get<ReviewTaskResponse[]>('/api/reviews')
      if (!mountedRef.current) return
      setTasks(nextTasks)
      setSelectedId((current) => {
        if (current || !nextTasks[0]) return current
        setSelectedFindingId(nextTasks[0].findings[0]?.id ?? null)
        return nextTasks[0].id
      })
    } catch (error) { if (mountedRef.current) setRequestError(error instanceof Error ? error.message : '审核任务加载失败') }
  }, [])
  const refreshTask = useCallback(async (taskId: string, { select = false }: { select?: boolean } = {}) => {
    try {
      const task = await api.get<ReviewTaskResponse>(`/api/reviews/${taskId}`)
      if (mountedRef.current) mergeTask(task, { select })
    }
    catch (error) { if (mountedRef.current) setRequestError(error instanceof Error ? error.message : '审核任务状态更新失败') }
  }, [mergeTask])

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])
  useEffect(() => { void loadTasks() }, [loadTasks])
  useEffect(() => {
    if (!activeTaskIds.length) return
    const poll = async () => {
      if (pollingRef.current) return
      pollingRef.current = true
      try { await Promise.all(activeTaskIds.map((taskId) => refreshTask(taskId))) }
      finally { pollingRef.current = false }
    }
    const timer = window.setInterval(() => void poll(), 1000)
    return () => window.clearInterval(timer)
  }, [activeTaskIds, refreshTask])
  useEffect(() => {
    if (selectedFindingId && !findings.some((finding) => finding.id === selectedFindingId)) setSelectedFindingId(null)
  }, [findings, selectedFindingId])

  const selectFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const nextFile = event.target.files?.[0] ?? null
    setFile(nextFile); setValidationError(nextFile ? await validateFile(nextFile) : null)
  }
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!file) { setValidationError('请选择待审文件。'); return }
    const error = await validateFile(file)
    if (error) { setValidationError(error); return }
    setIsSubmitting(true); setValidationError(null); setRequestError(null)
    try {
      const formData = new FormData()
      formData.append('document_type', documentType)
      formData.append('file', file)
      const task = await api.post<ReviewTaskResponse>('/api/reviews', formData)
      mergeTask(task, { select: true })
      if (activeStatuses.has(task.status)) await refreshTask(task.id, { select: true })
    } catch (requestError) { setRequestError(requestError instanceof Error ? requestError.message : '提交审核任务失败') }
    finally { setIsSubmitting(false) }
  }
  const taskAction = async (action: 'retry' | 'cancel') => {
    if (!selectedTask) return
    setRequestError(null)
    try { mergeTask(await api.post<ReviewTaskResponse>(`/api/reviews/${selectedTask.id}/${action}`), { select: true }) }
    catch (error) { setRequestError(error instanceof Error ? error.message : '审核任务操作失败') }
  }
  const downloadCsv = async () => {
    if (!selectedTask) return
    try {
      const response = await fetch(`/api/reviews/${selectedTask.id}/export.csv`, { credentials: 'same-origin' })
      if (!response.ok) throw await ApiError.fromResponse(response, '导出审核清单失败')
      const url = URL.createObjectURL(await response.blob())
      const link = document.createElement('a')
      link.href = url; link.download = `review-${selectedTask.id}.csv`; document.body.append(link); link.click(); link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 0)
    } catch (error) { setRequestError(error instanceof Error ? error.message : '导出审核清单失败') }
  }

  return (
    <section className="review-page" aria-labelledby="page-title">
      <header className="page-header"><div><p className="eyebrow">异步审核</p><h1 id="page-title">文档审核</h1><p>依据已配置规则生成风险提示，所有结果均须经人工复核确认。</p></div>{selectedTask && <StatusBadge tone={taskTone(selectedTask.status)}>{statusLabel(selectedTask.status)}</StatusBadge>}</header>
      <div className="review-layout">
        <form className="workspace-card review-upload" onSubmit={(event) => void submit(event)}>
          <div className="section-heading"><h2>提交文档</h2><p>支持 PDF、DOCX、UTF-8 TXT，单文件不超过 20 MB。</p></div>
          <label htmlFor="review-file">选择待审文件</label>
          <input id="review-file" type="file" accept=".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain" onChange={(event) => void selectFile(event)} />
          {file && <p className="selected-file">已选择：{file.name}</p>}
          <label htmlFor="review-document-type">文件类型</label>
          <select id="review-document-type" value={documentType} onChange={(event) => setDocumentType(event.target.value as ReviewDocumentType)} disabled={isSubmitting}>{documentTypes.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select>
          {validationError && <p className="danger-text" role="alert">{validationError}</p>}
          <button className="primary-button" type="submit" disabled={!file || Boolean(validationError) || isSubmitting}>{isSubmitting ? '正在提交…' : '开始审核'}</button>
        </form>
        <section className="workspace-card review-progress" aria-labelledby="review-progress-title">
          <div className="section-heading"><h2 id="review-progress-title">审核进度</h2><p>{selectedTask ? `${selectedTask.document_filename} · ${selectedTask.progress}%` : '选择或提交任务后显示进度。'}</p></div>
          {tasks.length > 0 && <div className="review-task-list" aria-label="审核任务">{tasks.map((task) => <button key={task.id} type="button" className={task.id === selectedId ? 'is-selected' : ''} onClick={() => selectTask(task)}><span>{task.document_filename}</span><StatusBadge tone={taskTone(task.status)}>{statusLabel(task.status)}</StatusBadge></button>)}</div>}
          {!selectedTask ? <p className="empty-state">暂无审核任务。</p> : <>
            <ol className="review-timeline">{stageLabels.map(([stage, label], index) => { const currentStageIndex = stageLabels.findIndex(([value]) => value === selectedTask.stage); const isCurrent = index === currentStageIndex; const isTerminal = isCurrent && retryableStatuses.has(selectedTask.status); return <li key={stage} aria-current={isCurrent ? 'step' : undefined} className={`${isCurrent ? 'is-current' : index < currentStageIndex ? 'is-complete' : ''}${isTerminal ? ` is-terminal is-terminal-${selectedTask.status}` : ''}`}><span aria-hidden="true" />{label}</li> })}</ol>
            {retryableStatuses.has(selectedTask.status) && <p className="review-terminal-marker">{statusLabel(selectedTask.status)}，停留在{stageLabel(selectedTask.stage)}</p>}
            {selectedTask.error_summary && <p className="danger-text" role="alert">{selectedTask.error_summary}</p>}
            <div className="form-actions">{activeStatuses.has(selectedTask.status) && <button type="button" className="secondary-button" onClick={() => void taskAction('cancel')}>取消审核</button>}{retryableStatuses.has(selectedTask.status) && <button type="button" className="primary-button" onClick={() => void taskAction('retry')}>重新审核</button>}<button type="button" className="secondary-button" onClick={() => void downloadCsv()} disabled={selectedTask.status !== 'completed'}>导出审核清单</button></div>
          </>}
        </section>
      </div>
      {requestError && <p className="danger-text" role="alert">{requestError}</p>}
      <section className="workspace-card review-findings" aria-labelledby="review-findings-title">
        <div className="section-heading"><h2 id="review-findings-title">风险清单</h2><p>{selectedTask ? `共 ${selectedTask.findings.length} 项发现` : '审核完成后显示风险项。'}</p></div>
        <div className="review-filters"><label htmlFor="review-risk">风险等级</label><select id="review-risk" value={riskFilter} onChange={(event) => setRiskFilter(event.target.value as 'all' | ReviewRisk)}><option value="all">全部风险</option><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select><label htmlFor="review-human-status">人工复核状态</label><select id="review-human-status" value={humanReviewFilter} onChange={(event) => setHumanReviewFilter(event.target.value)}><option value="all">全部状态</option><option value="pending">待复核</option><option value="confirmed">已确认</option><option value="dismissed">已忽略</option><option value="approved">已通过</option><option value="rejected">已驳回</option><option value="resolved">已处理</option></select></div>
        <div className="review-findings-layout"><div className="finding-list">{findings.length ? findings.map((finding) => <button type="button" className={`finding-item${selectedFinding?.id === finding.id ? ' is-selected' : ''}`} key={finding.id} onClick={() => setSelectedFindingId(finding.id)}><StatusBadge tone={finding.risk === 'high' ? 'danger' : finding.risk === 'medium' ? 'warning' : 'success'}>{riskLabel(finding.risk)}</StatusBadge><strong>{finding.description}</strong><small>{finding.location} · {humanReviewLabel(finding.human_review_status)}</small></button>) : <p className="empty-state">没有符合当前筛选条件的风险项。</p>}</div>
          <aside className="finding-detail" aria-label="风险项详情">{selectedFinding ? <><h3>风险项详情</h3><dl><div><dt>原文摘录</dt><dd>{selectedFinding.excerpt}</dd></div><div><dt>规则出处</dt><dd>{selectedFinding.rule_source}</dd></div><div><dt>修改建议</dt><dd>{selectedFinding.recommendation}</dd></div></dl><p className="review-advisory">审核结果为辅助提示，不构成正式审核结论；请结合原文和业务要求进行人工复核。</p></> : <p className="empty-state">选择风险项后查看原文、规则出处和修改建议。</p>}</aside></div>
      </section>
    </section>
  )
}
