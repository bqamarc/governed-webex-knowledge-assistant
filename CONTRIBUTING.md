# Contributing

Start with [Architecture](docs/ARCHITECTURE.md). Preserve the signed-webhook, exact-space,
answerability, citation, redaction, deduplication, and ambiguous-post boundaries.

Use only synthetic fixtures. Do not submit credentials, production IDs, private source locations,
conversation exports, customer data, or restricted documents.

Before opening a pull request:

```bash
python -m pip install -e '.[dev]'
make verify
git diff --check
```

Explain the behavioral change, configuration or migration impact, security considerations, and
verification performed. A source change must document its ownership, publication permission,
review status, audience, freshness, answerability, and citation policy.
