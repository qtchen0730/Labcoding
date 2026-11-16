ALTER TABLE sessions ADD COLUMN admin_code_hash BLOB;

CREATE TABLE IF NOT EXISTS credential_audit (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    target_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL CHECK (action IN ('password_reset', 'authorization_code_changed')),
    changed_at INTEGER NOT NULL
);
