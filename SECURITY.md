# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| latest  | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly.

**Do NOT open a public GitHub issue.**

Instead, email hello@mtrmagickey.com

You should receive an acknowledgement within 48 hours. We will work with you to understand the scope, assess the risk, and coordinate a fix before public disclosure.

## Security Practices

- Secrets are loaded from environment variables / OS keyring — never hardcoded
- Admin console uses token-based authentication with `hmac.compare_digest` (constant-time)
- CSRF protection via custom header requirement on state-changing requests
- Rate limiting on the authentication endpoint
- SQL queries use parameterized values
- All outbound HTTPS connections use default TLS certificate verification
- Docker image runs as non-root user (`appuser`)
- Dependencies are pinned in `LeisureLLM/requirements.lock`

## Credential Rotation

If credentials are compromised, rotate all affected keys immediately (Discord, OpenAI, Tavily, etc.) and update `LeisureLLM/.env`.
