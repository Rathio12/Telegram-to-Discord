# Security Policy

## Reporting a vulnerability

Do not open a public issue for leaked credentials or security vulnerabilities. Contact the project owner privately with a clear description and reproduction steps.

## Credential safety

If a Telegram API hash, session file, Discord webhook URL, or other secret is exposed:

1. Revoke or rotate it immediately.
2. Remove it from the working tree and Git history if it was committed.
3. Replace it only in the local `.env` file.
4. Check logs and public artifacts for further exposure.

The project ignores local secrets, sessions, databases, and runtime files through `.gitignore`.
