---
name: Primary Market Due Diligence
description: Primary-market company registry, funding, KYC, and IC memo workflow
tools:
  - finance_run_workflow
  - finance_get_company_registry
  - finance_get_funding_rounds
  - finance_get_provider_status
---

# Primary Market Due Diligence

Use `finance_run_workflow` with workflow primary-market-due-diligence. If paid
registry or funding providers are missing, call `finance_get_provider_status`
and report the configuration gap.
