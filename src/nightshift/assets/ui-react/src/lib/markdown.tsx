/**
 * Minimal, dependency-free Markdown → HTML for the task brief preview, ported
 * from the legacy app.js (escapeHtml / inlineMd / renderMarkdown). The whole
 * source is HTML-escaped up front, so no author-supplied markup survives; the
 * block parser then emits a safe, fixed tag set (headings, lists, blockquotes,
 * fenced code, rules, paragraphs) with inline formatting via inlineMd.
 *
 * Returns an HTML string; render it with the <Markdown> component below, which
 * wraps it in a `.markdown-body` container (spacing rules live in theme.css).
 */

function escapeHtml(s: string): string {
  return String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      })[c] as string,
  )
}

// Inline Markdown on an already-escaped string: code spans, links, bold, italic.
// Links are limited to http(s)/mailto/relative targets so a `javascript:` URL
// can't sneak in.
function inlineMd(s: string): string {
  return s
    .split(/`([^`]+)`/)
    .map((part, idx) => {
      if (idx % 2 === 1) return `<code>${part}</code>`
      return part
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) =>
          /^(https?:|mailto:)/i.test(url) || /^[/#]/.test(url)
            ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`
            : m,
        )
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/__([^_]+)__/g, '<strong>$1</strong>')
        .replace(/\*([^*]+)\*/g, '<em>$1</em>')
        .replace(/(^|[^\w])_([^_]+)_(?=[^\w]|$)/g, '$1<em>$2</em>')
    })
    .join('')
}

export function renderMarkdown(src: string): string {
  const lines = escapeHtml(src || '').split('\n')
  const out: string[] = []
  let para: string[] = []
  let list: { type: 'ul' | 'ol'; items: string[] } | null = null
  let quote: string[] = []

  const closePara = () => {
    if (para.length) {
      out.push(`<p>${inlineMd(para.join('<br>'))}</p>`)
      para = []
    }
  }
  const closeList = () => {
    if (list) {
      const items = list.items.map((it) => `<li>${inlineMd(it)}</li>`).join('')
      out.push(`<${list.type}>${items}</${list.type}>`)
      list = null
    }
  }
  const closeQuote = () => {
    if (quote.length) {
      out.push(`<blockquote>${inlineMd(quote.join('<br>'))}</blockquote>`)
      quote = []
    }
  }
  const closeAll = () => {
    closePara()
    closeList()
    closeQuote()
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]

    // Fenced code block — copy verbatim until the closing fence (or EOF).
    const fence = line.match(/^\s*(`{3,}|~{3,})/)
    if (fence) {
      closeAll()
      const marker = fence[1][0]
      const buf: string[] = []
      const close = new RegExp(`^\\s*\\${marker}{3,}\\s*$`)
      for (i++; i < lines.length && !close.test(lines[i]); i++) buf.push(lines[i])
      out.push(`<pre><code>${buf.join('\n')}</code></pre>`)
      continue
    }
    if (/^\s*$/.test(line)) {
      closeAll()
      continue
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      closeAll()
      out.push('<hr>')
      continue
    }

    const h = line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/)
    if (h) {
      closeAll()
      out.push(`<h${h[1].length}>${inlineMd(h[2])}</h${h[1].length}>`)
      continue
    }

    const bq = line.match(/^\s*&gt;\s?(.*)$/) // `>` is escaped to `&gt;`
    if (bq) {
      closePara()
      closeList()
      quote.push(bq[1])
      continue
    }
    closeQuote()

    const ul = line.match(/^\s*[-*+]\s+(.*)$/)
    const ol = ul ? null : line.match(/^\s*\d+[.)]\s+(.*)$/)
    if (ul || ol) {
      closePara()
      const type = ul ? 'ul' : 'ol'
      if (!list || list.type !== type) {
        closeList()
        list = { type, items: [] }
      }
      list.items.push((ul || ol)![1])
      continue
    }
    closeList()
    para.push(line.trim())
  }
  closeAll()
  return out.join('\n')
}

/** Render a markdown string into the styled `.markdown-body` container. */
export function Markdown({ source }: { source: string }) {
  return (
    <div
      className="markdown-body text-sm text-text"
      // eslint-disable-next-line react/no-danger -- output is escaped + fixed-tag
      dangerouslySetInnerHTML={{ __html: renderMarkdown(source) }}
    />
  )
}
