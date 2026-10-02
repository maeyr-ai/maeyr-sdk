# SDK runbook

For a public-client incident, record library version, request method, endpoint class, timeout, retry count, and typed error without logging auth headers or body secrets. Reproduce with an idempotent call before changing retry settings.

For private tracing saturation, inspect in-flight/dropped counters, sink health, queue age, and recorder drain behavior. If a sink is slow, restore sink capacity or reduce sampling; avoid raising task limits without a memory profile. Verify consumer services are pinned to the intended private-runtime SHA before rollback or rollout.

The PyPI release workflow runs public and private test/lint gates, dependency audit, distribution verification, then publish. Backend image workflows pin reviewed runtime SHAs independently. No production incident or rollback was exercised during this audit.
