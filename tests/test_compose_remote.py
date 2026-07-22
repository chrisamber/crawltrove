from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_default_compose_has_remote_capable_durable_components():
    services = _compose()["services"]

    assert {
        "db",
        "minio",
        "minio-init",
        "worker-init",
        "crawltrove",
        "worker-standard",
        "worker-browser",
    } <= set(services)
    assert services["worker-standard"]["build"]["target"] == "worker-standard"
    assert services["worker-browser"]["build"]["target"] == "worker-browser"


def test_compose_uses_s3_and_explicit_private_network_db_override():
    services = _compose()["services"]
    core_environment = services["crawltrove"]["environment"]
    standard_environment = services["worker-standard"]["environment"]
    browser_environment = services["worker-browser"]["environment"]

    assert "ARTIFACT_STORE_BACKEND=s3" in core_environment
    assert "WORKER_ALLOW_INSECURE_DB=true" in standard_environment
    assert "WORKER_ALLOW_INSECURE_DB=true" in browser_environment
    assert "ports" not in services["minio"]
    assert services["minio-init"]["read_only"] is True
    assert "DATABASE_URL=postgresql://crawltrove:crawltrove@db:5432/crawltrove" in core_environment


def test_default_stack_bootstraps_private_enrollment_bundles_before_workers():
    services = _compose()["services"]

    bootstrap = services["worker-init"]
    assert bootstrap["entrypoint"][-1].endswith("bootstrap_local_workers.py")
    assert "CRAWLTROVE_LOCAL_BOOTSTRAP=true" in bootstrap["environment"]
    assert bootstrap["depends_on"]["crawltrove"]["condition"] == "service_healthy"
    assert bootstrap["volumes"] == ["worker_enrollments:/enrollments"]
    assert bootstrap["cap_add"] == ["CHOWN"]

    for name in ("worker-standard", "worker-browser"):
        worker = services[name]
        assert "profiles" not in worker
        assert worker["depends_on"]["worker-init"]["condition"] == "service_completed_successfully"
        enrollment = worker["volumes"][0]
        assert enrollment["type"] == "volume"
        assert enrollment["source"] == "worker_enrollments"
        assert enrollment["target"] == "/run/crawltrove-workers"
        assert enrollment["read_only"] is True

    bootstrap_source = (ROOT / "scripts" / "bootstrap_local_workers.py").read_text()
    assert '("captcha", ("browser", "captcha", "http"))' in bootstrap_source


def test_worker_services_are_isolated_and_health_checked():
    services = _compose()["services"]

    for name in ("worker-standard", "worker-browser"):
        worker = services[name]
        assert worker["read_only"] is True
        assert "no-new-privileges:true" in worker["security_opt"]
        assert worker["healthcheck"]["test"]
        assert "ports" not in worker

    assert services["worker-browser"]["cap_add"] == ["SYS_CHROOT"]
    assert "seccomp=./seccomp_profile.json" in services["worker-browser"]["security_opt"]


def test_owned_components_are_opt_in_profiles_without_host_ports():
    services = _compose()["services"]

    assert services["egress-agent"]["profiles"] == ["egress"]
    assert services["worker-captcha"]["profiles"] == ["captcha"]
    assert services["worker-captcha"]["build"]["target"] == "worker-captcha"
    assert services["egress-agent"]["build"]["target"] == "egress-agent"

    for name in ("egress-agent", "worker-captcha"):
        service = services[name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "ports" not in service
        assert "no-new-privileges:true" in service["security_opt"]

    egress_mount = services["egress-agent"]["volumes"][0]
    assert egress_mount["target"] == "/run/crawltrove-egress"
    assert egress_mount["read_only"] is True
    assert services["egress-agent"]["expose"] == ["9443"]
    assert services["worker-captcha"]["environment"][-1:] == [
        "CAPTCHA_AUTHORIZED_DOMAINS=${CAPTCHA_AUTHORIZED_DOMAINS:-}",
    ]
    assert services["worker-captcha"]["depends_on"]["worker-init"]["condition"] == "service_completed_successfully"
    assert "CAPTCHA_DB_PASSWORD=${CAPTCHA_DB_PASSWORD:-crawltrove-captcha-db-local}" in services["worker-init"]["environment"]
    assert "CAPTCHA_S3_ACCESS_KEY=${CAPTCHA_S3_ACCESS_KEY:-crawltrove-captcha}" in services["minio-init"]["environment"]


def test_dockerfile_declares_all_runtime_targets():
    source = (ROOT / "Dockerfile").read_text()

    assert " AS core" in source
    assert " AS worker-standard" in source
    assert " AS worker-browser" in source
    assert " AS worker-captcha" in source
    assert " AS egress-agent" in source
    assert "python:3.11-slim AS worker-standard" in source
    assert "python:3.11-slim AS egress-agent" in source
    assert "sync_playwright" in source


def test_minio_init_scopes_lifecycle_and_worker_policies():
    source = (ROOT / "scripts" / "minio_init.sh").read_text()

    assert '"workers/$worker_id/*"' in source
    assert '"workers/$STANDARD_WORKER_ID/tmp/"' in source
    assert '"workers/$CAPTCHA_WORKER_ID/tmp/"' in source
    assert 'configure_worker "$CAPTCHA_WORKER_ID"' in source
    assert "mc anonymous set none" in source


def test_minio_enables_local_server_side_encryption():
    environment = _compose()["services"]["minio"]["environment"]

    assert any(value.startswith("MINIO_KMS_SECRET_KEY=") for value in environment)
