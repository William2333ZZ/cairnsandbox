# User Guide

For people who **use** the sandbox cloud: tenants, the agents they connect, and anyone writing SDK calls. The design and its rationale live in the numbered documents one level up (start from the [docs/README](../../README.md) (Chinese)), the interface contract is in [CONTRACT.md](../../../CONTRACT.md), and this guide is the version organized by task and by endpoint.

| Read | For whom |
| --- | --- |
| [Tenant manual](manual.md) | People who use the workbench console to manage projects, members, sandboxes, egress, volumes, billing, and audit |
| [API reference](api/README.md) | People who call HTTP directly: organized into eight contexts, one section per endpoint, with examples |
| [Python SDK](sdk.md) | People integrating through code; includes a mapping to the E2B SDK |
| [JS/TS SDK](sdk-js.md) | People integrating from Node / Deno / Bun / the browser; differences from the Python SDK |
| [Going to production](production.md) | People deploying it for external users: reverse proxy, domains, environment variables, launch checklist |
| [MCP](mcp.md) | Letting agents such as Claude Code / Codex use sandboxes as tools |
| Tutorials: [Connect with Claude Code](tutorials/claude-code-mcp.md) · [Migrate from E2B](tutorials/migrate-from-e2b.md) · [Use your own image as a template](tutorials/custom-template.md) · [Budgets, tickets, and reports](tutorials/budget-and-ticket.md) | People getting started for the first time |
| [Versioning and compatibility commitments](compatibility.md) · [Changelog](../../../CHANGELOG.md) | People who need to upgrade or depend on interface stability |
| [Operations manual](operations.md): runbooks, metrics and alerts, backup and restore drills, capacity planning | On-call and operations staff |
| [Deployment](../../../deploy/README.md) · [Numbers](../../research/benchmark-2026-11.md) · [Threat model](../../013-threat-model.md) (Chinese) | Operators and reviewers |

The writing rules are the same as for the numbered documents (end of docs/README): every endpoint, field, and button follows CONTRACT and the code; this guide does not get ahead of them.
