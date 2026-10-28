# Security considerations

Do not publish runtime `data/`, `config/` or `.env`. Administrators should keep their controller IPv4 allowlist narrow, pin the HTTPS API certificate, and keep the API secret URL private.

The API uses bearer tokens in URL paths to retain Outline API client compatibility. Never log these paths; restrict access to the authorized controller and consider using a private WireGuard/Tailscale transport for an additional layer.

Dynamic `ssconf://` URLs contain secrets and must not be publicized. A user can download the configuration when they know the unguessable link. TLS and cloud security rules must not issue browser challenges to Outline clients.

No untrusted source should be allowed to modify `data/` or the Docker socket. Running Docker containers does not by itself imply isolation against a compromised host root user.

This repository is not a substitute for an independent security review. Do not deploy on an active system without a migration and rollback plan.
