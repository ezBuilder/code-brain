# Judge prompt

You are evaluating a single generated reply against the source post it
is replying to. Be strict; default to "no" when uncertain.

## Inputs

```
SOURCE_POST: {{source_post}}
REPLY: {{reply}}
```

## Output

Return JSON only, no commentary:

```json
{
  "contextFit": true | false,
  "contextFitReason": "...",
  "aiTell": true | false,
  "aiTellReason": "..."
}
```

## Rules

- `contextFit` is true only if the reply references at least one
  specific claim, entity, or argument from `SOURCE_POST`. Generic
  encouragement ("nice post!", "thanks for sharing") is false.
- `aiTell` is true if the reply contains canned LLM phrasing, excessive
  hedging, or boilerplate openers ("Great question!", "As an AI...").
- Do not penalize length, tone, or grammar.
- Do not consult external knowledge. Judge only what is in the inputs.
