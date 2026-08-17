-- Gaffer SQLite schema. Idempotent: safe to run on every pipeline start.

CREATE TABLE IF NOT EXISTS teams (
    id                  INTEGER PRIMARY KEY,
    code                INTEGER,               -- crest code (badges/t{code}.png)
    name                TEXT NOT NULL,
    short               TEXT NOT NULL,
    strength_att_home   INTEGER,
    strength_att_away   INTEGER,
    strength_def_home   INTEGER,
    strength_def_away   INTEGER,
    strength_overall    INTEGER
);

CREATE TABLE IF NOT EXISTS players (
    id                  INTEGER PRIMARY KEY,   -- FPL element id
    code                INTEGER,               -- opta/photo code (for player photos)
    web_name            TEXT NOT NULL,
    first_name          TEXT,
    second_name         TEXT,
    team_id             INTEGER REFERENCES teams(id),
    position            TEXT NOT NULL,         -- GKP/DEF/MID/FWD
    price               INTEGER NOT NULL,      -- now_cost, tenths of a million
    status              TEXT,                  -- a/d/i/s/u/n (available/doubtful/injured/...)
    chance_playing      INTEGER,               -- chance_of_playing_next_round (0-100 or NULL)
    selected_by_pct     REAL,
    transfers_in_event  INTEGER DEFAULT 0,
    transfers_out_event INTEGER DEFAULT 0,
    cost_change_event   INTEGER DEFAULT 0,     -- price change this GW (tenths)
    cost_change_start   INTEGER DEFAULT 0,     -- cumulative change since GW1 (tenths)
    minutes             INTEGER DEFAULT 0,     -- season-to-date
    starts              INTEGER DEFAULT 0,
    form                REAL DEFAULT 0,
    points_per_game     REAL DEFAULT 0,
    ep_next             REAL,                  -- FPL's own expected points (baseline to beat)
    ict_index           REAL DEFAULT 0,        -- FPL's ICT composite (influence/creativity/threat)
    xg_per_90           REAL DEFAULT 0,
    xa_per_90           REAL DEFAULT 0,
    xgi_per_90          REAL DEFAULT 0,
    xgc_per_90          REAL DEFAULT 0,        -- expected_goals_conceded_per_90 (team defence proxy)
    defcon_per_90       REAL DEFAULT 0,        -- defensive_contribution_per_90 from FPL
    saves_per_90        REAL DEFAULT 0,        -- T-13 scoring rates, from season totals
    yellow_per_90       REAL DEFAULT 0,
    red_per_90          REAL DEFAULT 0,
    og_per_90           REAL DEFAULT 0,
    pen_save_per_90     REAL DEFAULT 0,
    pen_miss_per_90     REAL DEFAULT 0,
    bonus_per_90        REAL DEFAULT 0,
    base_xg90           REAL DEFAULT 0,        -- last-season xG/90 (survives the FPL stats reset)
    base_xa90           REAL DEFAULT 0,        -- last-season xA/90
    base_minutes        INTEGER DEFAULT 0,     -- last-season minutes (reliability baseline)
    base_starts         INTEGER DEFAULT 0,     -- last-season starts
    -- Last-season defensive contributions per 90 (G-L). The DEFCON counterpart
    -- of base_xg90, and it exists so the projection can tell a CURRENT-season
    -- rate from a PRIOR-season one. `defcon_per_90` alone cannot: FPL resets
    -- `minutes` at the season rollover but keeps its per-90 fields, and
    -- `ingest.ingest_players` additionally falls back to last season's figure,
    -- so out of season that column holds a prior-season rate beside a
    -- current-season minutes count of 0. Shrinking one against the other threw
    -- away the best DEFCON evidence in the system.
    --
    -- Nullable ON PURPOSE, and it is the only base_* column that is. NULL means
    -- "no prior season has been read for this player" and 0.0 means "read, and
    -- he made none" — two different claims, and `ingest.enrich_history` needs to
    -- tell them apart to know whom to backfill exactly once. A DEFAULT 0 here
    -- would make every already-enriched player look permanently unread.
    base_defcon90       REAL,
    -- WHICH season the base_* above came from, as FPL labels it ('2024/25').
    -- history_past[-1] is the most recent season FPL HOLDS for that player, and
    -- for anyone who spent time outside the Premier League that is not last
    -- season at all. Without this the artifact stamped every baseline with the
    -- current prior season and presented an old cameo as current evidence.
    base_season         TEXT DEFAULT '',
    news                TEXT,
    set_piece_notes     TEXT
);

