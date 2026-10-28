# Changelog

All notable changes to the Community Communications Stack will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Milestone 0: Reconnaissance and Architecture Decisions:**
  - Initial threat model (`docs/threat-model.md`).
  - Architecture documentation (`docs/architecture.md`).
  - Architecture Decision Records (`docs/decisions/0001` through `0006`).
- **Milestone 1: Project Foundation:**
  - Python packaging with `pyproject.toml` and `uv` support.
  - JSON Schema specification (`config/schema.json`) for `community-stack.yaml`.
  - Operator CLI (`community-stack`) with `init`, `check`, and `plan` commands.
  - Secret management with strict `.env` loading, permission checks, and automatic redaction filter.
  - Test suite with pytest and code formatting/linting with ruff.
  - GitHub Actions CI workflow.
- **Milestone 2: Minimal Deployable Infrastructure:**
  - Modular Docker Compose services for Caddy proxy (`services/proxy`), Landing page (`services/landing`), Matrix Synapse with PostgreSQL (`services/matrix`), and Element Web client (`services/element`).
  - Network segmentation isolating database traffic (`matrix_internal`) from public ingress (`proxy_net`).
  - Ansible host preparation roles and master deployment playbook (`ansible/`).
  - Implemented `community-stack install`, `community-stack status`, and `community-stack logs` CLI commands.
  - End-to-end deployment documentation and firewall/DNS guide (`docs/deployment.md`).
  - Integration test suite for container service configurations, network policies, and healthchecks.
