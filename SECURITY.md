# Security

## Reporting

Please report security issues privately to the repository owner (GitHub Security Advisories on this repo, or a private message to @SemoryX). Do not open a public issue with credentials or personal resumes attached.

## Do not commit

- `webapp/db.conf` / `jobscraper/db.conf` (passwords)
- LLM API keys (stored in MySQL `aijs_settings` — keep the DB local)
- `jobscraper/state/` (logged-in Chrome profiles / cookies)
- Resumes and other files under `documents/`

## Local binding

The workbench defaults to `127.0.0.1`. Do not expose it to the public internet without authentication.
