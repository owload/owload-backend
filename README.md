# owload-back

The backend for owload-front (an encrypted virtual "drive" with client-side
encryption). The server never sees plaintext — both the append-only operation
log and the drive's byte data area are opaque ciphertext from the backend's
point of view.

Both sides use the `drive`/`driveId` terminology (`/drives`, the `driveId`
query parameter) since 2026-07-13 — the frontend and the backend are in sync.

## Stack

- **FastAPI** — the HTTP API
- **PostgreSQL + SQLAlchemy 2.0 (async) + Alembic** — users, drives, upload
  sessions
- **S3-compatible storage** (S3/MinIO) via `aioboto3` — the drive's byte data
  area, cut into fixed blocks (1 MiB), each block a separate S3 object; a
  partial block write is done as a read-modify-write
- **Keycloak (OIDC/JWT)** — authentication; the backend only verifies tokens
  through the realm's JWKS, there are no local logins/passwords

## Data model

- `users` — a local profile (`id` = Keycloak `sub`, `email`, `name`); created
  lazily on `POST /userinfo`
- `drives` — a "drive": `owner_user_id`, `title`, `acl` (JSONB
  `{userId: [privileges]}`), `key_nonce`/`counter_nonce`, `ops_data` (bytea,
  grows by atomic appends), `data_length` (the current length of the byte data
  area)
- `upload_sessions` — reserves `[byte_offset, byte_offset+byte_length)` in the
  drive's `data_length`; `finalize` only marks the session as finished, the
  bytes themselves are already "live" right after being written
- `s3_presets`/`drive_storage_targets` — several S3 targets per drive (a
  mandatory `MASTER` + up to 2 `SLAVE`s, see
  [owload-docs/decisions/0011](../owload-docs/decisions/0011-multi-s3-storage-topology.md)).
  Writing (`write_range`/`append_ops_backup`/`write_drive_meta`) fans out to all
  of the drive's `ACTIVE`/`PROVISIONING` targets simultaneously; if a PUT fails
  on at least one target, the targets that already managed to write are rolled
  back with a compensating DELETE and the client gets a 502 (the rollback
  itself is best-effort, its failure is only logged). Reading (`iter_range`,
  `GET /data/ranges`) always goes to the `MASTER` only. When creating a drive
  (`POST /drives`) you can pass a `presetId` (it must be `tier=hot` — `cold` is
  forbidden for a master) or a `customConfig` (its own connection parameters,
  always treated as hot); if neither is passed, the drive gets an implicit
  `MASTER` target on the shared legacy preset (the same behaviour as before,
  for clients that do not yet send either of these fields). The tables for
  drives that already existed at the time of the migration are populated with
  the same implicit preset by migration `0005`. Secrets (`secret_key`) are
  encrypted with a separate backend key (`BACKEND_SECRET_KEY`, see
  `.env.example`) — this is an infrastructure secret, not the same one that
  encrypts drive data on the client

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
# adjust KEYCLOAK_ISSUER / KEYCLOAK_AUDIENCE to your realm/client —
# they must match the "iss"/"aud" claims in a real token character for character

docker compose up -d          # postgres + minio
alembic upgrade head          # apply the DB schema

# create the bucket in MinIO (once)
docker compose exec minio mc alias set local http://localhost:9000 minioadmin minioadmin
docker compose exec minio mc mb local/owl-files

