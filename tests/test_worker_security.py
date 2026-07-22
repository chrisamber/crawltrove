import json
import ssl

import pytest

from app.worker_config import WorkerConfig
from tests.conftest import requires_db


def test_remote_bundle_requires_verified_tls(tmp_path):
    ca = tmp_path / "ca.pem"
    cert = tmp_path / "edge-1.pem"
    key = tmp_path / "edge-1-key.pem"
    # The paths must exist before SSLContext loads the bundle.  Empty files
    # deliberately prove that configuration does not silently fall back to
    # plaintext when the enrolled certificates are invalid.
    for path in (ca, cert, key):
        path.write_text("not a certificate")
    key.chmod(0o600)
    bundle = tmp_path / "worker.json"
    bundle.write_text(json.dumps({
        "workerId": "edge-1",
        "databaseUrl": "postgresql://ct_worker_edge_1@db.example/crawltrove",
        "caCert": str(ca),
        "clientCert": str(cert),
        "clientKey": str(key),
        "capabilities": ["http"],
        "protocolVersion": 1,
        "artifactPrefix": "workers/edge-1/",
    }))
    bundle.chmod(0o600)
    with pytest.raises(ValueError, match="TLS"):
        WorkerConfig.from_file(bundle)


def test_plaintext_requires_explicit_override(monkeypatch):
    monkeypatch.delenv("WORKER_ALLOW_INSECURE_DB", raising=False)
    with pytest.raises(ValueError, match="verified TLS"):
        WorkerConfig.from_mapping({
            "workerId": "local",
            "databaseUrl": "postgresql://local@db/crawltrove",
            "capabilities": ["http"],
        })


def test_plaintext_override_reports_degraded(monkeypatch):
    monkeypatch.setenv("WORKER_ALLOW_INSECURE_DB", "true")
    config = WorkerConfig.from_mapping({
        "workerId": "local",
        "databaseUrl": "postgresql://local@db/crawltrove",
        "capabilities": ["http"],
        "protocolVersion": 1,
        "artifactPrefix": "workers/local/",
    })
    assert config.ssl_context is None
    assert config.security_state == "degraded"


def test_bundle_uses_verified_tls(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKER_ALLOW_INSECURE_DB", raising=False)
    ca = tmp_path / "ca.pem"
    cert = tmp_path / "worker.pem"
    key = tmp_path / "worker-key.pem"
    # Avoid certificate generation in this unit test; TLS construction is
    # isolated so the required policy is observable without external tools.
    ca.write_text("ca")
    cert.write_text("cert")
    key.write_text("key")
    key.chmod(0o600)
    captured = {}

    class Context:
        check_hostname = False
        verify_mode = ssl.CERT_NONE

        def load_cert_chain(self, certfile, keyfile):
            captured["chain"] = (certfile, keyfile)

    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: captured.setdefault("context", Context()))
    config = WorkerConfig.from_mapping({
        "workerId": "edge-1", "databaseUrl": "postgresql://edge@db/crawl",
        "caCert": str(ca), "clientCert": str(cert), "clientKey": str(key),
        "capabilities": ["http"], "protocolVersion": 1,
        "artifactPrefix": "workers/edge-1/",
    })
    assert config.ssl_context.check_hostname is True
    assert config.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert captured["chain"] == (str(cert), str(key))


@requires_db
async def test_worker_role_has_no_table_access(db):
    async with db.acquire() as conn:
        direct = await conn.fetchval(
            "SELECT has_table_privilege('crawltrove_worker','crawl_tasks','SELECT')"
        )
        execute = await conn.fetchval(
            "SELECT has_function_privilege('crawltrove_worker',"
            "'worker_api.heartbeat(uuid,uuid)', 'EXECUTE')"
        )
    assert direct is False
    assert execute is True


@requires_db
async def test_worker_identity_is_derived_from_current_user(db):
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT proconfig FROM pg_proc WHERE proname = 'claim' "
            "AND pronamespace = 'worker_api'::regnamespace"
        )
    assert "search_path=pg_catalog,public" in [
        setting.replace(" ", "") for setting in row["proconfig"]
    ]


@requires_db
async def test_remote_complete_requires_worker_owned_artifact_prefix(db):
    from app.crawl import repository
    from app.crawl.config import CrawlConfig

    job_id = await repository.submit_job(CrawlConfig(url="https://example.com"))
    async with db.acquire() as conn:
        role = await conn.fetchval("SELECT session_user")
        await conn.execute(
            """INSERT INTO workers (id, db_role, capabilities, protocol_version, state, artifact_bucket, artifact_prefix)
               VALUES ('security-test', $1, ARRAY['http'], 1, 'active', 'bucket', 'workers/security-test/')
               ON CONFLICT (id) DO UPDATE SET db_role = EXCLUDED.db_role, state = 'active'""",
            role,
        )
        claim = await conn.fetchrow("SELECT * FROM worker_api.claim(ARRAY['http']::TEXT[])")
        assert claim is not None and claim["job_id"] == job_id
        denied = await conn.fetchval(
            "SELECT worker_api.complete($1,$2,$3::jsonb,'{}'::jsonb)",
            claim["id"], claim["lease_token"],
            json.dumps({"uri": "s3://bucket/workers/other/x.md", "size": 1,
                        "sha256": "0" * 64, "media_type": "text/markdown"}),
        )
        wrong_bucket = await conn.fetchval(
            "SELECT worker_api.complete($1,$2,$3::jsonb,'{}'::jsonb)",
            claim["id"], claim["lease_token"],
            json.dumps({"uri": "s3://other/workers/security-test/x.md", "size": 1,
                        "sha256": "0" * 64, "media_type": "text/markdown"}),
        )
    assert denied is False
    assert wrong_bucket is False


@requires_db
async def test_worker_api_has_no_stub_functions(db):
    async with db.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM pg_proc WHERE pronamespace = 'worker_api'::regnamespace "
            "AND proname LIKE '%stub%'"
        )
    assert count == 0
