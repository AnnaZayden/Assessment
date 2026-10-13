# Image Processing Service

A small Cloudinary-style service. You give it an image URL, and it returns the image resized and/or converted, plus a one-page web UI for trying it out.

```
GET /process?url=https://picsum.photos/id/1025/1600/1067&width=800&height=600&format=webp&crop=fill
```

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m app           # http://127.0.0.1:8000  (UI at /, OpenAPI docs at /docs)
.venv/bin/python -m playwright install chromium   # once, for the UI tests (or have Google Chrome installed)
.venv/bin/python -m pytest        # 248 tests, no network needed
```

Logs are JSON, one line per request, each with a request ID.

Or with Docker: `docker build -t images . && docker run -p 8000:8000 images`. The image has a `HEALTHCHECK` against `/healthz`.

## Tests and checks

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .   # lint + formatting
.venv/bin/mypy                                              # strict type checking of app/ and image_client/
.venv/bin/python -m coverage run -m pytest && .venv/bin/python -m coverage report   # fails below 100%
```

All tool settings live in `pyproject.toml`.

- **Fake internet** (`tests/conftest.py`): fake DNS and a fake HTTP upstream, so address checks, redirects, timeouts, size limits and every error code are tested without the network.
- `test_processing.py`: resizing, cropping, every input and output format, colour modes, quality, metadata and colour-profile handling.
- `test_fetch_safety.py`: address checks, redirects, deadlines, size limits, gzip bombs.
- `test_http.py`: parameters and error messages, caching headers, rate limiting, `format=auto`, request IDs, metrics, and what reaches the logs.
- `test_tls.py`: a real local HTTPS server with a test CA, checking that connecting to a pinned IP still uses SNI and verifies the certificate, never reuses connections, and ignores proxy variables.
- `test_units.py`: settings validation, the cache, the rate limiter, the real DNS lookup for spellings like `2130706433`, request coalescing, cancellation, concurrency limits and load shedding.
- `test_client.py`: the Python client SDK.
- `test_ui.py`: the web UI in headless Chromium against a live server.
- **Coverage and break-checks:** line and branch coverage of `app/` and `image_client/` is 100%. Each security check was also broken on purpose to confirm a test fails.
- **CI** (`.github/workflows/ci.yml`) runs lint, types, tests and coverage on every push and pull request, and builds the Docker image and checks that the container answers `/healthz` with the right commit. See [Deployment](#deployment-cicd) for what happens after that on `main`.

Stack: Python 3.13+, FastAPI, httpx (fetching), and Pillow (decode/encode). I wrote it in Python because that is what the machine I built it on had installed. With Node, the equivalent would be Fastify plus sharp/libvips, which is also faster at resizing.

## Deployment (CI/CD)

Every push to `main` that passes all checks is shipped automatically. A failing commit never ships, because the later jobs wait for every check to pass.

```
push ──► lint ─┐
         test ─┼─► publish ──► deploy
       docker ─┘   (ghcr.io)   (Render)
```

1. **lint, test, docker** run on every push and pull request.
2. **publish** (only on `main`) builds the image and pushes it to the GitHub Container Registry as `ghcr.io/annazayden/ours-image-service:sha-<commit>` and `:latest`. It uses the workflow's own token, so no secret is needed. The package is private while the repository is.
   ```bash
   docker run -p 8000:8000 ghcr.io/annazayden/ours-image-service:latest   # after: gh auth token | docker login ghcr.io -u <user> --password-stdin
   ```
3. **deploy** triggers a deploy on Render. It then waits until `/healthz` reports the new commit as its `version`, and smoke-tests `/limits` and a real `/process` request.

### One-time setup for the deploy job

Until this is done, the deploy job is skipped with a notice and CI stays green.

1. **Create the service.** On [Render](https://render.com), sign in with GitHub, choose **New → Blueprint** and pick this repository. [`render.yaml`](render.yaml) defines the service: Docker, free plan, health check on `/healthz`, and auto-deploy **off**, so only CI deploys.
2. **Store the deploy hook as a secret.** In the service's **Settings → Deploy Hook**, copy the URL. It's a secret, because anyone holding it can trigger deploys. Store it as a GitHub secret, never in the code:
   ```bash
   gh secret set RENDER_DEPLOY_HOOK_URL   # paste the URL when prompted
   ```
3. **Tell CI the public URL,** so it can confirm each deploy went live (this one isn't a secret):
   ```bash
   gh variable set PUBLIC_URL --body https://<your-service>.onrender.com
   ```

**Notes on the free plan:**
- **Cold starts:** it sleeps after about 15 minutes without traffic, so the first request after that takes up to about a minute.
- **Memory:** it has 512 MB of RAM, so `render.yaml` lowers the concurrency and cache limits to keep the worst case well under that.
- **Rate limiting:** it trusts `X-Forwarded-For` from Render's proxy, so each visitor is rate-limited separately. After the first deploy, check that it's working: the `rate_limit_tracked_clients` value on `/metrics` should rise as different visitors use the service.

## API

### `GET /process`

| Param     | Values                                    | Default |
|-----------|-------------------------------------------|---------|
| `url`     | `http(s)` URL of the source image, required | — |
| `width`   | 1–4000                                    | keep aspect ratio |
| `height`  | 1–4000                                    | keep aspect ratio |
| `crop`    | `fit` · `fill` · `scale`                  | `fit` |
| `format`  | `jpeg` (`jpg`) · `png` · `webp` · `avif` · `gif` · `auto` | source format (png if the source format can't be written) |
| `quality` | 1–100 (applies to jpeg / webp / avif)     | 80 |

- **`fit`**: the image fits inside `width×height` and keeps its aspect ratio. The output can be smaller than the box in one dimension. I made this the default because it never distorts or crops.
- **`fill`**: the image covers `width×height` and is centre-cropped to exactly that size. It needs both dimensions.
- **`scale`**: the image is stretched to exactly `width×height`.
- If you pass only one of `width` or `height`, the other is computed from the aspect ratio (any crop mode).

Other behaviour:

- EXIF orientation is applied, so phone photos come out the right way up.
- **All metadata is stripped** (EXIF including GPS, XMP, text chunks). Only the ICC colour profile is kept, and only when it still matches the pixels.
- For animated GIF or WebP sources, only the first frame is output.
- Converting a transparent image to JPEG composites it onto white.
- Unknown or duplicate query parameters are rejected (`widht=500` → 400) rather than silently ignored.

The success response is the image bytes, with these headers:

- `Content-Type`
- `Cache-Control: public, max-age=86400`
- `ETag` (a hash of the output)
- `X-Cache: HIT|MISS|COALESCED`
- `X-Image-Width` and `X-Image-Height`
- `X-Source-Format`, `X-Source-Width`, `X-Source-Height` and `X-Source-Bytes`, which describe the source image (width and height after EXIF orientation), so clients can compare input and output without fetching the source themselves
- `X-RateLimit-Limit` and `X-RateLimit-Remaining`. These are the widely used de-facto names; the IETF `RateLimit` header draft is still changing.
- `X-Request-ID`, on every response including errors. A valid incoming `X-Request-ID` (up to 64 characters of letters, digits, `.`, `_` and `-`) is reused, so you can trace a request across systems. It also appears in every log line.
- `Vary: Accept` when `format=auto`.

A request with a matching `If-None-Match` gets a `304`.

**`format=auto`** returns AVIF if the `Accept` header lists `image/avif`, otherwise WebP if it lists `image/webp`, otherwise the source format. Entries with `q=0` are treated as refused. Browsers send these types themselves, so `<img src="/process?url=…&format=auto">` gets the smallest format each browser supports.

### Other endpoints

- `GET /limits` returns the limits this instance actually enforces, plus the supported formats.
- `GET /healthz` is a liveness check.
- `GET /metrics` returns Prometheus-format metrics:
  - request counts by route and status, and a latency histogram;
  - cache hits and misses, and errors by code;
  - the number of renders in flight and clients being rate-limited.

  Labels use route templates, so source URLs never appear.
- `GET /video/thumbnail` returns `501` (see "What I left out").

### Errors

Every error, including 404s and unexpected 500s, has the same JSON shape and is sent with `Cache-Control: no-store`:

```json
{"error": {"code": "invalid_parameter", "message": "'width' must be an integer between 1 and 4000, got 'abc'.", "param": "width"}}
```

| Status | `code` | When |
|---|---|---|
| 400 | `missing_parameter`, `invalid_parameter`, `unknown_parameter`, `duplicate_parameter`, `invalid_url` | You need to fix the request. `param` names the field. |
| 403 | `url_not_allowed` | The URL, or a redirect it leads to, points at a private or internal address |
| 413 | `source_too_large` | The source is over 15 MB |
| 415 | `unsupported_media_type` | The source isn't one of the accepted image formats |
| 422 | `source_too_many_pixels`, `output_too_large`, `corrupt_image` | The source was fetched but can't be processed |
| 429 | `rate_limited` | Includes a `Retry-After` header |
| 503 | `service_busy` | Too many renders already queued; includes `Retry-After`. Retry shortly |
| 501 | `not_implemented` | Video thumbnails |
| 502 | `upstream_error` (with `upstream_status`), `upstream_unreachable`, `dns_error`, `too_many_redirects` | The source server misbehaved |
| 504 | `upstream_timeout` | The source took longer than 10s |

The 4xx/5xx split follows one rule. 4xx means the caller can fix it by changing the request. 502/504 means the problem is on the source's side, and retrying later might work.

## Web UI

The page at `/` uses only the public API, like any other client.

- It shows the original and processed images side by side, each with its dimensions, format and file size. The original's details come from the `X-Source-*` headers. The original image itself is loaded directly from its URL so it is shown unmodified. If the host blocks embedding, the UI says so and still shows the details.
- Previews are shrunk to fit the panel, so under each one the page says what percentage of actual size it is shown at. Clicking a preview opens the image at full size in a new tab.
- It shows the exact request URL and an equivalent `curl` command, each with a copy button.
- Errors are shown with their status, code and message, plus the raw JSON body. Each panel has an empty state and a loading state.
- Sample buttons try a normal photo, a transparent PNG, a blocked internal address and a URL that isn't an image.

## Production readiness: what I chose and why

### Untrusted URLs (SSRF)

This is the risk I spent the most time on. An open image proxy is a classic way into internal networks, for example `http://169.254.169.254/` (cloud credentials), `localhost` admin ports, or `10.x` services. The code is in `app/fetcher.py` and `app/urls.py`.

- **Schemes:** only `http` and `https`. That excludes `file:`, `gopher:` and `data:`.
- **Ports:** only 80 and 443. This stops the service being used to port-scan third parties. The list is configurable.
- **Embedded credentials** (`user:pass@host`) are rejected. They exist mostly to make URLs confusing.
- **Address check:** I resolve the host myself and refuse it if **any** returned address is not globally routable. That covers:
  - loopback, RFC 1918, link-local/metadata, CGNAT, multicast, reserved and unspecified addresses;
  - IPv6 forms that hide an IPv4 address inside them (IPv4-mapped, 6to4, NAT64, Teredo). Without this, `::ffff:127.0.0.1` would get through.
- **No DNS rebinding:** after checking, the client connects to *the exact IP that was checked*. The `Host` header and TLS SNI still carry the original name, so certificates are verified normally. Otherwise an attacker's DNS could answer with a public IP for the check and `127.0.0.1` for the connection.
  - The trade-off is that connection keep-alive is turned off. Pooled connections are keyed by IP, so a TLS connection opened and verified for `a.com` could otherwise be reused for `b.com` on the same CDN IP. A custom transport that resolves inside the connection pool would get keep-alive back.
- **Redirects** are followed by hand: at most 3, and every hop goes through all of the checks above.
- Environment proxy settings are ignored (`trust_env=False`), because an env proxy would bypass the pinning.

### Resource limits

| Limit | Value | Reasoning |
|---|---|---|
| Fetch timeout | 10s total (3s connect) | It's a deadline for the whole fetch, including redirects. Per-read timeouts alone let a server drip one byte every few seconds forever. |
| Source size | 15 MB | Covers large phone photos and DSLR JPEGs. It's enforced **while streaming**: `Content-Length` is checked up front, but the server can lie about it, so the running count of *decoded* bytes is checked too. That also caps gzip bombs. |
| Source pixels | 40 MP | Decoded memory is what actually costs, not file size. A 100 KB PNG can declare 50,000×50,000 pixels. Dimensions are checked from the header **before decoding**. 40 MP is about 160 MB as RGBA, which covers phone cameras. |
| Output size | 4000 px per side | Bounds the cost of resizing and encoding. Dimensions computed from the aspect ratio are checked too: `width=4000` on a very tall image is rejected with a message saying to pass both dimensions. |
| Input formats | JPEG, PNG, WebP, GIF, BMP, AVIF | Pillow is restricted to these decoders (`formats=` on `Image.open`), so hostile input never reaches its long tail of rarely-audited parsers. **TIFF is excluded** because libtiff has a long CVE history. **SVG is excluded** because rendering it means a full XML/CSS engine that can itself fetch URLs. Formats are detected from the file's bytes, never from `Content-Type` or the file extension. |
| Concurrency | 32 fetches, 4 decodes | Decoding runs in a thread pool and is what uses memory: 4 × 160 MB worst case. Fetches mostly wait on the network, so more can run at once. |

### Abuse

- Each client IP gets a **token bucket**: 60 requests a minute with bursts of 20. Over that it returns `429` with `Retry-After`, and every response carries `X-RateLimit-Remaining`.
- `X-Forwarded-For` is only trusted when `IMG_TRUST_PROXY_HEADERS=true`. Even then only the right-most entry is used, because that's the one our own proxy added and the rest are client-controlled.
- Idle buckets are cleaned up, so memory grows with the number of *active* clients, not with every IP ever seen.
- **Load shedding:** if more than 128 distinct renders are already running or queued, new ones get `503 service_busy` with `Retry-After` instead of joining an unbounded queue. Requests identical to one already running still join it, because they cost nothing extra.
- Limitation: the limiter is per-process and in-memory. Running more than one instance would need Redis or a limit at the edge.

### Performance and caching

- **Result cache:** an in-process LRU cache of processed images, bounded by **bytes** (256 MB) and age (1 h). The key is the normalised parameters, so for example `quality` left out and `quality=80` share one entry. Errors are never cached.
- **Request coalescing:** if many identical requests arrive at once, they share one fetch and one render instead of stampeding the source.
- **HTTP caching:**
  - `Cache-Control: public, max-age=86400` lets browsers and CDNs absorb repeat traffic.
  - The content-hash `ETag` gives cheap `304` responses.
  - The 1 h server cache is shorter than the browser max-age so that changes to a source image eventually show up.
- **Speed:** Lanczos resampling with `reducing_gap` (a fast pre-shrink before the high-quality pass).

### Privacy

- The service strips all image metadata from its output, which matters for user photos.
- Logs record only the route and the source **host**, never a full URL, because paths and query strings often carry tokens or personal data. Uvicorn's access log is off, and httpx's own request logging, which prints full URLs, is limited to warnings. A test checks that a token in a source URL never reaches any log line.

## What I left out, and what I'd do next

Roughly in priority order:

1. **Shared cache and CDN.** Put a CDN in front, keyed on the full URL, and store results in object storage (S3/R2) so instances share them and keep them across restarts. The in-memory LRU is a stand-in for both.
2. **Signed URLs or API keys.** Right now anyone can make the service fetch anything public. Cloudinary-style HMAC-signed parameters would stop it being used as a free proxy and give each customer a key to budget against. Rate limiting by IP is the weakest form of request budgeting.
3. **Host allowlist mode** for deployments that only need to serve their own buckets.
4. **Video thumbnails** (`/video/thumbnail`). This needs ffmpeg in a sandboxed subprocess with its own CPU, memory and time limits. ffmpeg parses a huge range of formats and some can reference other files or URLs (HLS playlists, concat), so it needs its own locked-down protocol allowlist. That wasn't possible to do properly in the time available, so the route returns a clear `501` instead.
5. **Smarter output:** JPEG draft-mode decoding for big downscales, animated output, focal-point/gravity crops, and colour conversion of CMYK sources through their ICC profile (today it's a naive conversion).
6. **Respecting upstream caching headers** (`Cache-Control`/`ETag` revalidation) instead of a fixed TTL, and briefly caching upstream 404s.
7. **Operations at scale:** the metrics, rate limiter and cache are per process. With several instances, scrape each one's `/metrics` and move the limiter and cache to shared stores.

## Client SDK

`image_client/` is a small typed Python client:

```python
from image_client import ImageClient, ImageServiceError

with ImageClient("http://127.0.0.1:8000") as images:
    src = images.url("https://example.com/cat.jpg", width=400, format="auto")  # for an <img> tag
    try:
        result = images.process("https://example.com/cat.jpg", width=400, format="webp")
        print(result.width, result.height, result.source.bytes, result.cache)
    except ImageServiceError as err:
        print(err.status, err.code, err.param, err.message, err.retryable, err.request_id)
```

Every failure raises `ImageServiceError`. That covers API errors, network errors, and replies that aren't in the API's error format. Each carries the fields needed to decide whether to retry.

## Layout

```
app/
  main.py           app factory: wiring, error rendering, the request-observing middleware
  api.py            routes, the rate-limit dependency, response headers
  params.py         the query schema (validation + OpenAPI docs) and format=auto negotiation
  errors.py         every error code with its HTTP status, and the error body shape
  urls.py           URL syntax rules and the public-IP check
  fetcher.py        pinned, redirect-checked, size-limited fetching
  processor.py      decode → orient → resize → encode (sync, runs in a thread)
  service.py        cache + request coalescing + concurrency limits + load shedding
  cache.py          byte-bounded TTL LRU
  ratelimit.py      token bucket
  observability.py  request IDs, JSON logs, Prometheus metrics
  config.py         every setting, validated at startup, overridable via IMG_* env vars
image_client/       typed Python client SDK
static/index.html   the web UI
tests/              see "Tests and checks"
```

Every setting can be overridden with an environment variable named after it, for example `IMG_MAX_SOURCE_BYTES=5000000` or `IMG_RATE_LIMIT_PER_MINUTE=120`. `HOST` and `PORT` are also accepted, because hosting platforms set those.
- **Bad values stop startup:** a value that is out of range, negative, or impossible (for example a connect timeout longer than the total timeout) makes the service refuse to start, with a message naming the setting.
- **Unknown variables warn:** an `IMG_*` variable that isn't a setting, usually a typo, produces a warning.
