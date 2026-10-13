import { useEffect, useRef, useState, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { KnowledgeDocumentDetail, KnowledgeDocumentStatus, KnowledgeDocumentSummary } from '../api/types'
import { ErrorPanel } from '../components/ErrorPanel'
import { StatusBadge } from '../components/StatusBadge'

type ReviewAction = 'approve' | 'reject' | 'archive'

const statusOptions: Array<{ value: '' | KnowledgeDocumentStatus; label: string }> = [
  { value: '', label: '全部状态' },
  { value: 'uploaded', label: '筛选：已上传' },
  { value: 'parsing', label: '筛选：解析中' },
  { value: 'pending_review', label: '筛选：待审核' },
  { value: 'indexing', label: '筛选：入库中' },
  { value: 'active', label: '筛选：已生效' },
  { value: 'parse_failed', label: '筛选：解析失败' },
  { value: 'index_failed', label: '筛选：入库失败' },
  { value: 'rejected', label: '筛选：已驳回' },
  { value: 'archived', label: '筛选：已归档' },
]

const statusLabels: Record<KnowledgeDocumentStatus, string> = {
  uploaded: '已上传',
  parsing: '解析中',
  pending_review: '待审核',
  indexing: '入库中',
  active: '已生效',
  parse_failed: '解析失败',
  index_failed: '入库失败',
  rejected: '已驳回',
  archived: '已归档',
}

const actionCopy: Record<ReviewAction, { dialog: string; description: string; confirm: string }> = {
  approve: { dialog: '确认审核通过', description: '审核通过后将建立活动索引，资料会参与知识检索。', confirm: '确认入库' },
  reject: { dialog: '确认驳回资料', description: '驳回后资料不会参与知识检索。', confirm: '确认驳回' },
  archive: { dialog: '确认归档资料', description: '归档后资料将从活动检索范围移除。', confirm: '确认归档' },
}

function statusLabel(status: KnowledgeDocumentStatus) {
  return statusLabels[status]
}

function statusTone(status: KnowledgeDocumentStatus): 'success' | 'warning' | 'danger' | 'neutral' {
  if (status === 'active') return 'success'
  if (status === 'parse_failed' || status === 'index_failed' || status === 'rejected') return 'danger'
  if (status === 'parsing' || status === 'pending_review' || status === 'indexing') return 'warning'
  return 'neutral'
}

function shouldPoll(status: KnowledgeDocumentStatus) {
  return status === 'parsing' || status === 'indexing'
}

function formatDate(value: string | null) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short', hour12: false }).format(new Date(value))
}

function ConfirmDialog({ action, documentTitle, busy, onCancel, onConfirm, returnFocusTo, fallbackFocusTo, backgroundElement }: {
  action: ReviewAction | null
  documentTitle: string
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
  returnFocusTo: HTMLElement | null
  fallbackFocusTo: HTMLElement | null
  backgroundElement: HTMLElement | null
}) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  const dialogRef = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!action) return
    const hadAriaHidden = backgroundElement?.getAttribute('aria-hidden')
    if (backgroundElement) {
      backgroundElement.inert = true
      backgroundElement.setAttribute('inert', '')
      backgroundElement.setAttribute('aria-hidden', 'true')
    }
    cancelRef.current?.focus()
    return () => {
      if (backgroundElement) {
        backgroundElement.inert = false
        backgroundElement.removeAttribute('inert')
        if (hadAriaHidden === null || hadAriaHidden === undefined) backgroundElement.removeAttribute('aria-hidden')
        else backgroundElement.setAttribute('aria-hidden', hadAriaHidden)
      }
      if (returnFocusTo?.isConnected) returnFocusTo.focus()
      else if (fallbackFocusTo?.isConnected) fallbackFocusTo.focus()
    }
  }, [action, backgroundElement, fallbackFocusTo, returnFocusTo])

  if (!action) return null
  const copy = actionCopy[action]
  const onKeyDown = (event: React.KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape' && !busy) { event.preventDefault(); onCancel(); return }
    if (event.key !== 'Tab') return
    const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not([disabled])') ?? [])
    if (!focusable.length) { event.preventDefault(); return }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }
  return createPortal(
    <div className="confirm-backdrop" role="presentation">
      <section ref={dialogRef} className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title" onKeyDown={onKeyDown}>
        <p className="eyebrow">人工审核确认</p>
        <h2 id="confirm-dialog-title">{copy.dialog}</h2>
        <p><strong>{documentTitle}</strong></p>
        <p>{copy.description}</p>
        <div className="form-actions">
          <button ref={cancelRef} type="button" className="secondary-button" onClick={onCancel} disabled={busy}>取消</button>
          <button type="button" className="primary-button" onClick={onConfirm} disabled={busy}>{busy ? '正在处理…' : copy.confirm}</button>
        </div>
      </section>
    </div>,
    document.body,
  )
}

