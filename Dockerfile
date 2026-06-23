# Builds the prview server image. Primarily exercised by CI to prove the build
# (and as a reproducible runtime). Note: live PR review shells out to the host's
# `gh` and `claude` CLIs, which are not in this image — for real use, run prview
# on your host (`uv run prview`). See README.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY prview ./prview

RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["uv", "run", "--no-dev", "prview"]
