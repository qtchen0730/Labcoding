CREATE TABLE IF NOT EXISTS token_quotas (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    generation INTEGER NOT NULL DEFAULT 0,
    limit_tokens INTEGER,
    used_tokens INTEGER NOT NULL DEFAULT 0,
    CHECK(limit_tokens IS NULL OR limit_tokens >= 0),
    CHECK(used_tokens >= 0)
);
CREATE TABLE IF NOT EXISTS token_requests (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    generation INTEGER NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    state TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    created_at INTEGER NOT NULL,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS token_requests_pending ON token_requests(user_id, generation, state);
CREATE TABLE IF NOT EXISTS token_quota_audit (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    generation INTEGER NOT NULL,
    limit_tokens INTEGER,
    created_at INTEGER NOT NULL
);
