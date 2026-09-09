# Single image for every service this project owns: api, worker and the
# development-only source simulator. Running one image keeps the OCR toolchain,
# the Python dependency set and the extractor versions identical across
# processes, which is what makes measured extraction results comparable.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tesseract-ocr-spa is required: the synthetic corpus is Spanish, and OCR
# accuracy against the ground truth is measured with that language model.
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
      tesseract-ocr \
      tesseract-ocr-spa \
      libgl1 \
      curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies are installed from an exact lock file so that an image built
# today and an image built in six months resolve to the same tree.
COPY requirements.lock ./requirements.lock
RUN python -m pip install --require-hashes=false -r requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY corpus ./corpus
COPY evaluation ./evaluation

RUN python -m pip install --no-deps -e .

# Non-root: the object store and the report directory are the only writable
# paths, and they are created before the drop.
RUN useradd --create-home --uid 10001 iep \
 && mkdir -p /var/lib/iep/objects /var/lib/iep/reports \
 && chown -R iep:iep /var/lib/iep /app
USER iep

EXPOSE 8000
CMD ["uvicorn", "iep.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
