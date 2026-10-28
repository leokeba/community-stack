# ADR 0003: Operator CLI and Configuration Management

## Status
Accepted

## Context
Community operators need a single, consistent CLI tool (`community-stack`) to manage infrastructure lifecycle without having to memorize raw Docker Compose or Ansible commands. The configuration must be human-readable, machine-validated, and strictly separate non-sensitive configuration from secrets.

## Decision
1. **Language and packaging:**
   - Python 3.10+ package using modern standard `pyproject.toml` (PEP 517/621).
   - CLI implemented using Python's standard `argparse` (or lightweight `click`) with zero unnecessary dependencies for core commands, ensuring resilience.
2. **Configuration file:**
   - Human-readable YAML configuration file (`community-stack.yaml` or custom path).
   - Strict validation against a versioned JSON Schema (`config/schema.json`) using `jsonschema`.
   - Comprehensive default values and actionable validation error messages.
3. **Secrets handling:**
   - Secrets are strictly separated into an environment file (`.env` or `.community-stack.env`) with `0600` permissions.
   - Secrets are NEVER committed to version control. An annotated `.env.example` provides templates.
   - Built-in redaction filter for all CLI outputs, logs, and diagnostic reports (`community-stack doctor`).

## Consequences
- Single, predictable interface for all administrative operations.
- Misconfigurations are caught early before touching live infrastructure.
- Safe secret lifecycle prevents accidental credential leaks.
