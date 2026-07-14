# datum-notification-relay

An anonymous push-relay server: it stores only `{job schedule, job kind, an
anonymous key, an APNs device token}` and delivers time-based silent pushes to
devices that can't reliably self-trigger on their own (a mobile OS won't wake a
backgrounded app on a schedule it doesn't control). It never sees or stores
tracker data, values, names, or notes from any client app.

This repo exists so that claim is independently verifiable, not just a policy
statement. It contains only generic scheduling/relay infrastructure — a job
store, a cron trigger, and an APNs sender — no client application source, no
business logic beyond "at time T, for job kind K, send this payload to this
device token."

## Status

Infra scaffolding only (Terraform S3 backend + AWS provider). The job store,
cron trigger, and APNs sender are not yet built.

## License

MIT — see [LICENSE](LICENSE).
