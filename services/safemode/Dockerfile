FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY cli ./cli
COPY config ./config
RUN pip install --no-cache-dir .

ENV PYTHONPATH=/app/cli \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-m", "community_stack.safemode.bot", "--config", "/data/config.yaml"]
