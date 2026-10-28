# ADR 0002: Ingress and Automated TLS

## Status
Accepted

## Context
A community infrastructure deployment must expose HTTP/HTTPS endpoints for:
1. Public landing page and service directory.
2. Web client (Element Web).
3. Matrix homeserver client-server API (`/_matrix/client/...`) and well-known delegation (`/.well-known/matrix/...`).
4. Matrix federation API (`:8448` or `:443` with SRV/well-known delegation).
5. Canonical announcement feed (HTTP / RSS / JSON).

TLS must be automated so non-specialist volunteer operators do not have to manually obtain, configure, or renew certificates with Let's Encrypt / ACME.

Candidate reverse proxies:
1. **Caddy** (Go):
   - Automatic HTTPS by default via Let's Encrypt and ZeroSSL.
   - Clean, declarative `Caddyfile` syntax that is easy to generate and inspect.
   - Built-in HTTP/2 and HTTP/3 support.
   - Safe defaults and low operational maintenance overhead.
2. **Nginx + Certbot**:
   - Highly flexible, but requires external cron/systemd certbot renewal scripts, separate container coordination, and complex configuration files.
3. **Traefik**:
   - Dynamic label-based routing, but higher configuration complexity for non-specialist operators.

## Decision
We select **Caddy** (`caddy:2.8.4-alpine` pinned version) as the public ingress reverse proxy.
- Automatically handles ACME TLS issuance and renewal.
- Configured via a single generated `Caddyfile`.
- Internal services communicate over private Docker networks; only Caddy binds ports 80 and 443 (and 8448 if federation port is directly mapped).

## Consequences
- Zero-touch certificate management for valid DNS setups.
- Clear error logs when DNS propagation is pending or failing.
- Simple, transparent reverse-proxy rules for volunteer operators.
