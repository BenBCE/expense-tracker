# expense-tracker

Self-hostable Telegram bot that collects business-trip receipts, runs them through
Claude (Sonnet) Batch API for vision-based extraction, and ships back an Excel
report, a combined PDF and a zip of the raw images.

## Stack

- Python 3.12 · FastAPI (webhook mode) · python-telegram-bot v21
- Postgres 16 · SQLAlchemy 2 async (asyncpg) · Alembic
- Hetzner Object Storage (S3-compatible) via aioboto3
- Anthropic Claude Batch API
- APScheduler for the in-process batch poller

## Bot commands

| Command | Description |
|---|---|
| `/start_trip <name>` | Open an active trip (one per user). |
| send a photo / image doc | Upload a receipt to the active trip. Replies with `📸 receipt #N saved`. |
| `/note <text>` | Attach a note to the most recent receipt. |
| `/list` | Count + last 5 receipts of the active trip. |
| `/cancel <n>` | Soft-delete receipt `#n` (also removes the file from the bucket). |
| `/end_trip` | Close the trip, kick the batch job, eventually receive xlsx + pdf + zip. |

`/end_trip` is idempotent: if the trip is already processing or done it re-sends
the existing report (or a "still working" notice).

## Project layout

```
app/
  main.py              FastAPI app + webhook + lifespan
  bot/handlers.py      Telegram command + photo handlers
  bot/keyboard.py      Bot command list
  db/models.py         SQLAlchemy models
  db/session.py        Async engine + session factory
  storage/s3.py        Hetzner Object Storage client (aioboto3)
  ai/batch.py          Anthropic Batch API submit/poll/results + sync fallback
  ai/schema.py         Pydantic extraction contract
  reports/xlsx.py      Excel report (openpyxl)
  reports/pdf.py       Combined PDF (reportlab)
  reports/zip.py       Raw images zip (stdlib)
  scheduler.py         APScheduler poller
  end_trip_flow.py     Orchestration: submit → poll → build → deliver
  config.py            pydantic-settings
alembic/               Migrations
Dockerfile
docker-compose.yml
.env.example
```

## 1 — Hetzner Object Storage

1. Sign in to the Hetzner Cloud Console → **Object Storage** → create a bucket
   in the location you prefer. The endpoint follows the pattern
   `https://<location>.your-objectstorage.com` where `<location>` is one of
   `fsn1`, `hel1`, `nbg1`.
2. Create either **one** or **two** private buckets (ACL = private):
   - One bucket — set `S3_BUCKET_RECEIPTS` and `S3_BUCKET_REPORTS` to the same
     name. Receipts and reports are separated by the configurable prefixes
     `S3_PREFIX_RECEIPTS` (default `receipts`) and `S3_PREFIX_REPORTS`
     (default `reports`).
   - Two buckets — for example `receipts-raw` and `reports`, with empty
     prefixes if you don't want a nested folder.

   The default key layout is:
   - `<S3_PREFIX_RECEIPTS>/{user_id}/{trip_id}/{receipt_id}.jpg`
   - `<S3_PREFIX_REPORTS>/{user_id}/{trip_id}/{filename}.{xlsx,pdf,zip}`
3. Create an **S3 access key** under the same project. Note the key id and
   secret. Put them into `S3_ACCESS_KEY` / `S3_SECRET_KEY`.
4. Set `S3_REGION` to match the location (e.g. `fsn1`) and `S3_ENDPOINT_URL`
   to the matching endpoint URL.

The app uses **path-style addressing** for object operations and
**virtual-hosted style** only when minting presigned URLs (Hetzner serves both;
virtual-hosted plays nicer with strict S3 clients).

## 2 — Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → grab the token.
   Put it into `TELEGRAM_BOT_TOKEN`.
2. Pick a long random string for `TELEGRAM_WEBHOOK_SECRET`
   (`openssl rand -hex 32`). Telegram includes this as the
   `X-Telegram-Bot-Api-Secret-Token` header so the app can reject spoofed calls.
