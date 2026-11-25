-- Additive lifecycle audit only; no account/quota/history changes.
CREATE TABLE IF NOT EXISTS model_service_audit (
    id INTEGER PRIMARY KEY,
    occurred REAL NOT NULL,
    actor TEXT NOT NULL,
    operation TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
