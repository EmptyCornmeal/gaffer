/**
 * 6.1 / 6.2 -- what week it is, and therefore what this surface is for.
 *
 * The app had one home page that showed the same thing on a Monday morning as
 * it did ninety seconds before a deadline. Nine per cent of the week is live
 * football, and `Live` held prime navigation for all of it; the other 91% it
 * was a dead tab. Meanwhile the question a reader actually arrives with changes
 * completely across the week: on Monday it is "what happened?", on Thursday it
 * is "is it too early to decide?", on Friday it is "what do I do?".
 *
 * So the surface is selected by deadline-relative state. Two rules keep that
 * from being a gimmick:
 *
 *   1. **The state is always visible** (6.2). A surface that silently changes
 *      shape is harder to learn, not easier. The label is not decoration; it is
 *      the thing that makes the behaviour legible.
 *   2. **Nothing is hidden, only ordered.** Every state can reach everything.
 *      A reader who wants the decision on a Monday is not told to come back on
 *      Friday -- the decision is simply not the first thing on the page.
 *
 * Pure functions, so every boundary is testable against a fixed clock instead
 * of by waiting for one.
 */

/** The five states, plus the locked gap that 5.4 named. */
export type NowState = 'review' | 'watch' | 'wait' | 'decide' | 'locked' | 'live'

export interface NowContext {
  /** ms. The next deadline, or null when none is published. */
  deadline: number | null
  /** ms. Now. */
  now: number
  /** Is football being played right now? From the live artifact, not guessed. */
  footballOn: boolean
  /** Has a gameweek finished whose review has not been read yet? */
  hasFreshReview: boolean
}

/**
 * How long before a deadline the surface starts leading with the decision.
 *
 * A DECLARED PRODUCT CHOICE, not a measurement, and labelled as one wherever it
 * is shown. It is set where it is because FPL deadlines are on the morning of
 * the first fixture and the last team news lands the day before: 30 hours puts
 * a Saturday 11:00 deadline into `decide` from Friday morning, which is when
 * the press conferences have happened.
 */
export const DECIDE_HOURS = 30

/**
 * ...and the point before that where the honest answer is "not yet".
 *
 * Also a declared choice. Between here and DECIDE_HOURS the projections exist
 * and are worth seeing, but acting on them means acting before the team news,
 * which is the single largest thing Gaffer cannot see.
 */
export const WAIT_HOURS = 78

const HOUR = 3_600_000

/** How long after a deadline the gameweek is locked but no ball has been kicked. */
export const LOCKED_HOURS = 4

export function nowState(ctx: NowContext): NowState {
  // Football beats everything. Whatever the clock says, if the ball is moving
  // then the only number that changes is the score.
  if (ctx.footballOn) return 'live'

  if (ctx.deadline == null || Number.isNaN(ctx.deadline)) {
    return ctx.hasFreshReview ? 'review' : 'watch'
  }

  const untilHours = (ctx.deadline - ctx.now) / HOUR

  // 5.4's gap: the squad is locked and the football has not started.
  if (untilHours <= 0) {
    return untilHours >= -LOCKED_HOURS ? 'locked' : 'watch'
  }

  if (untilHours <= DECIDE_HOURS) return 'decide'

  // A finished gameweek nobody has looked at outranks a distant deadline: the
  // first thing a reader wants on a Monday is what happened, not a projection
  // for a match five days away.
  if (ctx.hasFreshReview) return 'review'

  return untilHours <= WAIT_HOURS ? 'wait' : 'watch'
}

export interface StateCopy {
  /** Two or three words, shown as the state chip. */
  label: string
  /** One sentence: what this surface is for right now. */
  says: string
  /** The tone of the chip. */
  tone: 'good' | 'warn' | 'bad' | 'info'
}

export const STATE_COPY: Record<NowState, StateCopy> = {
  review: {
    label: 'Review',
    says: 'A gameweek has finished. This is what happened and what it says about the advice.',
    tone: 'info',
  },
  watch: {
    label: 'Watch',
    says: 'Nothing to decide yet. The deadline is far enough away that acting now buys nothing.',
    tone: 'info',
  },
  wait: {
    label: 'Wait',
    says: 'The projections are ready, but the team news is not. Deciding now means deciding without it.',
    tone: 'warn',
  },
  decide: {
    label: 'Decide',
    says: 'This is the window. The recommendation and what it rests on come first.',
    tone: 'good',
  },
  locked: {
    label: 'Locked',
    says: 'The deadline has passed. Nothing can be changed until the gameweek ends.',
    tone: 'bad',
  },
  live: {
    label: 'Live',
    says: 'Football is being played. Scores are provisional until the bonus settles.',
    tone: 'good',
  },
}

/**
 * What the surface leads with, in order. Every state lists every block: this
 * is an ORDERING, not a set of feature flags, and that is deliberate. A reader
 * who wants the decision on a Monday scrolls; they are never told to come back
 * later.
 */
export type NowBlock = 'decision' | 'calendar' | 'live' | 'review' | 'squad' | 'league'

const ALL: NowBlock[] = ['decision', 'calendar', 'live', 'review', 'squad', 'league']

const LEAD: Record<NowState, NowBlock[]> = {
  review: ['review', 'league', 'decision'],
  watch: ['squad', 'decision', 'league'],
  wait: ['calendar', 'decision', 'squad'],
  decide: ['decision', 'calendar', 'squad'],
  locked: ['squad', 'review', 'league'],
  live: ['live', 'league', 'squad'],
}

export function blockOrder(state: NowState): NowBlock[] {
  const lead = LEAD[state] ?? []
  return [...lead, ...ALL.filter((b) => !lead.includes(b))]
}

/** Human text for the countdown, without pretending to a precision it lacks. */
export function timeToDeadline(ctx: NowContext): string | null {
  if (ctx.deadline == null || Number.isNaN(ctx.deadline)) return null
  const h = (ctx.deadline - ctx.now) / HOUR
  if (h <= 0) return 'deadline passed'
  if (h < 1) return `${Math.max(1, Math.round(h * 60))} min`
  if (h < 48) return `${Math.round(h)}h`
  return `${Math.round(h / 24)} days`
}
