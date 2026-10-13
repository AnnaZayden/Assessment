# Image Processing Service

Give it an image URL and it returns the image resized and/or converted. Give it a video URL and it returns one frame as an image. It also has a simple web UI for trying it out.

```
GET /process?url=https://picsum.photos/id/1025/1600/1067&width=800&height=600&format=webp
GET /video/thumbnail?url=https://www.w3schools.com/html/mov_bbb.mp4&time=3&width=480
```

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt ".[dev]"
.venv/bin/python -m app
```

Open http://127.0.0.1:8000 for the UI, or http://127.0.0.1:8000/docs for the API docs.

With Docker:

```bash
docker build -t images . && docker run -p 8000:8000 images
```

## Test it

```bash
.venv/bin/python -m playwright install chromium   # once, for the UI tests
.venv/bin/python -m pytest
```

The tests don't need the internet. Lint, type checks and coverage (100% required) run in CI on every push.

## API

### `GET /process`

| Param     | Values                                   | Default |
|-----------|------------------------------------------|---------|
| `url`     | image URL (required)                     | — |
| `width`   | 1–4000                                   | keep aspect ratio |
| `height`  | 1–4000                                   | keep aspect ratio |
| `crop`    | `fit` · `fill` · `scale`                 | `fit` |
| `format`  | `jpeg` · `png` · `webp` · `avif` · `gif` · `auto` | same as source |
| `quality` | 1–100                                    | 80 |

- **fit**: fits inside the box without cropping or stretching
- **fill**: fills the box exactly, cropping the edges
- **scale**: stretches to the exact size
- **auto**: picks the smallest format the browser supports (AVIF, then WebP)

Metadata such as GPS location is removed from every output image.

### `GET /video/thumbnail`

Takes the same parameters as `/process`, plus:

| Param  | Values                      | Default |
|--------|-----------------------------|---------|
| `time` | seconds into the video, e.g. `1.5` | first frame |

- Supported videos: MP4, MOV, WebM, MKV, AVI (up to 50 MB)
- The default output format is JPEG
- Asking for a time past the end of the video returns a `400`

ffmpeg does the decoding (installed with `imageio-ffmpeg`, no system package needed). It only reads the downloaded file, only accepts the formats above, and is stopped after 10 seconds. On Linux it is also capped at 1 GB of memory.

### Other endpoints

- `GET /` is the web UI
- `GET /healthz` is a health check
- `GET /limits` lists the current limits
- `GET /metrics` returns Prometheus metrics

### Errors

Every error returns JSON in the same shape:

```json
{"error": {"code": "invalid_parameter", "message": "'width' must be an integer between 1 and 4000, got 'abc'.", "param": "width"}}
```

- **4xx**: something is wrong with the request (bad parameter, blocked URL, image too large, rate limited)
- **5xx**: the source server failed or timed out, or the service is busy

## Safety and limits

- **Blocks internal addresses.** It won't fetch `localhost`, private networks or cloud metadata IPs, and it checks every redirect too.
- **Limits size.** Images are capped at 15 MB and 40 megapixels, videos at 50 MB, outputs at 4000 px per side, and fetches at 10 seconds.
- **Rate limits.** Each IP gets 60 requests per minute.
- **Caches results.** Repeat requests are served from memory, and identical requests that arrive together share one fetch.

Any limit can be changed with an environment variable, for example `IMG_MAX_SOURCE_BYTES=5000000`.

## Project layout

```
app/          the service (routes, fetching, image processing, caching, rate limiting)
image_client/ a small Python client for the API
static/       the web UI
tests/        tests
```
