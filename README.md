# datum-notification-relay

An anonymous push-relay server. It stores only `{job schedule, job kind, an
anonymous key, an APNs device token}` and delivers time-based silent pushes to
devices that can't reliably self-trigger on their own (a mobile OS won't wake
a backgrounded app on a schedule it doesn't control). It never sees or stores
tracker data, values, names, or notes from any client app.

This repo exists so that claim is independently verifiable, not just a policy
statement. It contains only generic scheduling/relay infrastructure — a job
store, a cron trigger, and an APNs sender — no client application source, no
business logic beyond "at time T, for job kind K, send this payload to this
device token."

## The job model

```
Job {
  id: UUID
  kind: "remoteFetch" | "automationFire"
  schedule: oneShotDatetime | recurring     // once / hourly / everyNHours / dailyAtHour
  supportID: String          // anonymous per-account key, PII-free
  deviceToken: String        // per-device APNs token

  // kind == "remoteFetch" only:
  endpointURL: String?
  extractionPath: String?

  // kind == "automationFire" only:
  automationID: UUID?
  targetKind: "tracker" | "checkIn"?
  targetID: UUID?
  metric: "increment" | "presenceOn" | "presenceOff"?
}
```

`remoteFetch` jobs fetch `endpointURL` at the scheduled time, pick one scalar
value out of the JSON response via `extractionPath` (a small dot-path
grammar, not a full JSONPath), and push it. `automationFire` jobs send a
bare silent push carrying `{targetKind, targetID, metric}` — no fetch, no
external network call. Every push in v1 is silent
(`content-available: 1`); the receiving app decides what, if anything, to
render.

## Architecture

Serverless end-to-end — no VM, nothing to SSH into, cost roughly tracks
actual usage. This was the explicit reason a Coolify-hosted box was dropped
in favor of AWS (see the app repo's `NOTIFICATION-SERVER-INFRA.md` §6):

```
                          ┌─────────────────────────┐
   Client app  ── PUT ──▶ │  API Gateway (HTTP API)  │
   (registers/updates     └────────────┬─────────────┘
    a job)                             │ AWS_PROXY
                                        ▼
                          ┌─────────────────────────┐
                          │  Lambda: jobs_api         │
                          │  (CRUD on the job store)  │
                          └────────────┬─────────────┘
                                        │ PutItem/GetItem/DeleteItem/Query
                                        ▼
                          ┌─────────────────────────┐
                          │  DynamoDB: jobs table     │
                          │  + gsi-identity            │
                          │  + gsi-due                  │
                          └────────────▲─────────────┘
                                        │ Query(gsi-due)
                          ┌─────────────┴─────────────┐
   EventBridge rule  ───▶ │  Lambda: run_due_jobs      │
   (rate(5 minutes))      │  - remoteFetch: SSRF-guarded│
                          │    HTTPS GET + JSON extract │
                          │  - automationFire: no fetch │
                          │  - signs an APNs ES256 JWT   │
                          │  - POSTs to APNs over HTTP/2 │
                          └───────┬───────────┬─────────┘
                                  │           │
                     GetSecretValue        HTTPS POST
                                  │           │
                                  ▼           ▼
                     ┌──────────────────┐  ┌───────────────┐
                     │ Secrets Manager   │  │ APNs           │
                     │ (.p8 key material,│  │ (Apple's push  │
                     │  populated by     │  │  gateway)      │
                     │  hand, never by   │  └───────────────┘
                     │  Terraform)       │
                     └──────────────────┘
```

**Why serverless, why this shape:**
- The workload is bursty and schedule-driven (a 5-minute poll, occasional
  registration writes) — nowhere near enough sustained traffic to justify a
  standing server, and Lambda's pay-per-invocation model fits that directly.
- Two Lambdas with disjoint, least-privilege IAM roles: `jobs_api` can only
  touch the job store; `run_due_jobs` is the only thing that can read the
  APNs secret or send a push. Neither can do the other's job.
- DynamoDB on-demand billing — no capacity planning, scales to zero between
  polls.
- A single EventBridge rate rule (not one EventBridge Scheduler schedule per
  job) drives the due-job check. Simpler to reason about and to keep in one
  repo; the tradeoff (a shared single-partition DynamoDB GSI for the "what's
  due" query) is a known v1 scaling limit — see `src/relay/store.py`'s module
  docstring for the exact ceiling and the two ways to raise it later.
- Language: Python. Small surface, first-class Lambda support, and the only
  non-stdlib pieces needed (`pyjwt[crypto]` for ES256 JWT signing, `httpx`
  for the HTTP/2 client APNs requires) are both mature, narrowly-scoped
  libraries — no framework, no ORM, nothing pulling in application shape it
  doesn't need.

## Repository layout

```
src/relay/
  models.py            Job/Schedule — the data shape, pure, no I/O
  scheduling.py         due-job math (once/hourly/everyNHours/dailyAtHour), pure
  extraction.py          JSON dot-path value picker, pure
  ssrf.py                 SSRF containment policy (scheme/IP-range checks), pure core + a
                           thin DNS-resolving wrapper
  fetch.py                 bounded, SSRF-guarded outbound GET (real network I/O)
  apns.py                   ES256 JWT signing + payload building (pure) + HTTP/2 POST
                             to APNs (real network I/O, injectable client)
  store.py                   DynamoDB access layer
  handlers/
    jobs_api.py               API Gateway Lambda entrypoint (job CRUD)
    run_due_jobs.py            scheduled Lambda entrypoint (the cron runner)
tests/                          pytest — see "What's tested" below
scripts/build_lambda_layer.sh    builds the pyjwt/httpx dependency layer
*.tf                              Terraform (see below)
```

## What's real vs. scaffolded vs. out of scope

**Real / unit-tested (120 tests, all pure logic — no AWS, no network):**
- The `Job`/`Schedule` model, validation, and JSON round-tripping.
- The due-job scheduling math for all four schedule types, including
  first-run anchoring and late-check behavior.
- The JSON dot-path extractor, including its malformed-path rejections.
- SSRF address-range classification (private/loopback/link-local/multicast/
  reserved/unspecified, including IPv4-mapped IPv6 wrappers).
- APNs ES256 JWT construction (built and independently verified against the
  matching public key in the test) and payload building (including an
  explicit test that no tracker-content field can leak into a payload).
- The DynamoDB job store's CRUD + due-job query semantics, against a mocked
  DynamoDB (`moto`) — includes a test that a fired `once` job actually drops
  out of the due set and a recurring job becomes due again after its
  interval.
- The `jobs_api` Lambda handler's routing/status-code behavior, against the
  same mocked store.

**Scaffolded but not verified end-to-end (would need real AWS + a real APNs
key + a real device to verify, all explicitly out of scope for this pass):**
- `fetch.py`'s actual outbound HTTP GET (redirect handling, size cap,
  timeout) — real network I/O, not unit-tested; the SSRF policy pieces it
  calls are.
- `apns.py`'s `send_push` over a *real* `httpx.Client(http2=True)` — the
  request-construction is tested via an injectable fake client, but no push
  has ever actually been sent to Apple's servers.
- `run_due_jobs.handle`'s end-to-end Lambda invocation (Secrets Manager read
  → fetch/skip → push → mark-ran/retire) — each piece is tested in
  isolation; the whole chain has not been run against real AWS resources.
