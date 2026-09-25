# Deployment

## Runtime roles

The image exposes three roles:

- `webex-knowledge-assistant migrate` applies the public baseline schema.
- `webex-knowledge-assistant api` receives and durably enqueues signed webhook events.
- `webex-knowledge-assistant worker` leases jobs, retrieves reviewed evidence, and posts replies.

Run migrations before API or worker startup. Hosted API and worker instances must share PostgreSQL.
SQLite is suitable only for development and single-process tests.

The wheel is the application-code layer. The source distribution and container also carry
`alembic.ini`, migrations, and the synthetic manifest. When installing only the wheel, provide an
equivalent Alembic working directory and set `KNOWLEDGE_MANIFEST` to an operator-managed file.

## Required production configuration

| Variable | Migrate | API | Worker | Purpose |
|---|---:|---:|---:|---|
| `ENVIRONMENT=production` | Yes | Yes | Yes | Enables production preflight |
| `DATABASE_URL` | Yes | Yes | Yes | Shared PostgreSQL connection URL |
| `KNOWLEDGE_MANIFEST` | No | Yes | Yes | Reviewed manifest path inside the artifact |
| `ALLOWED_WEBEX_SPACE_IDS` | No | No | Yes | Exact comma-separated group-space allowlist |
| `ALLOWED_WEBEX_PERSON_IDS` | No | No | Optional | Exact sender allowlist when direct messages are enabled |
| `WEBEX_BOT_PERSON_ID` | No | No | Yes | Bot identity for self-message and mention checks |
| `WEBEX_BOT_TOKEN` | No | No | Yes | Bot API credential, supplied at runtime |
| `WEBEX_WEBHOOK_SECRET` | No | Yes | No | Secret used to verify the raw Webex webhook body |
| `WEBEX_API_BASE_URL` | No | No | Optional | Webex API URL; production is pinned to `https://webexapis.com/v1` |

The Compose definition follows this least-privilege split. Do not replace it with one shared
environment block: migration does not need Webex secrets, the API does not need a bot token, and
the worker does not need the webhook verification secret.

Direct messages are disabled by default. Enabling them also requires a non-empty exact person
allowlist. Define the audience, retention, and support policy before doing so. Do not use wildcard
space or person policies.

Define a retention job for completed/dead-letter delivery metadata and processed-message
deduplication rows. The application does not persist fetched message text, but runtime identifiers
can still be sensitive and are not pruned automatically in version 0.1.

## Delivery outcomes

The worker retries requests that are known not to have been accepted: a connection failure before
send and explicit rate limiting. Read-only fetch failures are also safe to retry. A `Retry-After`
value is bounded to five minutes before it is used. Permanent client rejections are dead-lettered
without retaining a duplicate-suppression claim. A transport read/write failure, `408`, or `5xx`
response during message creation is different: Webex may already have accepted the reply, so that
message is marked ambiguous and is not automatically replayed. Reconcile ambiguous outcomes
manually before changing their state.

## Network boundary

The API needs inbound HTTPS from Webex only at `/webhooks/webex`. The worker needs outbound HTTPS
to `webexapis.com`. PostgreSQL should be reachable only by the application roles. Health endpoints
must not reveal configuration values, source content, space IDs, or credentials.

## Webhook lifecycle

1. Deploy an immutable reviewed revision.
2. Apply migrations and verify `/readyz`.
3. Register or update one owned Webex webhook using an exact HTTPS target and a secret.
4. Verify the returned target, resource, event, filter, and active state.
5. Run one authorized synthetic message through the intended space.
6. Keep the previous revision and webhook target available for rollback.

Webhook registration and deployment are separate from publishing source code. This repository does
not create a bot identity, webhook, hosted service, or secret.

## Rollback

Restore the previous immutable application revision. If the webhook target changed, restore the
recorded previous target only after its readiness check passes. Do not automatically replay jobs in
an ambiguous-post state; reconcile them first.
