"""Vikunja account creation through its supported container CLI."""

from __future__ import annotations

import os
import pty
import re
import secrets
import select
import subprocess
import termios
import time
from typing import Any

from community_stack.config import ConfigError, load_config
from community_stack.redact import redact_text

CONTAINER = "community_vikunja"
CLI = "/app/vikunja/vikunja"
PASSWORD_PROMPT = re.compile(rb"(?i)password[^\r\n:]{0,100}:\s*")
CLI_TIMEOUT_SECONDS = 60


class VikunjaUserError(Exception):
    """A Vikunja account action that could not be completed."""


def _run_user_create(command: list[str], password: str) -> None:
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as exc:
        raise VikunjaUserError(f"Could not open a terminal for Vikunja: {exc}") from exc

    process: subprocess.Popen | None = None
    output = bytearray()
    prompts_sent = 0
    prompt_offset = 0
    try:
        try:
            attributes = termios.tcgetattr(slave_fd)
            attributes[3] &= ~termios.ECHO
            termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)
            process = subprocess.Popen(
                command,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        except OSError as exc:
            raise VikunjaUserError(f"Could not start Vikunja's account command: {exc}") from exc
        finally:
            os.close(slave_fd)

        deadline = time.monotonic() + CLI_TIMEOUT_SECONDS
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise VikunjaUserError("Vikunja's account command timed out.")
            readable, _, _ = select.select([master_fd], [], [], min(0.25, remaining))
            if not readable:
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                if process.poll() is not None:
                    break
                raise
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > 8192:
                trimmed = len(output) - 4096
                del output[:trimmed]
                prompt_offset = max(0, prompt_offset - trimmed)
            for match in PASSWORD_PROMPT.finditer(bytes(output), prompt_offset):
                if prompts_sent == 2:
                    break
                os.write(master_fd, (password + "\n").encode("utf-8"))
                prompts_sent += 1
                prompt_offset = match.end()

        return_code = process.wait(timeout=5)
    except VikunjaUserError:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise VikunjaUserError(f"Vikunja's account command failed: {exc}") from exc
    finally:
        os.close(master_fd)

    if return_code != 0 or prompts_sent != 2:
        detail = redact_text(output.decode("utf-8", errors="replace"), [password]).strip()
        detail = " ".join(detail.split())[-500:]
        message = detail or "the Vikunja CLI did not complete account creation."
        raise VikunjaUserError(f"Vikunja could not create the account: {message}")


def create_user(context, params: dict[str, Any]) -> dict[str, str]:
    try:
        config = load_config(context.config_path)
    except (ConfigError, OSError) as exc:
        raise VikunjaUserError(f"Could not load Community Stack configuration: {exc}") from exc
    if not config.get("services", {}).get("vikunja", {}).get("enabled", False):
        raise VikunjaUserError("Vikunja is disabled; enable it before creating accounts.")

    password = secrets.token_urlsafe(24)
    command = [
        "docker",
        "exec",
        "-it",
        CONTAINER,
        CLI,
        "user",
        "create",
        "--username",
        params["username"],
        "--email",
        params["email"],
    ]
    # Omitting --password makes Vikunja prompt twice; the temporary password stays
    # out of argv, environment variables, and persistent logs.
    _run_user_create(command, password)
    return {"user_id": params["username"], "temporary_password": password}