- A *real* push actually reaching a *real* device via APNs — the
  Terraform-provisioned infrastructure has been live in AWS since 2026-07-14
  (see "Deploying" below), and `jobs_api`/`run_due_jobs` run in production on
  every scheduled poll, but nobody has yet confirmed an end-to-end send with
  a live APNs key and a live device.

**Known, documented, left as follow-up (not attempted this pass):**
- DNS-rebinding TOCTOU gap in `fetch.py` between the SSRF address check and
  the actual connection (documented in that file's module docstring).
- The `gsi-due` single-partition DynamoDB GSI's scaling ceiling (documented
  in `store.py`; fine at expected volume, has two known fixes if it isn't).
- No API-level rate limiting / abuse throttling beyond what API Gateway does
  by default — the "no auth beyond supportID+deviceToken" model means a job
  id or an identity pair being guessed is the main residual risk, mitigated
  only by UUIDv4 entropy today.

## Deploying

Live in AWS since 2026-07-14 (account `947047971987`, `datum` AWS CLI
profile) — the steps below are the same whether standing the infra up fresh
or pushing a routine code/infra change:

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Build the Lambda dependency layer (pyjwt[crypto] + httpx[http2], targeted
# at manylinux2014_x86_64 / Python 3.12 regardless of your local machine's
# platform — every dependency ships pure-Python or manylinux wheels):
./scripts/build_lambda_layer.sh

# The S3 backend doesn't inherit providers.tf's profile, so export this
# before init (not just plan/apply) or it fails with "no valid credential
# sources found":
export AWS_PROFILE=datum

terraform init
terraform validate
terraform plan

# Real, billed AWS infrastructure. If the APNs secret (below) isn't
# populated yet, registration/scheduling still work — pushes just fail at
# send time until it is.
terraform apply
```

Populate the APNs secret out-of-band (never via Terraform — see
`secrets.tf`) if it isn't already:

```sh
aws secretsmanager put-secret-value \
  --profile datum \
  --secret-id datum-relay-apns-key-dev \
  --secret-string '{"privateKeyPem":"-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"}'
```

## Running the tests

```sh
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## What's explicitly NOT in this repo

No Datum app source, no DatumKit, no design-system code, no business logic
beyond the two job kinds' bare mechanics above. The app-side registration and
push-handling code (deciding *when* to create a job, what a `metric` means to
the app, materializing an `Entry` on receipt) lives in the private `Datum`
app repo — it's a normal client feature with no transparency requirement of
its own. This repo only ever sees `{job schedule, job kind, an anonymous key,
an APNs device token}` and the bare routing fields each job kind carries.

## License

MIT — see [LICENSE](LICENSE).
