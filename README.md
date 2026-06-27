# MemTraceHarness

External harness for MemTrace-backed model workflows.

This repo starts with a deliberately thin v1:

- normalize work into a `TaskEnvelope`
- run one or more model adapters
- store a replayable SQLite trace
- produce structured claims and conflict records
- optionally write a draft-only node back to MemTrace

The first adapter is a deterministic mock. Real Claude / Gemini / OpenAI adapters should plug into the same interface later.

## Install

```powershell
cd D:\Workspace\MemTraceHarness
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

## Dry Run

```powershell
.\.venv\Scripts\python -m memtrace_harness.cli run `
  --workspace ws_spec_plan `
  --goal "Review whether the harness product boundary is clear" `
  --context-ref mem_f178ea8d
```

After install, the shorter module form also works:

```powershell
.\.venv\Scripts\python -m memtrace_harness run --workspace ws_spec_plan --goal "Review harness boundary"
```

This writes a local trace to:

```text
data/harness.sqlite3
```

## Draft Writeback

Set MemTrace connection details first:

```powershell
$env:MEMTRACE_MCP_URL = "http://localhost:8000/api/v1/mcp/mcp"
$env:MEMTRACE_API_TOKEN = "<token>"
```

See `.env.example` for the expected variables.

Then run:

```powershell
.\.venv\Scripts\python -m memtrace_harness.cli run `
  --workspace ws_spec_plan `
  --goal "Review whether the harness product boundary is clear" `
  --context-ref mem_f178ea8d `
  --writeback
```

Writeback is draft-first. The v1 harness should not automatically resolve inquiries, create `answered_by` edges, or promote model consensus into formal knowledge.

## Shape

```text
src/memtrace_harness/
  adapters/          model adapter interface and mock adapter
  cli.py             command-line entrypoint
  config.py          env-driven configuration
  memtrace_client.py MCP JSON-RPC client for writeback
  runner.py          orchestration loop
  schemas.py         TaskEnvelope / ModelResponse / ConflictRecord
  trace_store.py     SQLite trace persistence
```

## Verify

```powershell
$env:PYTHONPATH = "src"
python -m compileall src
python -m unittest discover -s tests
```
