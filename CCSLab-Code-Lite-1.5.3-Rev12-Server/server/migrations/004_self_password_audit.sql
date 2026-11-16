CREATE TABLE IF NOT EXISTS self_password_audit (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    changed_at INTEGER NOT NULL
);
