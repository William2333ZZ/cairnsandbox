# Security policy

CairnSandbox runs untrusted code in microVMs, so we take isolation bugs seriously.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private vulnerability reporting on this repository
("Security" tab -> "Report a vulnerability"; the issue chooser also links to it). If that is unavailable, open a public issue that says only
that you have a security report and asks for a private channel, with **no details**; a maintainer will reply with one.
Include what you did, what you expected, what happened, the version or commit, and whether the host or another tenant was affected.

We aim to acknowledge a report within 3 working days and to tell you how we plan to proceed within 10. We will credit you in the fix
unless you ask us not to. Please give us a reasonable time to ship a fix before you disclose.

## Scope

In scope: guest-to-host escape, one sandbox reaching another sandbox's memory, disk or network, bypass of the egress policy or its owner approval,
cross-tenant data access or privilege escalation in the control plane, token or secret leakage, tampering with the audit chain.
Out of scope: denial of service by a tenant against their own quota, findings that need root on the host, issues in third-party software
that we only run (report those upstream, and tell us if our configuration makes them worse).

## Status, stated plainly

This project has **not** had an external penetration test or a third-party security audit. Its threat model is written down
(`docs/013-threat-model.md`) and the controls are exercised by the test suite, but those are our own claims. Do not use it as the only barrier
between untrusted public tenants and anything valuable until it has been independently reviewed. Hardening details and the known gaps are in
`docs/009-security-and-data.md` and `ROADMAP.md`.

## Supported versions

Only the latest release and the `main` branch receive security fixes while the project is at 0.x.
