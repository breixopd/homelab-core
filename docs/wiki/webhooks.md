# Webhooks

## Grafana alert receiver

`POST /api/webhooks/grafana-alert` accepts signed Grafana alerts and queues a
durable, narrowly scoped controller job for each affected service. The UI proxy
does not run Docker or recovery commands itself.

The deployment provisions Grafana's contact point automatically. It signs the
raw JSON body with the generated `GRAFANA_WEBHOOK_HMAC_SECRET` and sends:

- `X-Grafana-Alerting-Signature`: lowercase HMAC-SHA256 digest
- `X-Grafana-Alerting-Signature-Timestamp`: Unix timestamp used in the digest
- `Content-Type: application/json`

The signed message is `timestamp + ":" + raw_body`. Requests are rejected when
the secret is unavailable, the signature is invalid, the timestamp is more than
five minutes from controller time, the content type is wrong, or the body is
empty or larger than 64 KiB. The public receiver is additionally rate and
concurrency limited.

Only firing alerts carrying a valid `homelab_service` label are actionable.
The controller accepts at most eight distinct service targets per request,
deduplicates replayed submissions, and checks the live inventory and the
service manifest's `restart_policy` again before healing.

### Responses

Accepted jobs return HTTP 202 with durable job receipts:

```json
{
  "outcome": "queued",
  "reason": "",
  "jobs": [
    {
      "service": "sonarr",
      "job_id": "01J...",
      "replayed": false
    }
  ]
}
```

Payloads without firing service labels return HTTP 200 with
`"outcome": "ignored"`. Authentication failures return 401, invalid payloads
return 422, rate limits return 429, and unavailable controller or secret state
returns 503. Responses never contain secret values or raw recovery logs.

### Remedy behavior

The worker re-observes the named service immediately before acting. Healthy
services are skipped. An unhealthy service is eligible only when its plugin
manifest declares `restart_policy: safe`; targeted webhook remedies cannot
start dependencies, invoke structured multi-service heals, or trigger broad
node recovery.

Every attempted restart is health-verified. A failed or unverified restart
fails the controller job, while backoff is recorded as deferred. Job events and
the audit view expose the result without requiring access to container logs.

## Related

- [Homelab UI and operations](homelab-ui.md)
- [Operations and deployment](operations.md)
