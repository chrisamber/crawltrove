# Security policy

## Reporting a vulnerability

Please do not open a public issue for a vulnerability. Use this repository's
GitHub **Security** tab to submit a private vulnerability report. Include the
affected version, impact, reproduction steps, and any suggested mitigation.

Maintainers will acknowledge a report as soon as practical, validate the issue,
and coordinate a fix before public disclosure.

## Supported version

Security fixes target the latest commit on `main`. Older commits and private
forks are not separately supported.

## Deployment boundaries

CrawlTrove fetches attacker-controlled web content and should be treated as a
network-sensitive service.

- Docker Compose binds to loopback by default.
- Configure `APP_PASSWORD` and/or `API_KEYS` before exposing it to a network.
- CORS is disabled unless `CORS_ORIGINS` is explicitly set.
- Private, loopback, link-local, and non-public targets are blocked in the
  application by default.
- The Compose runtime launches Chromium as a non-root user with its sandbox
  enabled and serves captured HTML only as plain-text attachments.
- Remote workers must use enrolled least-privilege PostgreSQL identities,
  verified TLS client credentials, and worker-scoped S3 prefixes. The local
  Compose insecure-database override is not suitable for production.
- Persisted browser profiles require `SESSION_ENCRYPTION_KEY`; rotate with
  `SESSION_ENCRYPTION_PREVIOUS_KEYS` and protect both as production secrets.
- Live-session bearer tokens are scoped, single-use, short-lived credentials.
  Do not log or forward them outside the operator session.
- `ALLOW_PRIVATE_NETWORKS=true` removes that target restriction and must only be
  used by a trusted operator.
- This project is not presented as a hardened multi-tenant scraping SaaS.

For an internet-facing or multi-user deployment, add an outbound network policy
that denies non-public IPv4 and IPv6 ranges. The HTTP tier pins validated DNS
answers, but Chromium performs its own DNS lookup and retains a DNS-rebinding
race that application-level routing cannot fully remove.

Keep credentials in environment variables or an ignored `.env`; never commit
them. Rotate any credential that appears in logs, terminal output, an issue, or
a pull request.