CREATE TABLE IF NOT EXISTS fixtures (
    id              INTEGER PRIMARY KEY,
    gw              INTEGER,                    -- event; NULL for unscheduled
    team_h          INTEGER REFERENCES teams(id),
    team_a          INTEGER REFERENCES teams(id),
    kickoff         TEXT,
    fdr_h           INTEGER,                    -- FPL difficulty for home team
    fdr_a           INTEGER,
    finished        INTEGER DEFAULT 0
);

-- Per-player per-fixture actuals, retained across seasons.
--
-- Season-aware because FPL reuses element ids every season: without it, a new
-- season's player 1 would merge onto last season's player 1. Keyed by fixture,
-- not gameweek, so a double gameweek stores both matches.
--
-- These are POST-MATCH values. They exist to build evaluation targets and to
-- reconstruct historical decision points; nothing here may be fed into a
-- pre-deadline feature (see gaffer.leakage).
CREATE TABLE IF NOT EXISTS player_gw (
    season          TEXT NOT NULL,
    player_id       INTEGER NOT NULL,
    gw              INTEGER NOT NULL,
    fixture         INTEGER NOT NULL,
    kickoff_time    TEXT,
    minutes         INTEGER,
    total_points    INTEGER,
    goals           INTEGER,
    assists         INTEGER,
    clean_sheet     INTEGER,
    goals_conceded  INTEGER,
    own_goals       INTEGER,
    penalties_saved INTEGER,
    penalties_missed INTEGER,
    yellow_cards    INTEGER,
    red_cards       INTEGER,
    saves           INTEGER,
    bonus           INTEGER,
    bps             INTEGER,
    starts          INTEGER,
    defcon          INTEGER,                    -- defensive_contribution actions
    xg              REAL,
    xa              REAL,
    xgi             REAL,
    xgc             REAL,
    value           INTEGER,                    -- price at that gameweek (tenths)
    selected        INTEGER,                    -- ownership count at that gameweek
    was_home        INTEGER,
    opponent_team   INTEGER,
    ingested_at     TEXT,                       -- when we last saw/updated this row
    PRIMARY KEY (season, player_id, fixture)
);

-- Immutable-by-default snapshots of what the model projected, and when.
--
-- `projections` is wiped and rewritten every run, so without this there is no
-- record to score the model against once results land. `as_of` is the run
-- timestamp; `is_pre_deadline` marks snapshots taken before the target event's
-- deadline — the only ones a fair backtest may use.
CREATE TABLE IF NOT EXISTS projection_snapshots (
    season          TEXT NOT NULL,
    target_gw       INTEGER NOT NULL,
    player_id       INTEGER NOT NULL,
    as_of           TEXT NOT NULL,              -- ISO 8601 UTC, the run timestamp
    model_version   TEXT NOT NULL,
    horizon         INTEGER NOT NULL,           -- target_gw - projection_event
    is_pre_deadline INTEGER NOT NULL DEFAULT 1,
    deadline_time   TEXT,                       -- the target event's deadline
    p_start         REAL,
    exp_minutes     REAL,
    exp_goal_pts    REAL,
    exp_assist_pts  REAL,
    exp_cs_pts      REAL,
    exp_defcon_pts  REAL,
    exp_bonus_pts   REAL,
    exp_appearance  REAL,
    exp_conceded_pts REAL DEFAULT 0,
    exp_saves_pts   REAL DEFAULT 0,
    exp_cards_pts   REAL DEFAULT 0,
    exp_misc_pts    REAL DEFAULT 0,
    exp_points      REAL,
    confidence      REAL,
    availability    REAL,                       -- status/chance multiplier used
    PRIMARY KEY (season, target_gw, player_id, as_of)
);


-- Model output: one row per (player, gw) in the projection horizon.
CREATE TABLE IF NOT EXISTS projections (
    player_id       INTEGER REFERENCES players(id),
    gw              INTEGER,
    p_start         REAL,
    exp_minutes     REAL,
    exp_goal_pts    REAL,
    exp_assist_pts  REAL,
    exp_cs_pts      REAL,
    exp_defcon_pts  REAL,
    exp_bonus_pts   REAL,
    exp_appearance  REAL,
    exp_conceded_pts REAL DEFAULT 0,
    exp_saves_pts   REAL DEFAULT 0,
    exp_cards_pts   REAL DEFAULT 0,
    exp_misc_pts    REAL DEFAULT 0,
    exp_points      REAL,                        -- shipped value (may be blended)
    exp_points_model REAL,                       -- Gaffer's own component sum
    exp_points_ep_next REAL,                     -- FPL's ep_next, where available
    confidence      REAL,                        -- 0-1
    model_version   TEXT,
    generated_at    TEXT,
    PRIMARY KEY (player_id, gw)
);

