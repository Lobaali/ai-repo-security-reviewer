# AI Repository Security Reviewer

A command-line tool that statically reviews a public GitHub repository for
potential security issues using two collaborating LLM agents, verifies every
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


## Configuration

- **Secrets:** copy `.env.example` to `.env` and set `OPENAI_API_KEY`. `.env` is git-ignored.
- **Settings:** copy `config.example.yaml` to `config.yaml` (git-ignored) and adjust.
  Without a config file, the built-in defaults (identical to the example) are used.
  Unknown keys and invalid values are rejected at startup.

## Processing limits

Every limit is set in `config.yaml`. The defaults are reasoned starting values,
not proven optima; they should be tuned using the measured numbers from real runs.
When any limit reduces coverage, the report marks the review `partial` and lists
what was skipped and why.

**Why chunks of 20,000 characters?** Code averages roughly 3-4 characters per
token, so one chunk is about 5-7k tokens. Models accept much more, but:
- attention to detail drops in very long inputs, and security bugs are often a single line;
- the chunk shares the context with the prompt, tool results and the JSON answer;
- a failed request loses one small chunk instead of a large part of the repository.

The cost is more model calls and no direct view of other files; the agents'
read-only tools (`search_code`, `read_file`) compensate for the second.

| Limit | Default | Reasoning | Raising it means |
|---|---|---|---|
| `chunk_chars` | 20,000 | ~5-7k tokens: focused attention, room for tool output | Fewer calls, less attention per line |
| `max_total_chars` | 400,000 | ~100-130k tokens of code ≈ 20 chunks, a mid-sized project | More coverage, more cost |
| `max_files` | 40 | Files are ranked by risk, so the most relevant are reviewed first | Coverage of lower-risk files |
| `max_file_bytes` | 100,000 | ~2,500 lines; larger files are usually generated, vendored or minified | Budget spent on generated code |
| `max_model_calls` | 60 | Hard ceiling on cost and runtime for one review | Higher cost, fewer partial reviews |
| `investigator_max_tool_calls` | 3 | Enough to look up a function or two per chunk | Deeper but slower scanning |
| `reviewer_max_tool_calls` | 5 | Tracing where data comes from takes more steps | Better verdicts, more calls |
| `tool_output_max_chars` | 4,000 | ~100 lines, enough to show a whole function | Context fills faster |
| `request_timeout_seconds` | 60 | Reasoning models can take tens of seconds; a minute catches real hangs | Slower failure detection |
| `max_retries` | 2 | Rides out brief outages and rate limits without hammering the API | Longer worst-case waits |

**Call budget arithmetic.** Each tool round is one extra model call. Per chunk,
the Investigator uses 1-4 calls and the Reviewer 0-6 (it only runs when there
are valid candidates):

| Case | Calls per chunk | 20 chunks need |
|---|---|---|
| Best (nothing suspicious, no tools) | 1 | 20 |
| Typical (estimate, to be replaced by measurements) | ~2-4 | 40-80 |
| Worst (every agent uses every tool call) | 10 | 200 |

The default of 60 covers a typical mid-sized repository but not the worst case.
This is deliberate: it is a cost ceiling, and reaching it is reported, never hidden.