uvicorn app.main:app --reload
```

Swagger UI: http://localhost:8000/docs

## Tests

```bash
pytest
```

- `tests/test_security.py` — unit tests of JWT verification on its own RSA
  pair, they mock JWKS, a real Keycloak is not needed.
- `tests/test_integration.py` — end-to-end tests against a real Postgres+MinIO
  from `docker-compose.yml` (auth is replaced through
  `app.dependency_overrides`, a real Keycloak is not needed). They are skipped
  automatically if Postgres is unavailable.

## Authentication

The token is passed in the **`x-access-token`** header (not `Authorization:
Bearer`) — that is how the axios interceptor in `owload-front` sends it
(`src/auth-context-provider.tsx`, `src/engine/api/api.ts`).

## API

Paths have no shared prefix. `GET /healthz` needs no authorization, everything
else requires `x-access-token`.

### Users (`user-backend.ts`)
- `GET /userinfo` — the current user's profile; 404 if not yet saved
- `POST /userinfo` `{name}` — profile upsert (email/id are taken from the token)
- `GET /userinfo/search/{search}` — search by email/name (`ILIKE %search%`)

### Drives (`drive-backend.ts` — was `/containers`, now `/drives`)
- `GET /s3-presets` — the list of presets in the pool (`{id, label, tier}`,
  `tier` is always lowercase `hot`/`cold` — to fit the frontend's TS type
  `S3PresetTier`), without secrets and connection parameters. It is needed so
  that `create-drive-page.tsx` can draw the preset choice for `presetId` on
  `POST /drives`/`POST /drives/{driveId}/storage-targets`
- `POST /s3-presets/{presetId}/test-connection` — check the availability of a
  specific preset directly, even before it is bound to any drive: the fact that
  a preset is an admin one does not mean it is available right now (wrong
  credentials, the bucket was renamed/deleted, the endpoint is unavailable).
  The same contract `200 {"ok": bool, "error": string | null}` as the other
  test-connection endpoints (see below) — the implementation is shared
  (`BlockStorage.test_connection`), 404 if there is no preset with that id
- `POST /drives` `{title, keyNonce, counterNonce}` — create a drive
- `GET /drives` — the list of available ones (own + those where the user is in
  the ACL)
- `GET /drives/{driveId}` — drive info; 404 if it does not exist or there is no
  access
- `GET /drives/{driveId}/actions?limit=&before=` — the drive's action audit log
  (see below), newest first; `before` is the cursor (the `seq` of the last
  entry of the previous page), keyset pagination, not `OFFSET`
- `DELETE /drives/{driveId}` `{customTargetDecisions: [{targetId,
  deleteData}]}` — delete the whole drive. Only the owner
  (`drive.owner_user_id`), not any user from the `ACL` — `403` for the others
  who have access, `404` for those who have no access at all (so as not to
  reveal that the drive exists). The drive's preset targets are purged
  unconditionally; for **each** custom target an explicit `deleteData` decision
  is required (true — erase the data on the user's S3; false — leave it as is)
  — there must be a decision for exactly each custom target of the drive, no
  more and no fewer, otherwise `400` stating which target is missing or which
  one is extra. The drive record (`upload_sessions`/`drive_action_log`/
  `drive_storage_targets` — by cascade) is deleted in any case, only after the
  S3 cleanup of all targets has succeeded; a partial cleanup failure is `502`,
  the drive is **not** deleted from Postgres, and a repeated request is safe
  (the same idempotence as for removing a slave target, reusing the same
  `BlockStorage.purge_target_data()`). `?force=true` — delete the drive even if
  the cleanup of some target failed altogether (not a `BlockDeleteError` — the
  target is physically unreachable: the host is down, the keys were revoked —
  this is the user's own infrastructure, whose availability the backend does
  not guarantee). Without `force` this would otherwise be an eternal block: a
  drive with an unreachable custom target could never be deleted. With `force`
  — the data on the unreachable target may remain (a deliberate, logged risk,
  not a suppressed error)

### Managing a drive's S3 targets
- `GET /drives/{driveId}/storage-targets` — the list of the drive's targets
  (`{id, role, status, tier, presetId?, presetLabel?, isCustom,
  customEndpointUrl?, customBucket?, backfillCopied?, backfillTotal?}[]`,
  `tier` in lowercase); without secrets and connection parameters apart from
  `endpointUrl`/`bucket` for custom targets (the frontend needs them for a
  human-readable display). `backfillCopied`/`backfillTotal` — only for
  `status=PROVISIONING`, see "Backfill of new slave targets" below
- `POST /drives/{driveId}/storage-targets` `{presetId}` or `{customConfig}` —
  add a slave target (a preset of any tier or its own connection parameters;
  unlike the initial/master target, `cold` is allowed for a slave). At most 2
  slaves per drive. From this moment the new row takes part in the fan-out
  write on a par with the others — copying the already accumulated historical
  data onto it is done by a separate worker (`app/backfill_worker.py`, see
  below), not by this endpoint. The status of the new row: `ACTIVE` right away
  if the drive has neither byte data (`data_length == 0`) nor operation history
  (`ops_data` is empty) — then there is nothing to back up, the master and the
  new slave are already trivially identical (both empty); otherwise —
  `PROVISIONING` until the worker's next pass. The new target is compared by
  physical location (`endpoint_url`+`bucket` — not by `presetId`/credentials)
  with **all** of the drive's existing targets, including the `MASTER`: a
  match is `400`, because two targets on the same bucket would not be
  independent copies of the data, although the model would consider them such
  (deleting one would really erase the data of the other)
- `PATCH /drives/{driveId}/storage-targets/{targetId}/make-master` — reassign
  the master: a transactional change of roles (the current master → `SLAVE`,
  the given target → `MASTER`). The given target must be `status=ACTIVE` and
  not cold-tier — otherwise `400`. It does not require stopping active upload
  sessions (the fan-out does not depend on `role`)
- `DELETE /drives/{driveId}/storage-targets/{targetId}?confirm=true` — remove a
  slave target. The `MASTER` cannot be deleted by this endpoint (`400` —
  reassign first). Without `confirm=true` — `400`. On confirmation: the target
  is immediately marked `status=REMOVING` (the fan-out stops going to it right
  away, even before the physical deletion finishes), then all of this target's
  objects in S3 are really deleted (`data/`, `ops/`, `ops-batches/`, `drives/`
  under this `driveId`); if deleting some of the objects failed — `502`, the
  target stays `REMOVING` and the request can be repeated (deletion is
  idempotent). `&force=true` — detach the target even if its S3 is unreachable
  altogether (not about individual keys with an error — a really unreachable
  host / revoked credentials), otherwise a target on an unreachable custom
  storage would get stuck in `REMOVING` forever with no way out: the data on it
  may remain, this is an accepted and logged risk
- `POST /storage-targets/test` `{endpointUrl?, region, bucket, accessKey,
  secretKey, useSsl}` — check arbitrary connection parameters that are not yet
  saved anywhere (the drive creation / add-slave form, before sending the
  `POST`). It is not bound to a specific drive, hence no `{driveId}` in the
  path
- `POST /drives/{driveId}/storage-targets/{targetId}/test-connection` — check
  an already saved target; the body is empty, the backend itself fetches and
  decrypts the credentials — secrets are not shuttled through the API
- Both always respond `200 {"ok": bool, "error": string | null}` — a failed
  connection (wrong credentials, an unreachable host, a missing bucket, a 5 s
  timeout) is not 4xx/5xx, it is a result to show to the user, not an API
  error. The implementation is `head_bucket` under `asyncio.timeout(5)`
  (`BlockStorage.test_connection`)

### Operation log (`filesystem-backend.ts`, append-only)
- `GET /ops/{driveId}/{startBytePos}` — a slice of the log from the given
  position (`arraybuffer`); empty if `startBytePos` is beyond the current
  length; 400 for a negative value
- `POST /ops/{driveId}` — raw bytes in the body, atomically appended to the end
  of the log (`ops_data || :chunk`)

### Sessions and the drive's byte data area
- `POST /sessions?driveId=&byteLength=` — atomically reserves an area in the
  drive's `data_length`, returns `SessionInfo` (was `containerId`)
- `POST /sessions/finalize/{sessionId}` — marks the session as finished
- `POST /data?sessionId=&start=` — raw bytes in the body, written at the
  absolute offset `session.byteOffset + start` (in the block S3 storage)
- `GET /data?driveId=&start=&end=` — streams a range of the drive's bytes
  (`StreamingResponse`, stops fetching blocks from S3 as soon as the client
  disconnects — important for `<video>`, which sends many Range requests and
  aborts the previous ones when seeking); unwritten (but reserved) regions are
  returned as zeros (was `containerId`)
- `DELETE /data?driveId=&start=&end=` — frees a range when the client deletes
  a file. The backend knows nothing about "files" (file boundaries are known
  only to the client's ops log), so it really deletes from S3 only those 1
  MiB blocks that fall **entirely** into the range being deleted (which means
  nothing but this file can be there); a block that the range covers only
  partially (with bytes of still-live files mixed in) is not touched at all —
  neither zeroed nor rewritten. If S3 returned `Errors` in response to
  `delete_objects` for at least one key (not the same as a key simply not
  existing — S3 counts that as success), the endpoint responds `502` rather
  than silently pretending everything was deleted
- `GET /data/ranges?driveId=` — the list of byte ranges actually allocated in S3
  (merged contiguous blocks), `[{start, end}, ...]`. Only a key listing
  (`list_objects_v2`), without reading the block contents — cheap, the cost
  grows with the number of blocks, not the volume of data. It is meant for
  reconciliation with what the client, from its own ops log, considers
  "should be allocated", in order to then send `DELETE /data` for the excess

## Drive action audit log

The table `drive_action_log`: `drive_id`, `user_id`, `action`, `attributes`
(JSONB, with its own fields for each `action`), `created_at`. It is written
strictly on success — the row is added to the same transaction as the effect of
the action itself and is committed together with it
([app/services/audit.py](app/services/audit.py)); if the request failed
(404/400/409/502), the log row is not saved, because the transaction is rolled
back as a whole.

The logged actions are only those where one call strictly corresponds to one
meaningful action (not reads, not chunks of a byte write): `CREATE_DRIVE`,
`START_SESSION`, `FINALIZE_SESSION`, `SAVE_OP` (`{start, end}`), `DELETE_DATA`
(`{start, end, blocksDeleted}`). `GET_OPS`/`GET_DATA` (reads) and `SAVE_DATA`
(written for every chunk of `POST /data`, and a single file can produce from one
to many such rows depending on how the client split the write into chunks) are
deliberately not logged — see
[owload-docs/decisions/0003](../owload-docs/decisions/0003-stop-auditing-pure-reads.md)
and [owload-docs/decisions/0008](../owload-docs/decisions/0008-stop-auditing-save-data.md).

It is read through `GET /drives/{driveId}/actions?limit=&before=` — keyset
pagination by a monotonic `seq` (not `OFFSET`): inserting a new row between two
page requests cannot shift which rows the next page returns, unlike `OFFSET`,
which computes the position among what is "alive" at the moment of each
request.

## Recovery from S3 (the duplicate write)

Postgres is the source of truth for `ops_data` and the drive metadata, but on
every operation a duplicate write to S3 is also created — so that the data can
be recovered even if Postgres is lost:

- `drives/{driveId}/meta.json` — `{driveId, owner, title, keyNonce,
  counterNonce, createdAt}`, written once when the drive is created
  (`POST /drives`) and not changed afterwards
- `ops/{driveId}/{from}-{to}.bin` — every call of `POST /ops/{driveId}`
  additionally puts its chunk into S3 under a name encoding the byte range in
  `ops_data` (`from`/`to` are 20 digits with leading zeros, for correct
  lexicographic sorting when listing)

The write to S3 happens **before** the Postgres transaction's `commit()`: if
the backup failed, the exception propagates and the uncommitted Postgres write
is rolled back when the session is closed — so `ops_data`/the drive and their
backup in S3 never diverge.

### Compaction

`app/services/compaction.py` + `app/compaction_worker.py` — a separate process
independent of Postgres, which periodically lists `ops/{driveId}/`, glues
contiguous ranges into `ops-batches/{driveId}/batch-{from}-{to}.bin` and
deletes the original chunks:

```bash
python -m app.compaction_worker                       # one pass over all drives
python -m app.compaction_worker --loop                # an endless loop
python -m app.compaction_worker --loop --interval 900 # once every 15 minutes
```

Safe to crash at any point: the new batch object is written **before** the
original chunks are deleted, so an interruption between these two steps leaves
the range covered twice (not lost); the next pass finds and cleans up such
doubled chunks without re-batching anything. `reconstruct_ops_from_backup()` in
the same module restores the operation log from S3 alone (both prefixes,
`ops/` and `ops-batches/`, with deduplication of overlaps) — the drive recovery
after a loss of Postgres would work the same way.

**Running in docker-compose** (locally/staging, without a dedicated Dockerfile
for the worker — the `python:3.12-slim` image mounts the code and installs
dependencies at startup). The service is not started by a plain
`docker compose up -d`, only via a profile:

```bash
docker compose --profile compaction up -d compaction-worker
docker compose logs -f compaction-worker
```

**Running via cron** (production, without docker-compose) — for example every
15 minutes:

```cron
*/15 * * * * cd /opt/owload-back && /opt/owload-back/.venv/bin/python -m app.compaction_worker >> /var/log/owl-compaction.log 2>&1
```

### Backfill of new slave targets

`app/services/backfill.py` + `app/backfill_worker.py` — unlike compaction, this
worker needs Postgres (`drive_storage_targets`, to know which target is
`PROVISIONING`, and to resolve the credentials of the master and of the target
itself). Each pass finds all `PROVISIONING` rows across all drives and for each
copies from the master the current backlog — the data blocks (`data/`), the
operation log backup (`ops/`, `ops-batches/`) and the drive recovery record
(`drives/{driveId}/meta.json` — it is written only once when the drive is
created, onto the then current set of targets, so a slave added later would be
left without its copy without a backfill), then moves the target to `ACTIVE`.
The order "fan-out first, then the snapshot" (see
[owload-docs/decisions/0012](../owload-docs/decisions/0012-new-target-backfill-worker.md))
is already guaranteed by the fact that the target becomes `PROVISIONING` (and so
joins the fan-out write) before the worker even learns about it.

Idempotent and safe to crash at any point — copying simply overwrites the same
keys with the same bytes, there is no separate "how far we have copied" state
and nothing to lose. The race "the worker copies a block into which a live
write is happening in parallel" is an accepted risk (S3 last-write-wins), not
eliminated by this worker; see
[owload-docs/epics/0014](../owload-docs/epics/0014-storage-replication-reconciliation).

```bash
python -m app.backfill_worker                      # one pass
python -m app.backfill_worker --loop                # an endless loop
python -m app.backfill_worker --loop --interval 60  # once a minute
```

`docker compose --profile backfill up -d backfill-worker` — similar to
compaction, but it needs Postgres too, not only S3.

The progress of a specific backfill is visible through
`GET /drives/{driveId}/storage-targets` — while the target is `PROVISIONING`,
`backfillCopied`/`backfillTotal` show the current number of copied objects
against how many the master has right now (a live count through
`list_objects_v2`, not persistent state; `null`/`null` once the target is no
longer `PROVISIONING`).

One pass usually takes seconds to tens of seconds (it depends on the number of
drives with accumulated `ops/` chunks), so `--loop` in a container or a
one-off run via cron are equivalent options — choose according to your
deployment's taste.
