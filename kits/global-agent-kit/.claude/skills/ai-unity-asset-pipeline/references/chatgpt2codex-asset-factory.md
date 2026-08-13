# ChatGPT To Codex asset factory

This workflow lets ChatGPT generate immutable GPT Image 2 references and then
hand them to the reusable Blender/Unity pipeline. MCP tools cannot be called by
a shell script, so `ai-unity-asset prompt-batch` emits the deterministic queue
that the `ai-unity-asset-pipeline` skill consumes.

## Required ChatGPT To Codex sequence

1. Select the Unity project with `project_select`, preset `full-write`, and
   retain the returned lease ID for every later call.
2. Read `asset-image-batch.json`. For each `prompt-ready` job, call
   `chatgpt_image_generate` with its exact prompt, destination, idempotency key,
   and `requestedMode=very-high`.
3. End that ChatGPT turn after queueing. On a later connector turn, call
   `chatgpt_image_status` with the exact generation ID. Only accept a result
   whose image bytes, PNG MIME type, exact 2048x2048 dimensions, and SHA-256
   were validated and imported into the leased project. A smaller UI-generated
   square is visual QA only and must not enter mesh generation.
4. Preserve the PNG, prompt, sidecar, generation receipt, and provenance
   together. Reject inconsistent multiview sheets before any mesh work.
5. Request `gpt-5.6-sol` at `xhigh` reasoning when the connected ChatGPT/IDE
   surface exposes those options. The build agent must reuse an existing
   catalog recipe when possible. It may add one reusable Blender recipe for a
   genuinely new topology family; it must not create one script per asset.
6. Obtain a raw mesh only through an already authorized provider, or create it
   through the approved deterministic Blender recipe. Never infer that a paid
   provider or credential is authorized merely because a queue exists.
7. Run `ai-unity-asset optimize`, `validate`, and `publish`. Verify front,
   side, rear, and elevated Unity renders plus runtime scale, pivot, wheels,
   forward axis, LODs, material count, and collision clearance.

## Complete 1950-2999 transport loop

Run `ai-unity-asset transport-factory` once. Consume
`chatgpt2codex-image-queue.jsonl` first, then
`chatgpt2codex-build-queue.jsonl`. The gameplay source of truth is
`transport-gameplay-catalog.json`; never invent speed, capacity, cost,
reliability, hazard, energy, or infrastructure values in the build worker.

Each geometry variant has three independent reconstruction inputs: clean
`base`, repairable `damaged`, and extinguished `wrecked`. They must preserve
model identity. Day/night, breakdown, smoke, leaks, fire, sparks, explosion,
wake, contrail, rotor wash, and similar transient states come from the shared
`transport-vfx-pack.json`, not extra vehicle meshes.

Required asset sockets are `fx-engine`, `fx-power`, `fx-cargo`,
`fx-running-gear`, `fx-exhaust`, and `fx-center-of-mass`. A job cannot reach
`unity-verified` until the applicable sockets, gameplay model ID, Addressables
era label, multiview renders, scale, pivot and LODs have been checked.

## Job state machine

`prompt-ready -> image-queued -> image-ready -> mesh-ready -> optimized -> published -> unity-verified`

Failed or rejected output stays immutable and receives a new attempt record.
Never overwrite a previously accepted reference. Do not commit, push, spend
provider credits, or enable unrestricted Blender MCP scripting without the
operator's explicit approval.

## Build goal template

```text
Use the ai-unity-asset-pipeline skill. Consume one accepted queue job and its
validated local image. Requested coding model: gpt-5.6-sol. Reasoning: xhigh.
Reuse the catalog recipe and modules first. Produce a bottom-center, meter-scale,
+Z-forward asset with LOD0/1/2, consolidated PBR materials, FBX and GLB. Publish
to the job's Unity Addressables era label. Run manifest validation, Unity compile,
EditMode, PlayMode, and fresh multiview render proof. Preserve source/provenance.
```
