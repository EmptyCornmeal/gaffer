// Render a shareable, branded team card to a PNG (client-side canvas, no deps).
// Used by the "Share" button — the FPL-Twitter growth loop.

export interface SharePlayer {
  name: string
  pos: string
  team: string
  isC: boolean
  isVC: boolean
}

export interface ShareTeam {
  title: string
  subtitle: string
  players: SharePlayer[]
}

const POS_ORDER = ['GKP', 'DEF', 'MID', 'FWD']
const POS_LABEL: Record<string, string> = { GKP: 'Goalkeeper', DEF: 'Defence', MID: 'Midfield', FWD: 'Attack' }

export function renderTeamCard(team: ShareTeam): Promise<Blob> {
  const W = 1080
  const H = 1350
  const c = document.createElement('canvas')
  c.width = W
  c.height = H
  const g = c.getContext('2d')!
  const F = (px: number, bold = false) => `${bold ? '700 ' : ''}${px}px Inter, system-ui, sans-serif`

  // background + header band
  g.fillStyle = '#080e1a'
  g.fillRect(0, 0, W, H)
  g.fillStyle = '#0b1322'
  g.fillRect(0, 0, W, 210)
  g.fillStyle = '#10b981'
  g.fillRect(0, 206, W, 4)

  g.textBaseline = 'alphabetic'
  g.fillStyle = '#10b981'
  g.font = F(66, true)
  g.fillText('Gaffer', 60, 115)
  g.fillStyle = '#8ea0b5'
  g.font = F(28)
  g.fillText(team.subtitle, 60, 165)

  g.fillStyle = '#e6edf5'
  g.font = F(44, true)
  g.fillText(team.title, 60, 295)

  let y = 360
  for (const pos of POS_ORDER) {
    const ps = team.players.filter((p) => p.pos === pos)
    if (!ps.length) continue
    g.fillStyle = '#3987e5'
    g.font = F(24, true)
    g.fillText((POS_LABEL[pos] ?? pos).toUpperCase(), 60, y)
    y += 12
    for (const p of ps) {
      y += 52
      const tag = p.isC ? '  (C)' : p.isVC ? '  (V)' : ''
      g.fillStyle = p.isC ? '#34d399' : '#e6edf5'
      g.font = F(34, p.isC)
      g.fillText(p.name + tag, 90, y)
      g.fillStyle = '#8ea0b5'
      g.font = F(26)
      g.textAlign = 'right'
      g.fillText(p.team, W - 60, y)
      g.textAlign = 'left'
    }
    y += 30
  }

  g.fillStyle = '#5a6b80'
  g.font = F(24)
  g.fillText('Built with Gaffer · emptycornmeal.github.io/gaffer', 60, H - 54)

  return new Promise((resolve, reject) =>
    c.toBlob((b) => (b ? resolve(b) : reject(new Error('render failed'))), 'image/png'),
  )
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
