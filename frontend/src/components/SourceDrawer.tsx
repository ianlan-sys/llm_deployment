import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

import type { KnowledgeCitation } from '../api/types'

const focusableSelector = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

function formatUpdatedAt(value: string) {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short', hour12: false }).format(new Date(value))
}

export function SourceDrawer({ citation, onClose, returnFocusTo, backgroundElement }: {
  citation: KnowledgeCitation | null
  onClose: () => void
  returnFocusTo: HTMLElement | null
  backgroundElement: HTMLElement | null
}) {
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const dialogRef = useRef<HTMLElement>(null)

  useEffect(() => {
    if (!citation) return
    const hadAriaHidden = backgroundElement?.getAttribute('aria-hidden')
    const hadInert = backgroundElement?.hasAttribute('inert')
    if (backgroundElement) {
      backgroundElement.setAttribute('inert', '')
      backgroundElement.inert = true
      backgroundElement.setAttribute('aria-hidden', 'true')
    }
    closeButtonRef.current?.focus()
    return () => {
      if (backgroundElement) {
        backgroundElement.inert = false
        if (!hadInert) backgroundElement.removeAttribute('inert')
        if (hadAriaHidden === null || hadAriaHidden === undefined) backgroundElement.removeAttribute('aria-hidden')
        else backgroundElement.setAttribute('aria-hidden', hadAriaHidden)
      }
      if (returnFocusTo?.isConnected) returnFocusTo.focus()
    }
  }, [backgroundElement, citation, returnFocusTo])

  if (!citation) return null
  const trapFocus = (event: React.KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') { event.preventDefault(); onClose(); return }
    if (event.key !== 'Tab') return
    const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(focusableSelector) ?? [])
    if (!focusable.length) { event.preventDefault(); return }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }

  return createPortal(
    <div className="source-drawer-backdrop" role="presentation" onMouseDown={onClose}>
      <aside className="source-drawer" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="source-drawer-title" onKeyDown={trapFocus} onMouseDown={(event) => event.stopPropagation()}>
        <div className="drawer-heading">
          <div><p className="eyebrow">可追溯引用</p><h2 id="source-drawer-title">资料出处</h2></div>
          <button type="button" ref={closeButtonRef} className="icon-button" aria-label="关闭资料出处" onClick={onClose}>×</button>
        </div>
        <dl className="source-details">
          <div><dt>资料标题</dt><dd>{citation.source_title}</dd></div>
          <div><dt>更新时间</dt><dd>{formatUpdatedAt(citation.source_updated_at)}</dd></div>
          <div><dt>检索分数</dt><dd>{citation.score.toFixed(3)}</dd></div>
          <div><dt>位置</dt><dd>{citation.location}</dd></div>
          <div><dt>资料标识</dt><dd>{citation.source_id}</dd></div>
          <div><dt>片段标识</dt><dd>{citation.chunk_id}</dd></div>
          <div><dt>原文片段</dt><dd>{citation.text}</dd></div>
        </dl>
      </aside>
    </div>,
    document.body,
  )
}
