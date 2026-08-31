# H7 Evidence Loop Summary

Status: FAIL
Generated at: 2026-04-28T10:05:39.693853+00:00
Base URL: https://backend-production-326d.up.railway.app

## Totals

```json
{
  "run_dir": "/Users/example-owner/vc-saas/hiveclaw-main/tmp/reports/h7/2026-04-28-180538",
  "status": "FAIL",
  "endpoint_errors": {},
  "autonomous_audit_totals": {
    "agents": 33,
    "findings": 2,
    "errors": 2,
    "warnings": 0,
    "infos": 0,
    "by_category": {
      "trigger_runtime_gap": 2
    }
  },
  "autonomous_audit_findings": 2,
  "autonomous_audit_severity_counts": {
    "error": 2
  },
  "autonomy_repair_totals": {
    "agents": 33,
    "audit_findings": 2,
    "actions": 2,
    "auto_applyable_actions": 0,
    "manual_actions": 2,
    "by_action_type": {
      "verify_runtime_ledger_after_deploy": 2
    },
    "by_risk": {
      "low": 2
    }
  },
  "autonomy_repair_actions": 2,
  "autonomy_repair_auto_apply_actions": 0,
  "harness_validation_totals": {
    "agents": 33,
    "findings": 0,
    "errors": 0,
    "warnings": 0,
    "infos": 0,
    "by_category": {},
    "h4": {
      "long_tasks": 32,
      "passed": 32,
      "failed": 0,
      "validation_reports_present": 32
    },
    "h5": {
      "ledgers_present": 32,
      "validation_reports_present": 32,
      "passed": 32,
      "failed": 0
    }
  },
  "harness_validation_findings": 0,
  "harness_validation_severity_counts": {},
  "fail_reasons": [
    "autonomous_audit_error_or_critical_findings"
  ]
}
```

## Decision

The 24h window failed H7 gate. Fix endpoint/token errors or reported critical findings before relying on the evidence loop.

## Saved Files

- autonomous-audit-24h.json
- autonomy-repair-plan-24h.json
- harness-validation-24h.json
