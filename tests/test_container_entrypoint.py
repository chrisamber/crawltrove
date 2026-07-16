from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def test_container_repairs_volume_then_runs_as_pwuser(monkeypatch, tmp_path):
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["python", "-m", "app.container_entrypoint"]' in dockerfile
    assert "\nUSER pwuser\n" not in dockerfile

    from app import container_entrypoint

    data_dir = tmp_path / "data"
    research_dir = data_dir / "research"
    checkpoint = research_dir / "existing.json"
    research_dir.mkdir(parents=True)
    checkpoint.write_text("{}", encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_file = outside_dir / "leave-alone.json"
    outside_dir.mkdir()
    outside_file.write_text("{}", encoding="utf-8")
    outside_link = data_dir / "outside-link"
    outside_link.symlink_to(outside_dir, target_is_directory=True)
    calls = []
    user = SimpleNamespace(pw_name="pwuser", pw_uid=1000, pw_gid=1000)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setattr(container_entrypoint.os, "geteuid", lambda: 0)
    monkeypatch.setattr(container_entrypoint.pwd, "getpwnam", lambda _name: user)
    monkeypatch.setattr(
        container_entrypoint.os, "chown",
        lambda path, uid, gid, **kwargs: calls.append(
            ("chown", Path(path), uid, gid, kwargs)
        ),
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
    chowns = calls[:4]
    assert {call[1] for call in chowns} == {
        data_dir, research_dir, checkpoint, outside_link,
    }
    assert all(
        call[2:] == (1000, 1000, {"follow_symlinks": False})
        for call in chowns
    )
    assert outside_file not in {call[1] for call in chowns}
    assert calls[4:] == [
        ("initgroups", "pwuser", 1000),
        ("setgid", 1000),
        ("setuid", 1000),
        ("execvp", "uvicorn", ["uvicorn", "app.main:app"]),
    ]
