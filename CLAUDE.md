# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Working rules

Full text: `../owload-docs/CONTRIBUTING.md`. In short:

- Work comes from tasks in `../owload-docs/epics/`. A task is committed **at the same time in `owload-docs`, `owload-back` and `owload-front`** (docs always get the task status / architecture update); commit messages reference the same task.
- **Before every push, in every repository, check the diff:**
  - (a) no sensitive data — secrets, tokens/JWTs, keys, `.env` values, real hostnames/IPs, personal data or machine-specific paths;
  - (b) security does not get worse — the server must never receive, store or log the password, `K_master` or any derived key, plaintext content/names/paths, or anything outside the API contract (`../owload-docs/architecture/backend.md`); crypto changes need an ADR.
- English only in code, comments, docs and commit messages.
- Commit identity is `Owload <info@owload.com>` only — no `Co-Authored-By` or generated-by lines; pass it per commit with `git -c user.name=Owload -c user.email=info@owload.com commit ...`.
- Pre-production: commit straight to `main`; rewrite history only when explicitly agreed.

## What this is

FastAPI backend for **Owload** — an encrypted virtual "drive". The
server never sees plaintext: the append-only ops log and the drive's
byte-addressable data area are both opaque ciphertext from the backend's
point of view. Encryption/decryption and filesystem structure live entirely
on the client.

**Contract note:** this backend uses `drive`/`driveId` terminology
(`/drives`, `?driveId=`), and the frontend (`owload-front`,
`src/engine/backend/*.ts`) has been migrated to the same contract. The old
`/containers`/`?containerId=` naming no longer exists on either side.

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # then set KEYCLOAK_ISSUER/KEYCLOAK_AUDIENCE to match a real token's iss/aud exactly

# Local infra (Postgres + MinIO)
docker compose up -d
alembic upgrade head
docker compose exec minio mc alias set local http://localhost:9000 minioadmin minioadmin
docker compose exec minio mc mb local/owl-files

# Run the API
uvicorn app.main:app --reload      # Swagger at http://localhost:8000/docs

# Tests
pytest                              # full suite
pytest tests/test_security.py -v   # unit only, no infra needed
pytest tests/test_integration.py::test_free_range_deletes_only_fully_covered_blocks -v  # single test

