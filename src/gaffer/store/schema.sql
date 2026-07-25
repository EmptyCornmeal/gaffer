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
    base_xg90           REAL DEFAULT 0,        -- last-season xG/90 (survives the FPL stats reset)
    base_xa90           REAL DEFAULT 0,        -- last-season xA/90
    base_minutes        INTEGER DEFAULT 0,     -- last-season minutes (reliability baseline)
    base_starts         INTEGER DEFAULT 0,     -- last-season starts
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

-- Per-player per-gameweek actuals (populated as the season plays out).
CREATE TABLE IF NOT EXISTS player_gw (
    player_id       INTEGER REFERENCES players(id),
    gw              INTEGER,
    minutes         INTEGER,
    total_points    INTEGER,
    goals           INTEGER,
    assists         INTEGER,
    clean_sheet     INTEGER,
    bonus           INTEGER,
    bps             INTEGER,
    defcon          INTEGER,                    -- defensive_contribution actions
    xg              REAL,
    xa              REAL,
    was_home        INTEGER,
    opponent_team   INTEGER,
    PRIMARY KEY (player_id, gw)
);

-- Understat per-player season aggregate, matched to an FPL id where possible.
CREATE TABLE IF NOT EXISTS understat_player (
    us_id           INTEGER PRIMARY KEY,
    name            TEXT,
    team            TEXT,
    season          TEXT,
    games           INTEGER,
    minutes         INTEGER,
    goals           INTEGER,
    assists         INTEGER,
    xg              REAL,
    xa              REAL,
    npxg            REAL,
    shots           INTEGER,
    key_passes      INTEGER,
    fpl_id          INTEGER REFERENCES players(id),
    match_score     REAL                        -- fuzzy-match confidence 0-1
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
    exp_points      REAL,
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
    purchase_price  INTEGER,
    selling_price   INTEGER,
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