-- The user's current squad (latest known picks).
CREATE TABLE IF NOT EXISTS my_squad (
    gw              INTEGER,
    player_id       INTEGER REFERENCES players(id),
    is_captain      INTEGER DEFAULT 0,
    is_vice         INTEGER DEFAULT 0,
    multiplier      INTEGER DEFAULT 1,
    purchase_price  INTEGER,                    -- tenths of a million
    selling_price   INTEGER,                    -- tenths; FPL's sell-on rule
    price_source    TEXT,                       -- transfer_in|season_start|manual|conservative
    price_exact     INTEGER DEFAULT 0,          -- 1 when the purchase price is known
    PRIMARY KEY (gw, player_id)
);

-- Free-form key/value for entry-level state (bank, team value, free transfers, chips).
CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

CREATE INDEX IF NOT EXISTS idx_players_team ON players(team_id);
CREATE INDEX IF NOT EXISTS idx_projections_gw ON projections(gw);
CREATE INDEX IF NOT EXISTS idx_fixtures_gw ON fixtures(gw);
CREATE INDEX IF NOT EXISTS idx_player_gw_season ON player_gw(season, gw);
CREATE INDEX IF NOT EXISTS idx_player_gw_player ON player_gw(season, player_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_target
    ON projection_snapshots(season, target_gw, is_pre_deadline);

-- ---------------------------------------------------------------------------
-- Batch 5: the weekly loop
-- ---------------------------------------------------------------------------

-- What Gaffer recommended BEFORE a deadline. Append-only and immutable once the
-- deadline passes: `gaffer.snapshots.record` refuses to write after it, so this
-- table is the only honest baseline a post-gameweek review can score against.
-- Season-aware because FPL reuses element ids every year.
CREATE TABLE IF NOT EXISTS decision_snapshots (
    season          TEXT NOT NULL,
    entry_id        INTEGER NOT NULL,
    target_event    INTEGER NOT NULL,
    as_of           TEXT NOT NULL,              -- ISO 8601 UTC, the run timestamp
    deadline        TEXT NOT NULL,              -- the target event's deadline
    is_pre_deadline INTEGER NOT NULL DEFAULT 1,
    schema_version  INTEGER NOT NULL DEFAULT 1,
    content_hash    TEXT NOT NULL,              -- the decision, minus volatile stamps
    payload         TEXT NOT NULL,              -- JSON: the full versioned snapshot
    PRIMARY KEY (season, entry_id, target_event, as_of)
);

-- Post-gameweek reviews. Keyed to the snapshot they score (`snapshot_as_of`), so
-- a review can never drift onto a different decision than the one it judged.
-- Rewritable ONLY because FPL revises points after bonus/appeal review; the
-- decision side is read from `decision_snapshots` and never edited here.
CREATE TABLE IF NOT EXISTS gw_reviews (
    season          TEXT NOT NULL,
    entry_id        INTEGER NOT NULL,
    event           INTEGER NOT NULL,
    generated_at    TEXT NOT NULL,
    snapshot_as_of  TEXT,                       -- NULL when no snapshot existed
    schema_version  INTEGER NOT NULL DEFAULT 1,
    payload         TEXT NOT NULL,              -- JSON: attribution, luck, lesson
    PRIMARY KEY (season, entry_id, event)
);

-- Notification delivery state. Season-aware, and keyed by a stable dedupe key so
-- a re-run cannot re-alert on the same fact.
CREATE TABLE IF NOT EXISTS notifications (
    season          TEXT NOT NULL,
    dedupe_key      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    event           INTEGER,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    deep_link       TEXT,
    created_at      TEXT NOT NULL,
    state           TEXT NOT NULL,              -- pending|sent|failed|suppressed|dry_run
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    last_error      TEXT,
    dry_run         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (season, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_decision_snapshots_event
    ON decision_snapshots(season, entry_id, target_event, is_pre_deadline);
CREATE INDEX IF NOT EXISTS idx_reviews_event ON gw_reviews(season, entry_id, event);
CREATE INDEX IF NOT EXISTS idx_notifications_state ON notifications(season, state);
