from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def test_container_repairs_volume_then_runs_as_pwuser(monkeypatch, tmp_path):
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["python", "-m", "app.container_entrypoint"]' in dockerfile
    assert "\nUSER pwuser\n" not in dockerfile

    from app import container_entrypoint

    data_dir = tmp_path / "data"
    calls = []
    user = SimpleNamespace(pw_name="pwuser", pw_uid=1000, pw_gid=1000)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setattr(container_entrypoint.os, "geteuid", lambda: 0)
    monkeypatch.setattr(container_entrypoint.pwd, "getpwnam", lambda _name: user)
    monkeypatch.setattr(
        container_entrypoint.os, "chown",
        lambda path, uid, gid: calls.append(("chown", path, uid, gid)),
    )
    monkeypatch.setattr(
        container_entrypoint.os, "initgroups",
        lambda name, gid: calls.append(("initgroups", name, gid)),
    )
    monkeypatch.setattr(
        container_entrypoint.os, "setgid",
        lambda gid: calls.append(("setgid", gid)),
    )
    monkeypatch.setattr(
        container_entrypoint.os, "setuid",
        lambda uid: calls.append(("setuid", uid)),
    )
    monkeypatch.setattr(
        container_entrypoint.os, "execvp",
        lambda file, args: calls.append(("execvp", file, args)),
    )

    container_entrypoint.main(["uvicorn", "app.main:app"])

    assert data_dir.is_dir()
    assert calls == [
        ("chown", data_dir, 1000, 1000),
        ("initgroups", "pwuser", 1000),
        ("setgid", 1000),
        ("setuid", 1000),
        ("execvp", "uvicorn", ["uvicorn", "app.main:app"]),
    ]
