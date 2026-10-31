-- AI Software Engineering Agent: local analysis history (schema version 1)
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    analyzed_at      TEXT    NOT NULL,
    archive_name     TEXT    NOT NULL,
    archive_sha256   TEXT,
    tool_version     TEXT,
    duration_seconds REAL,
    file_count       INTEGER,
    total_size       INTEGER,
    finding_count    INTEGER,
    high_count       INTEGER,   -- critical + high
    medium_count     INTEGER,
    low_count        INTEGER,   -- low + info
    languages        TEXT,
    html_path        TEXT,
    json_path        TEXT,
    result_blob      BLOB       -- zlib-compressed JSON of the full result (redacted); NULL when too large
);
CREATE INDEX IF NOT EXISTS idx_analyses_name ON analyses(archive_name);

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    finding_id  TEXT,
    rule_id     TEXT,
    severity    TEXT,
    category    TEXT,
    file        TEXT,
    line        INTEGER,
    title       TEXT,
    evidence    TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_analysis ON findings(analysis_id);
