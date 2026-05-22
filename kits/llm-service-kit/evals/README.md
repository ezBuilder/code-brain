# Service-level evals

Distinct from Code Brain's internal evals (`.ai/evals/` in the host
repo). These measure your *product* — the comments your service
generates, the classifications it emits — not the dev tool.

## Minimum first axes for a generator

1. **Spam rate** — fraction of replies a downstream moderator flags or
   the platform shadow-bans.
2. **Context-fit rate** — sampled rate of replies that actually
   reference the source post (vs. generic boilerplate).
3. **Duplicate rate** — fraction of replies that are near-duplicates of
   replies in the last 7 days (Jaccard / embedding-cosine).
4. **AI-tell rate** — fraction containing telltale phrasing
   ("as an AI", "I'm happy to help", overuse of em-dashes, etc.).
5. **Policy violation rate** — banned-word hits, link policy, length
   policy.

Score weekly. Track the trend, not the absolute number.

## Files

- `axes.yaml` — definition of each axis and how it's measured.
- `judge_prompt.md` — prompt for the LLM-as-judge calls (context-fit,
  AI-tell). Keep the judge model the same across a measurement window;
  switching judges mid-window invalidates the trend.
