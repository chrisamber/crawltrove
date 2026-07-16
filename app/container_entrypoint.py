"""Prepare persistent storage, then run the container command as pwuser."""
import os
import pwd
import sys
from pathlib import Path


def main(command=None) -> None:
    command = list(sys.argv[1:] if command is None else command)
    if not command:
        raise SystemExit("container command required")

    if os.geteuid() == 0:
        user = pwd.getpwnam("pwuser")
        data_dir = Path(os.environ.get("DATA_DIR", "/workspace/data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        # ponytail: only the mount root needs repair; recurse if a future
        # root-running release creates nested files.
        os.chown(data_dir, user.pw_uid, user.pw_gid)
        os.initgroups(user.pw_name, user.pw_gid)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)

    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
