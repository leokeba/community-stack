FROM ghcr.io/astral-sh/uv:0.9.8@sha256:08f409e1d53e77dfb5b65c788491f8ca70fe1d2d459f41c89afa2fcbef998abe AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY cli ./cli
COPY config ./config
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/cli \
    PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "community_stack.admin.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