# New migration
alembic revision -m "description"   # then hand-write upgrade()/downgrade(), see existing versions/ for the style
```

Test files fall into two kinds:
- `test_security.py`, `test_storage.py`: pure unit tests, no external services, always run.
- `test_integration.py`, `test_compaction.py`: hit the real Postgres/S3 configured in `.env` (defaults to the docker-compose MinIO). They self-skip via an autouse fixture if Postgres/S3 isn't reachable — don't add mocks to work around a skip, fix the infra instead.

Auth in integration tests is stubbed via `app.dependency_overrides[get_current_user]` — no live Keycloak needed to run them.

## Architecture

### The data model is NOT "files" — it's a flat byte space + an opaque log

There is no `File` concept anywhere in this backend. A `Drive` (table
`drives`) has exactly two things:
1. `ops_data` (bytea) — an append-only log of client-encoded filesystem
   operations. The backend cannot parse it; it only appends bytes
   (`POST /ops/{driveId}`) and returns byte-range slices (`GET /ops/{driveId}/{startBytePos}`).
2. `data_length` (bigint) — the current size of a byte-addressable data area.
   Actual bytes live in S3 as 1 MiB blocks (see below). "Files" are a concept
   the *client* builds on top of this by tracking byte ranges in its own
   encrypted ops log — the backend never knows which bytes belong to which
   file.

This has a direct consequence for any new endpoint touching drive data:
**the backend can only act on byte ranges the caller supplies**, and can
never infer file boundaries. See `DELETE /data` below for how far this
constraint is pushed.

### Upload sessions: reserve-then-write

`data_length` only ever grows. `POST /sessions?driveId=&byteLength=`
atomically extends it (`UPDATE ... RETURNING`) and returns the
`[byte_offset, byte_offset+byte_length)` region reserved for this session.
`POST /data?sessionId=&start=` writes at `session.byte_offset + start`;
`POST /sessions/finalize/{sessionId}` only flips a status flag — bytes are
already "live" the moment they're written, finalize doesn't gate anything.
This mirrors the frontend's own in-memory mock backend
(`owload-front/src/engine/backend/test/mocks/filesystem.mock.ts`), which is
the closest thing to a spec for this byte-range/session behavior.

### Block storage (`app/services/storage.py`)

The data area is chunked into fixed 1 MiB (`BLOCK_SIZE`) blocks, one S3
object per block, keyed `data/{driveId}/{block_index:012d}`. S3 has no
in-place partial write, so:
- A write covering a whole block just `put_object`s it.
- A partial write does read-modify-write of that one block.
- A read (`iter_range`) uses S3 Range GETs to fetch only the requested
  slice of a block, never the whole 1 MiB, and streams block-by-block so a
  `StreamingResponse` can stop early if the client disconnects
  (`_stream_with_disconnect_check` in `app/api/routes/data.py` — important
  for video `<video>` elements that fire many overlapping Range requests).
- **Deletion only ever removes whole blocks** (`free_range`): a block is
  deleted iff the freed range fully covers it (nothing else can live there);
  a partially-overlapping block is left completely untouched. This is a
  hard architectural constraint, not a shortcut — see the "byte-addressing
  is permanent" reasoning below before trying to make deletion smarter.
- `delete_objects` responses are checked for partial per-key `Errors`
  (`BlockDeleteError`) — a missing key is success (S3 semantics), a real
  per-key failure is not, and the two must not be conflated.

**Byte-addressing is permanent — this is why blocks can only be trimmed from
the tail, never the head or middle.** Absolute byte offset `abs_pos` always
maps to `(abs_pos // BLOCK_SIZE, abs_pos % BLOCK_SIZE)` with zero
indirection — no lookup table, no per-block metadata. A block object's byte
0 is always exactly `block_index * BLOCK_SIZE`. Shrinking a block from the
tail preserves this (nothing after the freed tail exists in that object, so
byte 0's meaning never changes; reads past a short object already return
zero-fill). Shrinking from the head or middle would require every later
byte's position-within-block to shift, which breaks the zero-lookup
invariant and would require introducing a whole indirection layer (virtual
offset → physical location) that doesn't exist today. Don't build partial-
head/middle trimming without discussing this tradeoff with the user first.

### Dual-write recovery backup to S3 (independent of the block store above)

Beyond the block store, two things are additionally mirrored to S3 purely
for disaster recovery if Postgres is lost — this is a *second*, unrelated
purpose from the block storage above:
- `drives/{driveId}/meta.json` — written once at drive creation.
- `ops/{driveId}/{from}-{to}.bin` — one object per `saveOperation()` call,
  named by the byte range it occupies in `ops_data`.

The S3 write always happens **before** the Postgres `commit()` in the same
request (see `app/api/routes/drives.py::create_drive`,
`app/api/routes/ops.py::save_operation`): if the S3 write throws, the
exception propagates and the request's uncommitted Postgres changes are
rolled back when the session closes (`expire_on_commit=False` session +
no explicit rollback needed — closing an uncommitted session discards it).
This is the pattern to follow for any future "write must survive Postgres
loss" feature: S3 write first, commit second, never the other order.

`app/services/compaction.py` + `app/compaction_worker.py` (standalone, run
via cron or the `compaction` docker-compose profile — see README) merges
the small `ops/*.bin` chunks into larger `ops-batches/*.bin` objects,
crash-safely (batch written before originals deleted, so a crash mid-way
leaves double coverage, never data loss — the next run cleans up the
leftover chunks it finds already covered by an existing batch, before
doing any new merging).

### Audit log (`app/services/audit.py`, table `drive_action_log`)

`log_action(db, drive_id, user_id, action, **attributes)` just does
`db.add(...)` — it relies entirely on the caller's own transaction/commit
for atomicity. Call it right before a route's existing `db.commit()` so the
log entry only persists if the action it describes actually succeeded (no
separate commit, no try/except needed around it). `GET_OPS`/`GET_DATA` are
logged too, which means routes that previously had zero Postgres writes
(pure reads) now commit one row per call — worth remembering before adding
another read endpoint and wondering why it suddenly needs a `db` dependency.

### Auth

JWT arrives via the **`x-access-token` header**, not `Authorization:
Bearer` — this is what the actual frontend's axios interceptor sends
(`owload-front/src/auth-context-provider.tsx`). `app/core/security.py`
verifies against Keycloak's JWKS (cached, refetched once on unknown `kid`
before failing). No local user/password store — `users` table
(`app/db/models.py`) is just a lazily-created profile (name/email) for
`findUsers()` search, keyed by the Keycloak `sub`.

### Route module ↔ resource mapping

Routes have no shared prefix; each module's `APIRouter(prefix=...)` matches
the frontend's REST calls one-to-one:
- `app/api/routes/drives.py` — `/drives`
- `app/api/routes/ops.py` — `/ops`
- `app/api/routes/sessions.py` — `/sessions`
- `app/api/routes/data.py` — `/data` (write/read/delete/ranges)
- `app/api/routes/userinfo.py` — `/userinfo`

`app/api/deps.py::get_accessible_drive_or_404` is the shared authorization
check (owner or present in the drive's `acl` JSONB) — every route touching
a specific drive calls it and gets an indistinguishable 404 for "doesn't
exist" vs. "no access."
