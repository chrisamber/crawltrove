# CrawlTrove v0.2.1

CrawlTrove v0.2.1 tightens the supported Docker Compose runtime without
changing the API or stored data.

## Highlights

- Keep Chromium in a private IPC namespace instead of sharing the host IPC
  namespace.
- Drop every Linux capability except `SYS_CHROOT`, which Chromium's sandbox
  requires, and enable `no-new-privileges`.
- Run the application code baked into the container image instead of replacing
  it with a host source bind mount.

## Upgrade notes

- Rebuild the image after updating the source checkout:

  ```bash
  docker compose up --build
  ```

- No API, configuration, database, or artifact-storage migration is required.
