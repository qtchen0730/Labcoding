-- Applied by ModelControl after the account migration. Idempotent on startup.
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS model_confirmations (
    digest TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    expires REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_configuration_audit (
    id INTEGER PRIMARY KEY,
    occurred REAL NOT NULL,
    actor TEXT NOT NULL,
    result TEXT NOT NULL,
    old_context INTEGER NOT NULL,
    old_slots INTEGER NOT NULL,
    new_context INTEGER NOT NULL,
    new_slots INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
COMMIT;
