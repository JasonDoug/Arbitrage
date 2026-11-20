FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY pyproject.toml ./
COPY predictarb ./predictarb
RUN pip install .

RUN useradd --create-home app
USER app

# Overridden per service in docker-compose.yml
CMD ["predictarb-api"]
