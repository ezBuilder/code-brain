$Root = Resolve-Path "$PSScriptRoot/../.."
uv run --project "$Root/.ai/runtime" ai hook @args

