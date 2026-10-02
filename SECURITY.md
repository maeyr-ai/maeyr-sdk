# SDK security

The public client validates configuration and uses per-call timeouts and idempotency requirements for retried mutations. The private runtime centralizes signed internal requests, tenant identity, encryption helpers, strong-key guards, and safe tracing boundaries. Backend services remain responsible for enforcing authorization at their own entrypoints.

The public PyPI wheel must not contain private runtime code. `scripts/verify_public_sdk_distribution.py` checks the built distribution. Release CI tests both packages and audits their declared runtime dependencies plus the public MCP optional extra before publish. On 2026-10-02, pinned `pip-audit==2.10.1 --strict` found no known vulnerabilities in the resolved release set. A separate direct-minimum audit found no known advisories after raising the Pydantic, aiohttp, FastAPI, and MCP floors. The private runtime's logger floor is now 4 because earlier versions lack the imported formatter module. MCP is capped below 2 because its current major release changes APIs used by this SDK.

Avoid storing bearer tokens, internal signing keys, tenant secrets, or request bodies in logs, traces, examples, or exceptions. No penetration test, live secret-rotation exercise, remote release workflow result, or consumer network-policy verification was observed. Dependency advisory data can change after the local audit.
