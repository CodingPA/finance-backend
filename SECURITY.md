# Security Policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them
privately through GitHub instead:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Describe the issue, the affected file or endpoint, and the steps to
   reproduce it.

Only the maintainer can see the report. Never include real API keys or
tokens in a report -- yours or anyone else's.

This is a personal project maintained in spare time, so responses are
best-effort, and there is no bug bounty.

## Supported versions

There are no releases. Only the latest commit on `main` is supported --
please check that the problem still exists there before reporting.

## Scope

In scope: the code in this repository. For example, a way around the
sign-up allowlist or authentication, access to another user's data,
injection, or anything that leaks API keys or market data to someone who
shouldn't see it.

Out of scope:

- Deployments that ignore the README's "Crucial Security & ToS Compliance
  Notice", such as one with public registration opened up. Report those
  to whoever runs that instance.
- Bugs in third-party services this project calls (Tradier, SEC EDGAR,
  FMP and the others in the README). Report those to the provider.

**Test against your own local copy only.** Don't probe anyone's live
deployment, including the maintainer's.
