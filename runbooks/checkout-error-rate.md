---
title: Checkout service elevated error rate
tags: [checkout, http_5xx, error_rate, revenue]
---

# Checkout service elevated error rate

## Symptoms
- 5xx rate on `checkout` service > 5% sustained for 3+ minutes
- Cart submission API latency p95 > 2s

## First actions (in order)
1. Confirm the alert in Datadog dashboard `checkout-golden-signals`.
2. Check the last 3 deploys via `deploy-status checkout`. If a deploy is <15 min old and matches the spike, roll back with `deploy rollback checkout --confirm`.
3. Verify Redis pricing cache health — `redis-cli --stat` against `pricing-prod`.
4. If Redis is degraded, fail open to origin pricing: `feature-flag set checkout.pricing_cache off`.

## Escalation
- Page `#pay-oncall` if error rate persists 10 minutes after rollback.
- Loop in `@payments-lead` for any incident affecting revenue > $10k/min.

## Automated actions
```json
[
  {"name": "check redis health", "command": "redis-cli --stat", "auto": false},
  {"name": "flip pricing cache off", "command": "feature-flag set checkout.pricing_cache off", "auto": true},
  {"name": "rollback last checkout deploy", "command": "deploy rollback checkout --confirm", "auto": false}
]
```

## Common causes seen historically
- Bad pricing cache deploys (see PM-2024-07-19)
- Redis maxmemory eviction under load spikes
- Downstream tax service timeouts
