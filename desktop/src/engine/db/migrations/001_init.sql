-- §S3. Every timestamp is UTC ISO-8601 ("2026-10-14T21:41:25.123Z"). JSON columns are checked.

-- A series (§S2): its validated config, stored as JSON in schema order.
CREATE TABLE series (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  config     TEXT NOT NULL CHECK (json_valid(config)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- One publishing day of a series: its files, sheet row, title suggestions and the approved title (D6).
CREATE TABLE days (
  id              INTEGER PRIMARY KEY,
  series_id       TEXT NOT NULL REFERENCES series (id),
  date            TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
  files           TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(files)),
  row             TEXT CHECK (row IS NULL OR json_valid(row)),
  suggestions     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(suggestions)),
  approved_title  TEXT,
  approved_at     TEXT,
  approved_source TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE (series_id, date),
  CHECK ((approved_title IS NULL) = (approved_at IS NULL))
);

-- One day on one destination. `produced` maps each artifact to {value, origin: created | found}.
CREATE TABLE tasks (
  id          INTEGER PRIMARY KEY,
  day_id      INTEGER NOT NULL REFERENCES days (id),
  destination TEXT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN
                ('waiting', 'ready', 'running', 'scheduled', 'verified', 'failed', 'needs_login', 'pulled')),
  mode        TEXT NOT NULL CHECK (mode IN ('plan', 'drafts', 'live')),
  attempts    INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  produced    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(produced)),
  error       TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT,
  UNIQUE (day_id, destination)
);
CREATE INDEX tasks_state ON tasks (state);

-- Every transition of every entity, in order.
CREATE TABLE events (
  id         INTEGER PRIMARY KEY,
  entity     TEXT NOT NULL,
  entity_id  TEXT NOT NULL,
  from_state TEXT,
  to_state   TEXT,
  reason     TEXT,
  data       TEXT CHECK (data IS NULL OR json_valid(data)),
  at         TEXT NOT NULL
);
CREATE INDEX events_entity ON events (entity, entity_id, id);

-- API units spent per service and quota day (YouTube's day is America/Los_Angeles, D7).
CREATE TABLE quota (
  service    TEXT NOT NULL,
  quota_day  TEXT NOT NULL,
  units      INTEGER NOT NULL DEFAULT 0 CHECK (units >= 0),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (service, quota_day)
);

-- Stock photos used, for the no-repeat windows (§S13; legacy image_history).
CREATE TABLE photos (
  id           INTEGER PRIMARY KEY,
  series_id    TEXT REFERENCES series (id),
  source       TEXT NOT NULL,
  photo_id     TEXT NOT NULL,
  topic        TEXT,
  used_on      TEXT NOT NULL,
  photographer TEXT,
  url          TEXT,
  recorded_at  TEXT NOT NULL
);
CREATE INDEX photos_photo ON photos (source, photo_id);
CREATE INDEX photos_topic ON photos (topic, used_on);

CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL CHECK (json_valid(value)),
  updated_at TEXT NOT NULL
);

-- One row per held lock. The writer lock (§S14) is held by whoever runs tasks.
CREATE TABLE locks (
  name         TEXT PRIMARY KEY,
  pid          INTEGER NOT NULL,
  host         TEXT NOT NULL,
  purpose      TEXT,
  acquired_at  TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL
);
