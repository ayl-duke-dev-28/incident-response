---
title: Redis connection storm
tags: [redis, connections, cache, infra]
---

# Redis connection storm

## Symptoms
- Redis `connected_clients` > 8000
- Application logs show `ConnectionPool: Too many connections`

## First actions
1. Identify the offending service via `redis-cli client list | awk '{print $3}' | sort | uniq -c | sort -rn`.
2. If a single service dominates, scale it in place: `kubectl rollout restart deploy/<service>`.
3. Confirm the connection pool config in the service — `max_connections` should never exceed 100 per pod.

## Escalation
- Page `#infra-oncall` if restart does not resolve within 5 minutes.
