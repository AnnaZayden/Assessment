FROM python:3.13-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
RUN useradd --system --no-create-home svc
USER svc
# The commit this image was built from, reported by /healthz (CI passes it; local builds report "dev").
ARG GIT_COMMIT=""
LABEL org.opencontainers.image.source="https://github.com/AnnaZayden/ours-image-service" \
      org.opencontainers.image.revision="${GIT_COMMIT}"
ENV HOST=0.0.0.0 PORT=8000 GIT_COMMIT=${GIT_COMMIT}
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD ["python", "-c", "import os, urllib.request; port = os.environ.get('IMG_PORT') or os.environ.get('PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2)"]
CMD ["python", "-m", "app"]
