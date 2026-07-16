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
        # Existing Railway volumes may contain root-owned descendants.
        # ponytail: O(n) startup scan; use a migration marker if volume size
        # makes this meaningfully slow.
        os.chown(data_dir, user.pw_uid, user.pw_gid, follow_symlinks=False)
        for root, dirs, files in os.walk(data_dir):
            for name in dirs + files:
                os.chown(
                    Path(root, name),
                    user.pw_uid,
                    user.pw_gid,
                    follow_symlinks=False,
                )
        os.environ["HOME"] = user.pw_dir
        os.initgroups(user.pw_name, user.pw_gid)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)

    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