export function KnowledgeDocumentsPage() {
  const queryClient = useQueryClient()
  const pageRef = useRef<HTMLElement>(null)
  const actionTriggerRef = useRef<HTMLButtonElement | null>(null)
  const previewHeadingRef = useRef<HTMLHeadingElement>(null)
  const pinnedSelectionRef = useRef<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<'' | KnowledgeDocumentStatus>('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [title, setTitle] = useState('')
  const [category, setCategory] = useState('数据库、备份与恢复')
  const [scope, setScope] = useState('全局')
  const [version, setVersion] = useState('1.0')
  const [effectiveDate, setEffectiveDate] = useState('')
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<ReviewAction | null>(null)

  const listPath = statusFilter ? `/api/knowledge-documents?status=${encodeURIComponent(statusFilter)}` : '/api/knowledge-documents'
  const documentsQuery = useQuery({
    queryKey: ['knowledge-documents', statusFilter],
    queryFn: () => api.get<KnowledgeDocumentSummary[]>(listPath),
    refetchInterval: (query) => query.state.data?.some((item) => shouldPoll(item.status)) ? 1000 : false,
  })
  const detailQuery = useQuery({
    queryKey: ['knowledge-document', selectedId],
    queryFn: () => api.get<KnowledgeDocumentDetail>(`/api/knowledge-documents/${selectedId}`),
    enabled: selectedId !== null,
    refetchInterval: (query) => query.state.data && shouldPoll(query.state.data.status) ? 1000 : false,
  })
  const detail = detailQuery.data

  useEffect(() => {
    const documents = documentsQuery.data
    if (selectedId && pinnedSelectionRef.current === selectedId) {
      if (documents?.some((item) => item.id === selectedId)) pinnedSelectionRef.current = null
      return
    }
    if (!documents?.length) {
      if (!selectedId) return
      const selectedStillVisible = documents?.some((item) => item.id === selectedId)
      if (selectedStillVisible === false) setSelectedId(null)
      return
    }
    if (!selectedId || !documents.some((item) => item.id === selectedId)) setSelectedId(documents[0].id)
  }, [documentsQuery.data, selectedId])

  const uploadMutation = useMutation({
    mutationFn: (body: FormData) => api.post<KnowledgeDocumentSummary>('/api/knowledge-documents', body),
    onSuccess: async (uploaded) => {
      pinnedSelectionRef.current = uploaded.id
      setUploadError(null)
      await queryClient.invalidateQueries({ queryKey: ['knowledge-documents'] })
      if (pinnedSelectionRef.current === uploaded.id) setSelectedId(uploaded.id)
    },
    onError: (error) => setUploadError(error instanceof Error ? error.message : '资料上传失败'),
  })
  const actionMutation = useMutation({
    mutationFn: ({ id, action }: { id: string; action: ReviewAction | 'retry' }) => api.post<KnowledgeDocumentDetail | KnowledgeDocumentSummary>(`/api/knowledge-documents/${id}/${action}`),
    onSuccess: async (updated, variables) => {
      setActionError(null)
      if (variables.action !== 'retry' && 'chunks' in updated) queryClient.setQueryData(['knowledge-document', variables.id], updated)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['knowledge-documents'] }),
        queryClient.invalidateQueries({ queryKey: ['knowledge-document', variables.id] }),
      ])
    },
    onError: async (error, variables) => {
      setActionError(error instanceof Error ? error.message : '资料操作失败')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['knowledge-documents'] }),
        queryClient.invalidateQueries({ queryKey: ['knowledge-document', variables.id] }),
      ])
    },
  })

  const upload = (event: FormEvent) => {
    event.preventDefault()
    if (!file || !title.trim() || !category.trim() || !scope.trim() || !version.trim()) return
    const body = new FormData()
    body.append('title', title.trim())
    body.append('category', category.trim())
    body.append('scope', scope.trim())
    body.append('version', version.trim())
    if (effectiveDate) body.append('effective_date', effectiveDate)
    body.append('file', file)
    uploadMutation.mutate(body)
  }
  const openConfirmation = (action: ReviewAction, trigger: HTMLButtonElement) => {
    actionTriggerRef.current = trigger
    setActionError(null)
    setPendingAction(action)
  }
  const confirmAction = () => {
    if (!detail || !pendingAction) return
    const action = pendingAction
    actionMutation.mutate(
      { id: detail.id, action },
      { onSettled: () => setPendingAction(null) },
    )
  }
  const retry = () => {
    if (!detail) return
    setActionError(null)
    actionMutation.mutate({ id: detail.id, action: 'retry' })
  }

  return (
    <>
      <section ref={pageRef} className="knowledge-documents-page" aria-labelledby="page-title">
        <header className="page-header">
          <div><p className="eyebrow">审核后入库</p><h1 id="page-title">知识资料管理</h1><p>上传资料先解析预览，经人工审核后才会进入活动知识索引。</p></div>
          <div className="page-actions"><a className="secondary-button" href="/knowledge">返回知识问答</a></div>
        </header>
        <div className="knowledge-documents-layout">
          <aside className="document-sidebar">
            <form className="workspace-card document-upload" onSubmit={upload}>
              <div className="section-heading"><h2>上传运维资料</h2><p>支持 TXT、PDF、DOCX，单文件不超过 20 MiB。</p></div>
              <label htmlFor="document-file">选择运维资料</label>
              <input id="document-file" type="file" required accept=".txt,.pdf,.docx,text/plain,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={(event) => {
                const nextFile = event.target.files?.[0] ?? null
                setFile(nextFile)
                if (nextFile && !title.trim()) setTitle(nextFile.name.replace(/\.[^.]+$/, ''))
              }} />
              <label htmlFor="document-title">资料标题</label>
              <input id="document-title" value={title} onChange={(event) => setTitle(event.target.value)} required />
              <label htmlFor="document-category">分类</label>
              <input id="document-category" value={category} onChange={(event) => setCategory(event.target.value)} required />
              <label htmlFor="document-scope">适用范围</label>
              <input id="document-scope" value={scope} onChange={(event) => setScope(event.target.value)} required />
              <div className="document-form-row">
                <div><label htmlFor="document-version">版本</label><input id="document-version" value={version} onChange={(event) => setVersion(event.target.value)} required /></div>
                <div><label htmlFor="document-effective-date">生效日期</label><input id="document-effective-date" type="date" value={effectiveDate} onChange={(event) => setEffectiveDate(event.target.value)} /></div>
              </div>
              <button type="submit" className="primary-button" disabled={!file || !title.trim() || !category.trim() || !scope.trim() || !version.trim() || uploadMutation.isPending}>{uploadMutation.isPending ? '正在上传…' : '上传并解析'}</button>
              {uploadError && <p className="danger-text" role="alert">{uploadError}</p>}
            </form>
            <section className="workspace-card document-list-panel" aria-labelledby="document-list-title">
              <div className="section-heading"><h2 id="document-list-title">资料列表</h2><p>解析与入库期间每秒自动刷新。</p></div>
              <label htmlFor="document-status-filter">状态筛选</label>
              <select id="document-status-filter" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as '' | KnowledgeDocumentStatus)}>
                {statusOptions.map((item) => <option key={item.value || 'all'} value={item.value}>{item.label}</option>)}
              </select>
              {documentsQuery.isPending && <p className="empty-state">正在加载资料…</p>}
              {documentsQuery.isError && <ErrorPanel title="资料列表加载失败" detail={documentsQuery.error.message} onRetry={() => void documentsQuery.refetch()} />}
              {documentsQuery.data?.length === 0 && <p className="empty-state">当前筛选下暂无资料。</p>}
              <div className="document-list">
                {documentsQuery.data?.map((document) => (
                  <button type="button" key={document.id} aria-pressed={document.id === selectedId} className={document.id === selectedId ? 'is-selected' : ''} onClick={() => { pinnedSelectionRef.current = null; setSelectedId(document.id); setActionError(null) }}>
                    <span><strong>{document.title}</strong><small>{document.category} · {document.version}</small></span>
                    <StatusBadge tone={statusTone(document.status)}>{statusLabel(document.status)}</StatusBadge>
                  </button>
                ))}
              </div>
            </section>
          </aside>
          <section className="workspace-card document-preview" aria-labelledby="document-preview-title">
            <div className="section-heading"><h2 id="document-preview-title">解析预览与审核</h2><p>核对元数据、警告和原文位置后再执行审核操作。</p></div>
            {!selectedId && <p className="empty-state">从资料列表中选择一份资料查看详情。</p>}
            {detailQuery.isPending && selectedId && <p className="empty-state">正在加载解析预览…</p>}
            {detailQuery.isError && <ErrorPanel title="解析预览加载失败" detail={detailQuery.error.message} onRetry={() => void detailQuery.refetch()} />}
            {detail && (
              <>
                <div className="document-preview-heading"><div><h3 ref={previewHeadingRef} tabIndex={-1}>{detail.title}</h3><p>{detail.content_summary || '暂无内容摘要'}</p></div></div>
                <dl className="document-metadata">
                  <div><dt>分类</dt><dd>{detail.category}</dd></div>
                  <div><dt>适用范围</dt><dd>{detail.scope}</dd></div>
                  <div><dt>版本</dt><dd>{detail.version}</dd></div>
                  <div><dt>生效日期</dt><dd>{detail.effective_date || '—'}</dd></div>
                  <div><dt>媒体类型</dt><dd>{detail.media_type || '—'}</dd></div>
                  <div><dt>文件大小</dt><dd>{detail.file_size === null ? '—' : `${detail.file_size} B`}</dd></div>
                  <div><dt>创建时间</dt><dd>{formatDate(detail.created_at)}</dd></div>
                  <div><dt>审核人</dt><dd>{detail.reviewed_by || '—'}</dd></div>
                </dl>
                {detail.error_summary && <p className="document-error-summary" role="alert">{detail.error_summary}</p>}
                {detail.parse_warnings.length > 0 && <aside className="parse-warnings" aria-label="解析警告"><strong>解析警告</strong><ul>{detail.parse_warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></aside>}
                <div className="document-action-bar">
                  {detail.status === 'pending_review' && (
                    <>
                      <button type="button" className="primary-button" onClick={(event) => openConfirmation('approve', event.currentTarget)}>审核通过</button>
                      <button type="button" className="secondary-button" onClick={(event) => openConfirmation('reject', event.currentTarget)}>驳回</button>
                    </>
                  )}
                  {detail.status === 'active' && <button type="button" className="secondary-button" onClick={(event) => openConfirmation('archive', event.currentTarget)}>归档</button>}
                  {(detail.status === 'parse_failed' || detail.status === 'index_failed') && <button type="button" className="primary-button" onClick={retry} disabled={actionMutation.isPending}>重试</button>}
                </div>
                {actionError && <p className="danger-text" role="alert">{actionError}</p>}
                <section className="chunk-preview" aria-labelledby="chunk-preview-title">
                  <div className="section-heading"><h3 id="chunk-preview-title">解析片段</h3><p>共 {detail.chunks.length} 个片段</p></div>
                  {detail.chunks.length === 0 && <p className="empty-state">解析尚未完成，暂无片段。</p>}
                  {detail.chunks.map((chunk) => <article key={chunk.id}><header><strong>片段 {chunk.position}</strong><span>{chunk.location}</span></header><p>{chunk.content}</p></article>)}
                </section>
              </>
            )}
          </section>
        </div>
      </section>
      <ConfirmDialog
        action={pendingAction}
        documentTitle={detail?.title ?? ''}
        busy={actionMutation.isPending}
        onCancel={() => setPendingAction(null)}
        onConfirm={confirmAction}
        returnFocusTo={actionTriggerRef.current}
        fallbackFocusTo={previewHeadingRef.current}
        backgroundElement={pageRef.current}
      />
    </>
  )
}
