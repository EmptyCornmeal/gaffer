// Minimal, safe markdown → HTML for the Verdict card. Escapes HTML first, then
// applies a tiny subset (**bold**, `code`, bullets, paragraphs). Content comes
// from our own pipeline/AI, but we escape anyway.
export function mdLite(src: string): string {
  const esc = (s: string) =>
    s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  const inline = (s: string) =>
    esc(s)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/`(.+?)`/g, '<code>$1</code>')

  const blocks: string[] = []
  let list: string[] = []
  const flush = () => {
    if (list.length) {
      blocks.push(`<ul>${list.map((li) => `<li>${inline(li)}</li>`).join('')}</ul>`)
      list = []
    }
  }
  for (const raw of src.split('\n')) {
    const line = raw.trim()
    if (!line) {
      flush()
      continue
    }
    if (/^[-*]\s+/.test(line)) {
      list.push(line.replace(/^[-*]\s+/, ''))
    } else {
      flush()
      blocks.push(`<p>${inline(line)}</p>`)
    }
  }
  flush()
  return blocks.join('')
}
