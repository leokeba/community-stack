# Contributing to Community Communications Stack

Thank you for contributing to the Community Communications Stack! We welcome contributions that improve the resilience, simplicity, security, and usability of self-hosted community infrastructure.

## Core Design Principles

Before proposing changes, please review [docs/DESIGN.md](docs/DESIGN.md) and [docs/architecture.md](docs/architecture.md):
1. **Resilience over feature completeness:** A smaller system that can be recovered reliably is preferable to a complex system that cannot.
2. **Operational simplicity:** Volunteer non-specialists must be able to deploy, diagnose, and recover the stack.
3. **Upstream reuse:** Prefer established upstream software (Synapse, Element Web, Caddy, Restic, mautrix-whatsapp) over custom implementations.
4. **Safety by default:** Never silently weaken TLS, permissions, or security boundaries.

## Development Workflow

This project uses `uv` for Python virtual environments, dependency resolution, and tool execution.

### Prerequisites
- Python 3.10+
- `uv` installed ([docs.astral.sh/uv](https://docs.astral.sh/uv/))
- Docker and Docker Compose (v2)
- Git

### Setting Up Development Environment

```bash
# Clone the repository
git clone https://github.com/your-org/community-stack.git
cd community-stack

# Create virtual environment and install all dependencies (including dev)
uv venv --python 3.12
uv pip install -e ".[dev]"

# Run tests
uv run pytest

# Run linter & formatter checks
uv run ruff check .
uv run ruff format --check .
```

### Making Changes
- Write unit tests for new CLI functionality, schema validations, and secret handling.
- When introducing a major architectural or dependency decision, record an Architecture Decision Record in `docs/decisions/`.
- Ensure all tests pass and `ruff` reports zero warnings.
