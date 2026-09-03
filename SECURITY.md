# Security

This repository is a reference bridge for one private WeCom installation. It receives authenticated callbacks, stores message content, and can invoke an agent with server permissions. Treat the callback keys, application Secret, access tokens, message archive, and queue as sensitive.

## Safe deployment baseline

- Keep real values in a mode-`600` environment file. Never put them in source, screenshots, issues, CI logs, or shell history.
- Bind the callback process to `127.0.0.1` and expose it through an authenticated reverse proxy or Cloudflare Tunnel. Do not publish the local port directly.
- Keep callback signature verification, PKCS7 validation, CorpID validation, exact AgentID matching, and hardened XML parsing enabled.
- If trusted-domain verification is required, set `WECOM_VERIFY_FILE` to the exact downloaded `WW_verify_*.txt`; do not add a generic static-file server to the callback process.
- Set `WECOM_ALLOWED_USER_IDS`. The reply path refuses every application target outside that allowlist.
- For WeChat Customer Service, set `WECOM_KF_EXTERNAL_USER_ID` before production use. Leaving it empty deliberately binds the first authenticated customer who sends a message.
- Keep `/var/lib/wecom-agent`, its SQLite database, queue, raw messages, media, and persisted pending replies private to the service account (`700` directories, `600` files). Set `WECOM_MAX_MEDIA_BYTES` to a limit the host can safely retain.
- Give the agent only the filesystem and command permissions it actually needs. `AGENT_COMMAND` receives the complete route envelope, including sender identifiers and local media paths, so treat its stdin as sensitive. The channel bridge does not make a high-privilege agent safe by itself.
- Add the self-built application to WeChat Customer Service's “callable applications” list and authorize only the intended customer-service accounts before using that application's Secret for `kf/*` APIs.
- Cache access tokens and respect WeCom rate limits. Customer-service reply budgets are cumulative after the last customer message; they are persisted in SQLite and reset only by a new customer message.
- Use a dedicated Linux user and the supplied systemd hardening. Review `ReadWritePaths` before deployment.

## Secret exposure

If a Secret, callback Token, EncodingAESKey, tunnel credential, or access token is exposed:

1. Revoke or rotate it in the WeCom or tunnel administration console.
2. Replace the value in `/etc/wecom-agent.env`.
3. Restart the affected bridge service.
4. Remove the secret from Git history rather than only deleting it in a later commit.
5. Review outbound messages and callback logs for unexpected activity.

## Reporting a vulnerability

Use GitHub private vulnerability reporting or a private maintainer contact. Do not post working credentials, customer identifiers, message bodies, or exploit details in a public issue.
