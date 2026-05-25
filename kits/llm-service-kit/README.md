# llm-service-kit

Templates for shipping Claude-backed features in production (Navio,
internal tools, etc.). Code Brain itself is a dev-tool; these templates
are for the consuming product side.

## When to reach for this kit

You are building a service that calls the Claude API at runtime
(comment generation, summarization, classification, structured
extraction) and you need:

- JSON output your code can parse without regex hacks → Structured Outputs
- Cost-efficient bulk processing → Batch
- Repeatable quality measurement → Evals
- Tool use that lets Claude call your own functions safely → Tool Use

## Layout

- `structured_outputs/` — JSON schema templates for common shapes
  (comment-generation, content-classification, risk-scoring).
- `batch/` — async batch request shapes and result-collection helpers.
- `tool_use/` — client-tool definition skeletons + the strict-mode flag.
- `evals/` — service-level eval framework (separate from Code Brain's
  internal evals under `.ai/evals/`).

## What this kit does **not** do

- Does not ship an Anthropic API key or wrap the SDK.
- Does not run at Code Brain CLI time.
- Does not depend on `.ai/`.

Copy the relevant template into the consuming repo, adapt the schema,
wire the SDK call there. The kit is a starting point, not a runtime.
