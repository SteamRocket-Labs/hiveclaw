---
name: Finance Research
description: Disabled finance research entrypoint retained for catalog completeness until live provider-backed runtime is product-ready.
tools:
  - finance_get_provider_status
metadata:
  version: "0.2"
  category: finance
  hive.pack: finance_pack
---

# Finance Research

This pack is not a default runtime skill. Do not expose it to agents until the
finance runtime has real provider coverage, tenant-scoped credentials, and
production acceptance tests for current market data.

Use `finance_get_provider_status` only for operator diagnostics.
