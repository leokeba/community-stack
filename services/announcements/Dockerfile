FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY cli ./cli
COPY config ./config
RUN pip install --no-cache-dir .

ENV PYTHONPATH=/app/cli \
    PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "community_stack.admin.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
