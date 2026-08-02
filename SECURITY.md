# Security Policy

## Reporting a Vulnerability

Please **do not open a public issue** for security problems — especially leaked API keys, tokens, or cookies.

Instead, report privately via GitHub: go to the repository's **Security** tab → **Report a vulnerability** (private security advisory). You should receive an acknowledgment within a few days.

## What to report

- Leaked credentials committed to the repository (API keys, cookies, tokens)
- Vulnerabilities in the server that expose local files or user data
- Dependency vulnerabilities with a practical exploit path

## Guidelines for users

This app is **local-first**: your API keys (`.env`), cookies (`cookies.txt`), vocabulary database (`vocab.db`), and generated lessons (`output/`) stay on your machine and are excluded via `.gitignore`. To keep it that way:

- Never commit `.env`, `cookies.txt`, `vocab.db`, or downloaded media.
- If you fork this repo, double-check `git status --short --ignored` before pushing.
- If you accidentally commit a secret, rotate it immediately — removing it from history is not enough.

## Scope notes

- The server binds to `0.0.0.0` by default so you can reach it from other devices on your LAN (e.g. a phone). Set `ELT_HOST=127.0.0.1` to restrict it to this machine. Do not expose the port to the public internet — the app has no authentication layer.
- Third-party wordlists and MDX dictionaries are user-supplied; review their licenses before redistributing.
