# AI Repository Security Reviewer

A command-line tool that statically reviews a public GitHub repository for
potential security issues using two collaborating agents, verifies every
piece of evidence with deterministic code, and outputs a JSON report.

> Work in progress. Sections below are filled in as the project is built.

## Scope

- Reviews **Python (`.py`) files only**.
- **Static review only**: repository code is never executed, installed, or run.

## Severity levels

| Level | Meaning |
|---|---|
| `critical` | Directly exploitable with severe impact (e.g. remote code execution, auth bypass) and little or no precondition. |
| `high` | Exploitable with serious impact, but needs some precondition (e.g. command/SQL injection behind a login). |
| `medium` | Weakens security or exploitable only in specific conditions (e.g. weak hashing, verbose errors leaking internals). |
| `low` | Best-practice issue with limited direct impact (e.g. missing timeout on an outbound request). |