# ADR 0001: Matrix Homeserver Selection

## Status
Accepted

## Context
The community stack requires an independent, federated, open-standards messaging backend. Matrix was selected in the design baseline as the messaging protocol. We must select the specific homeserver implementation to package and deploy for community operators.

Candidate implementations:
1. **Matrix Synapse** (Python/Rust, upstream matrixdotorg/synapse):
   - Mature, official reference implementation.
   - Comprehensive documentation, wide community support, and extensive testing with Element Web.
   - Supports PostgreSQL for robust production operation.
   - Feature-complete support for federation, moderation, media repository, and administration APIs.
2. **Dendrite** (Go):
   - Lighter resource usage, but still beta with incomplete features and lower ecosystem usage.
3. **Conduit / Conduwuit** (Rust):
   - Fast, low memory usage, but smaller maintenance community and varying federation compatibility.

## Decision
We select **Matrix Synapse** with **PostgreSQL** as the primary supported homeserver for the community stack.
- Deployed via the official upstream container image `matrixdotorg/synapse:v1.119.0` (pinned version).
- PostgreSQL container `postgres:16-alpine` (pinned version) on an isolated Docker network.
- Registration will be closed (`enable_registration: false`) by default to prevent spam/abuse, managed via admin token or explicit invitation.

## Consequences
- Requires moderate memory (~1-2 GB RAM minimum recommended for the host).
- Proven stability, backup tooling (pg_dump), and guaranteed compatibility with Element Web and `mautrix-whatsapp`.
- Easy migration and restore paths documented by the Matrix community.
