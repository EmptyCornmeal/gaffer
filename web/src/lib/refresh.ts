// Polite matchday polling (T-22).
//
// The FPL API is free and unauthenticated, and Gaffer reaches it through a small
// shared proxy. That makes restraint a correctness property, not just good
// manners: an aggressive poller would get the proxy rate-limited for everyone.
//
// The rules, all enforced here rather than left to each caller:
//
//   * a hidden tab polls slowly, or not at all
//   * only one request is ever in flight; a tick during a fetch is dropped
//   * failures back off exponentially and recover on the next success
//   * the last good state is KEPT and marked stale, never blanked
//   * the timestamp of the last successful update is always available
//
// Deliberately framework-free so it can be unit-tested with a fake clock instead
// of a browser.

export type RefreshStatus = 'idle' | 'loading' | 'ok' | 'stale' | 'error'

export interface RefreshState<T> {
  status: RefreshStatus
  data: T | null
  /** ms epoch of the last SUCCESSFUL update, or null if there has never been one. */
  lastSuccess: number | null
  lastError: string | null
  consecutiveFailures: number
  inFlight: boolean
}

export interface RefreshOptions {
  /** Base interval while the tab is visible and matches are live. */
  intervalMs?: number
  /** Interval while the tab is hidden. 0 disables polling entirely. */
  hiddenIntervalMs?: number
  /** Longest gap after repeated failures. */
  maxBackoffMs?: number
  /** Data older than this is presented as stale even if the last fetch worked. */
  staleAfterMs?: number
}

export const DEFAULTS: Required<RefreshOptions> = {
  intervalMs: 60_000,        // once a minute during a match is plenty
  hiddenIntervalMs: 0,       // a backgrounded tab polls nothing at all
  maxBackoffMs: 10 * 60_000,
  staleAfterMs: 5 * 60_000,
}

/**
 * Next delay in ms, or null when polling should stop.
 *
 * Exported separately from the controller so the pacing logic can be tested
 * against a table of states rather than by waiting in real time.
 */
export function nextDelay(
  state: Pick<RefreshState<unknown>, 'consecutiveFailures'>,
  { visible, active }: { visible: boolean; active: boolean },
  opts: RefreshOptions = {},
): number | null {
  const o = { ...DEFAULTS, ...opts }
  if (!active) return null
  if (!visible) return o.hiddenIntervalMs > 0 ? o.hiddenIntervalMs : null
  if (state.consecutiveFailures === 0) return o.intervalMs
  const backoff = o.intervalMs * 2 ** state.consecutiveFailures
  return Math.min(backoff, o.maxBackoffMs)
}

/** Is the displayed data still current, given when it last succeeded? */
export function isStale(
  lastSuccess: number | null, now: number, opts: RefreshOptions = {},
): boolean {
  const o = { ...DEFAULTS, ...opts }
  if (lastSuccess == null) return true
  return now - lastSuccess > o.staleAfterMs
}

export function initialState<T>(data: T | null = null): RefreshState<T> {
  return {
    status: data == null ? 'idle' : 'ok',
    data,
    lastSuccess: null,
    lastError: null,
    consecutiveFailures: 0,
    inFlight: false,
  }
}

/**
 * Apply one fetch outcome to the state.
 *
 * A failure keeps `data` — the last valid scoreboard is far more useful than an
 * empty one — and marks it stale so nothing on screen claims to be current.
 */
export function applyResult<T>(
  state: RefreshState<T>,
  outcome: { ok: true; data: T; at: number } | { ok: false; error: string; at: number },
  opts: RefreshOptions = {},
): RefreshState<T> {
  if (outcome.ok) {
    return {
      status: 'ok',
      data: outcome.data,
      lastSuccess: outcome.at,
      lastError: null,
      consecutiveFailures: 0,
      inFlight: false,
    }
  }
  return {
    ...state,
    status: state.data == null ? 'error' : 'stale',
    lastError: outcome.error,
    consecutiveFailures: state.consecutiveFailures + 1,
    inFlight: false,
  }
}

/**
 * When the data on screen was *generated*, rather than when it was fetched.
 *
 * The two diverge exactly where it matters. Falling back to the published
 * artifact is a *successful* fetch of a file that may be hours old, so the
 * poller's `lastSuccess` says "just now" about data from this morning — a stale
 * snapshot wearing a live label, which is the single thing this page must never
 * do. Prefer the payload's own timestamp; use the fetch time only when it does
 * not carry one.
 */
export function dataTimestamp(
  generatedAtIso: string | null | undefined,
  lastSuccess: number | null,
): number | null {
  const t = generatedAtIso ? Date.parse(generatedAtIso) : NaN
  return Number.isFinite(t) ? t : lastSuccess
}

/** Human label for the last successful update. */
export function freshnessLabel(
  lastSuccess: number | null, now: number = Date.now(),
): string {
  if (lastSuccess == null) return 'never updated'
  const s = Math.max(0, Math.floor((now - lastSuccess) / 1000))
  if (s < 10) return 'just now'
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  return `${Math.floor(m / 60)}h ago`
}

type Fetcher<T> = () => Promise<T>

/**
 * A self-pacing poller.
 *
 * `onState` fires on every transition so a Svelte component can simply assign
 * the result. Nothing here touches the DOM beyond the visibility listener.
 */
export class Poller<T> {
  private timer: ReturnType<typeof setTimeout> | null = null
  private stopped = true
  state: RefreshState<T>

  constructor(
    private fetcher: Fetcher<T>,
    private onState: (s: RefreshState<T>) => void,
    private opts: RefreshOptions = {},
    private isActive: () => boolean = () => true,
    private isVisible: () => boolean =
      () => typeof document === 'undefined' || !document.hidden,
    private now: () => number = () => Date.now(),
  ) {
    this.state = initialState<T>()
  }

  private emit() {
    this.onState({ ...this.state })
  }

  async tick(): Promise<void> {
    // A tick arriving while a request is outstanding is dropped, not queued:
    // two concurrent fetches of the same endpoint is exactly the duplicate
    // request this class exists to prevent.
    if (this.state.inFlight) return
    this.state = { ...this.state, inFlight: true, status:
      this.state.data == null ? 'loading' : this.state.status }
    this.emit()
    try {
      const data = await this.fetcher()
      this.state = applyResult(this.state, { ok: true, data, at: this.now() },
                               this.opts)
    } catch (e) {
      this.state = applyResult(
        this.state, { ok: false, error: String(e), at: this.now() }, this.opts)
    }
    this.emit()
  }

  private schedule() {
    if (this.stopped) return
    const delay = nextDelay(
      this.state, { visible: this.isVisible(), active: this.isActive() },
      this.opts)
    if (delay == null) return
    this.timer = setTimeout(async () => {
      await this.tick()
      this.schedule()
    }, delay)
  }

  start(): void {
    this.stopped = false
    void this.tick().then(() => this.schedule())
  }

  /** Called when the tab becomes visible: refresh at once, then resume pacing. */
  wake(): void {
    if (this.stopped) return
    if (this.timer) clearTimeout(this.timer)
    void this.tick().then(() => this.schedule())
  }

  stop(): void {
    this.stopped = true
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
  }
}