3. The app registers the webhook automatically at startup at
   `${PUBLIC_BASE_URL}/tg`. Make sure `PUBLIC_BASE_URL` is reachable over HTTPS
   (Coolify or a reverse proxy in front of the container).
4. (Recommended) Lock the bot down to your own Telegram user id by setting
   `TELEGRAM_ALLOWED_USER_IDS` to a comma-separated list of numeric ids
   (e.g. `12345678` or `12345678,87654321`). Any messages from other users
   are silently ignored. To find your id, message [@userinfobot](https://t.me/userinfobot).

## 3 — Run locally with docker compose

```bash
cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, S3 keys, PUBLIC_BASE_URL
docker compose up --build
```

The `app` service:

- runs `alembic upgrade head` on boot,
- launches `uvicorn` on port 8000 (only `expose`d, not published — Coolify or a
  reverse proxy publishes it),
- has a read-only root filesystem with `/tmp` mounted as a tmpfs for report
  generation.

For local development you can put `localhost.run` / `ngrok` / Cloudflare Tunnel
in front of the container and set `PUBLIC_BASE_URL` to the tunnel URL.

## 4 — Deploying with Coolify

1. In Coolify, create a new **Resource → Docker Compose** application and point
   it at this repository.
2. Set the environment variables from `.env.example` in the Coolify UI:
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `ANTHROPIC_API_KEY`
   - `PUBLIC_BASE_URL` (use the public URL Coolify assigns)
   - `DATABASE_URL` (`postgresql+asyncpg://expense:expense@db:5432/expense`)
   - `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` (must match the URL)
   - `S3_ENDPOINT_URL`, `S3_REGION`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`,
     `S3_BUCKET_RECEIPTS`, `S3_BUCKET_REPORTS`
3. In the Coolify proxy, route the public domain to the `app` service on port
   8000. Coolify handles TLS termination.
4. Click **Deploy**. The first boot runs Alembic, sets the Telegram webhook,
   starts polling, and is ready.

The single container hosts both FastAPI and APScheduler — no separate worker
service is needed for v1.

## Extraction contract

For each receipt the app sends one Batch request; Claude must return JSON ONLY
matching:

```json
{
  "vendor": "string|null",
  "date": "YYYY-MM-DD|null",
  "currency": "ISO-4217|null",
  "subtotal": "number|null",
  "vat": "number|null",
  "total": "number|null",
  "category": "meals|lodging|transport|fuel|office|other",
  "line_items": [{"name": "string", "amount": 0}],
  "confidence": 0.0
}
```

Validation happens with pydantic. Items that fail JSON-parse or schema-validate
are retried once via the synchronous Messages API. Anything still failing is
marked `failed`; the raw response is stored in `expenses.raw_json` for review.

## Deliverables

After a batch completes the app generates three files in `/tmp`, uploads them
to the `reports` bucket and sends them via Telegram `send_document`:

1. `{trip_name}.xlsx` — sheet **Expenses** (Date, Vendor, Category, Subtotal,
   VAT, Total, Currency, Note, Receipt File) plus sheet **Summary** with totals
   grouped by category and grand totals per currency. Header row is frozen,
   amounts use a currency number format.
2. `{trip_name}.pdf` — page 1 has trip header + summary table; pages 2..N show
   one receipt image per page captioned `#n — vendor — date — total currency`.
3. `{trip_name}-receipts.zip` — raw images renamed
   `{NNN}_{YYYY-MM-DD}_{category}.jpg`.

If the combined size exceeds `BUNDLE_SIZE_LIMIT_MB` (default 45 MB) the app
ships a single `{trip_name}-bundle.zip` instead. As an ultimate fallback it
sends 24 h presigned URLs (virtual-hosted style).

## Development

```bash
uv sync --all-extras
ruff check .
mypy app
```

Migrations:

```bash
DATABASE_URL=postgresql://user:pass@localhost/db alembic upgrade head
DATABASE_URL=postgresql://user:pass@localhost/db alembic revision --autogenerate -m "msg"
```

## Out of scope for v1

Currency conversion, OCR fallback, web UI, multi-user trip sharing, and
human-in-the-loop receipt approval are planned for v1.1.
