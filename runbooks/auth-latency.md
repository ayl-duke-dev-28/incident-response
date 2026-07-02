---
title: Auth service latency spike
tags: [auth, latency, login]
---

# Auth service latency spike

## Symptoms
- Login p95 latency > 1.5s
- Increase in `token_exchange_slow` metric

## First actions
1. Check DB connection pool utilization on `auth-primary`.
2. Verify session store (Redis) latency.
3. Roll back any recent auth deploys within the last hour.

## Escalation
- Page `#identity-oncall` after 10 minutes.
