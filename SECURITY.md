# Security Policy

## Reporting Security Issues

We take the security of community infrastructure and communications seriously. If you discover a vulnerability or security flaw in the Community Communications Stack, please report it responsibly.

**Please do NOT report security vulnerabilities via public GitHub issues.**

Instead, please email security reports to:
`security@community-stack.local` (or file a private GitHub Security Advisory).

Include the following in your report:
- A description of the vulnerability and its potential impact.
- Clear reproduction steps or proof-of-concept.
- Any affected components (CLI, Ansible roles, Docker services, or upstream packages).
- Suggested mitigations, if known.

We will acknowledge receipt within 48 hours and work to provide a timely fix.

## Security Principles & Commitments

1. **No Hardcoded Secrets:** No secrets, test tokens, or live passwords may ever be committed to this repository.
2. **Redaction by Default:** The operator CLI and diagnostic tools (`community-stack doctor`, `logs`, etc.) automatically redact passwords, API tokens, session keys, and sensitive environment variables before displaying or outputting them.
3. **Upstream Image Pinning:** Production Docker container images are pinned to reviewed, specific version tags or immutable digests. The use of `latest` in deployment profiles is prohibited.
4. **Least Privilege & Isolation:** Database containers are placed on isolated internal Docker networks with no exposed host ports. Services run with non-root container users wherever supported by upstream.
5. **Client-Side Backup Encryption:** With `operations.backups.encryption: restic` (the default for new configurations), snapshots are encrypted on the host by restic before they are stored locally or sent off-site. Configurations without that setting write owner-only (`0600`) archives that are **not encrypted**, and `community-stack check` warns about them.
