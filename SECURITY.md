# Security policy

Whistle installs software and runs package managers with `sudo`, so security
problems matter. Thank you for reporting them responsibly.

## Reporting a vulnerability

**Please don't open a public issue.** Report it privately instead:

1. Go to the repo's **Security** tab.
2. Click **Report a vulnerability**.

This works for all three repos: [whistle](https://github.com/thisisroot/whistle/security/advisories/new),
[whistle-server](https://github.com/thisisroot/whistle-server/security/advisories/new)
and [whistle-recipes](https://github.com/thisisroot/whistle-recipes/security/advisories/new).

Include what you found, how to reproduce it, and what an attacker could do with it.
You'll get an answer within a week. Once it's fixed, we'll publish an advisory
and credit you, unless you'd rather stay anonymous.

## What counts

Especially:

- Making the client run a command, or touch a file, that the catalog or a
  recipe shouldn't be able to reach: shell injection, path traversal, option
  injection into a package manager.
- Getting an unverified or tampered download or catalog accepted.
- A recipe that escapes its Wine prefix.
- Anything that leaks secrets from the publishing workflow.

A third-party app that Whistle installs having a vulnerability of its own is
for that app's developers, not us. But if the catalog points to a malicious or
wrong package, report it here.

## Supported versions

Only the latest release gets security fixes.
