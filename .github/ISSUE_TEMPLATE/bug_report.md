---
name: Bug report
about: Report unexpected behaviour, crashes, or incorrect classification
title: "[bug] "
labels: ["bug"]
---

## Summary

A clear, concise description of what went wrong.

## Reproduction

Steps to reproduce the behaviour:

1. Configuration relevant to the issue (TOML snippet, redacted)
2. Command(s) executed
3. Observed output (NDJSON event or stderr line)

## Expected behaviour

What you expected `rcd` to do instead.

## Environment

- `rcd` version: <output of `rcd version`>
- `distribution` version: <e.g. v3.1.1>
- Deployment: standalone / pull-through cache
- OS / Docker version
- Redis version, `maxmemory`, `maxmemory-policy`

## Additional context

Anything else that helps debug the issue: storage backend, NFS, scale,
related upstream issues, etc.
