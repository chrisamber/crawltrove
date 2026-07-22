-- Core-only encrypted reusable acquisition profiles.

CREATE TABLE IF NOT EXISTS session_profiles (
    id UUID PRIMARY KEY,
    name TEXT UNIQUE NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    backend TEXT NOT NULL,
    pool_id TEXT NOT NULL,
    allowed_domains TEXT[] NOT NULL,
    ciphertext BYTEA NOT NULL,
    nonce BYTEA NOT NULL CHECK (octet_length(nonce) = 12),
    key_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

REVOKE ALL ON session_profiles FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawltrove_worker') THEN
        REVOKE ALL ON session_profiles FROM crawltrove_worker;
    END IF;
END $$;
