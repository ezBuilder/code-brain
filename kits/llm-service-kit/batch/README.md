# Batch templates

Use the Anthropic Message Batches API for bulk, non-interactive
workloads:

- Nightly summarization of blog posts collected during the day.
- Bulk classification of historical content for backfill.
- Eval suites with hundreds of cases.

**Do not** use batch for live comment generation, on-demand chat, or
anything a user is waiting on — batch turnaround is "usually under an
hour" but not guaranteed.

## Files

- `request_template.jsonl` — one request per line, with `custom_id` you
  use to correlate results back to your DB rows.
- `submit.py` — minimal submission stub (no SDK calls; replace the
  TODO).
- `collect.py` — polls a batch id and writes results to
  `.batch_results/<batch_id>.jsonl`.

## Cost note

Batch is ~50% cheaper than the same calls made one-at-a-time on the
standard API. If your job is "process N posts, no rush", default to
batch.
