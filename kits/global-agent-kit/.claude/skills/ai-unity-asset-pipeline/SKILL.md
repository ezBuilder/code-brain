---
name: ai-unity-asset-pipeline
description: Build, install, run, and verify a reusable image-to-game-ready-3D pipeline for Unity with controlled ImageGen references, approved AI mesh providers, Blender MCP/CLI, modular era catalogs, Blender retopology/LOD/pivot processing, and Unity import/render proof. Use for 2D-to-3D assets, vehicles, aircraft, ships, buildings, industries, Blender automation, or cross-project Unity asset factories.
---

# AI to Unity asset pipeline

Use the installed `ai-unity-asset` command. Keep provider generation, mesh
optimization, Unity import, and live visual proof as separate evidence lanes.

## Required flow

1. Run `ai-unity-asset doctor --project "$PWD"`.
2. For a new concept, run `ai-unity-asset prompt` and use ImageGen to create
   four orthographic views on a plain background. Preserve the generated image,
   prompt, and fingerprint sidecar as immutable references. Read
   [references/imagegen-prompts.md](references/imagegen-prompts.md) for fixed
   prompt fields and bulk rules.
   For era production, run `ai-unity-asset prompt-batch`; when ChatGPT To Codex
   is available, consume its queue exactly as documented in
   [references/chatgpt2codex-asset-factory.md](references/chatgpt2codex-asset-factory.md).
3. Generate a mesh only through a provider the user has authorized. Never read
   or print API keys. Paid providers and gated model access require explicit
   approval.
4. Run Blender normalization: bottom-center pivot, meters, UV preservation,
   material consolidation, LOD0/LOD1/LOD2, full wheel clearance, and FBX plus
   GLB export.
5. Use `ai-unity-asset catalog` for broad 1950-2999 production. Add an
   archetype record whenever an existing modular recipe fits; add Python only
   when a genuinely new topology recipe is required.
6. Publish into Unity with `ai-unity-asset publish` or use the complete
   `ai-unity-asset fleet` flow for the bundled Transport Tycoon vehicle family.
7. Verify the manifest, triangle budgets, Unity compilation/tests, and fresh
   front, rear, side, and elevated Unity renders before claiming success.

## Commands

```bash
ai-unity-asset doctor --project /absolute/unity/project
ai-unity-asset init --project /absolute/unity/project
ai-unity-asset prompt \
  --asset-id road-city-bus-1988-kr \
  --subject "full-size two-axle city bus" \
  --year 1988 \
  --output /absolute/prompts/road-city-bus-1988-kr.txt
ai-unity-asset prompt-batch \
  --start-year 1950 --end-year 2999 \
  --domain road --region east-asia \
  --output-dir /absolute/output/road-reference-batch
ai-unity-asset gameplay-catalog \
  --start-year 1950 --end-year 2999 \
  --output /absolute/output/transport-gameplay-catalog.json
ai-unity-asset transport-factory \
  --start-year 1950 --end-year 2999 \
  --region global-neutral \
  --output-dir /absolute/output/transport-factory \
  --unity-project /absolute/unity/project
ai-unity-asset fleet \
  --reference /absolute/reference.png \
  --output-dir /absolute/output \
  --unity-project /absolute/unity/project \
  --unity-destination Assets/Resources/Game/Vehicles/Generated
ai-unity-asset optimize --input /absolute/raw.glb --output-dir /absolute/output
ai-unity-asset validate --manifest /absolute/output/asset-manifest.json
ai-unity-asset catalog \
  --start-year 1950 --end-year 2999 \
  --output-plan /absolute/output/asset-production-plan.json
```

The fleet generator is a deterministic, reference-matched production fallback;
it does not pretend to be arbitrary image inference. For arbitrary subjects,
pass the mesh produced by an authorized AI provider to `optimize`.

`prompt-batch` requests GPT Image 2 high-quality 2048-square PNG references,
ChatGPT image mode `very-high`, and an asset build agent profile of
`gpt-5.6-sol` with `xhigh` reasoning. These are requested settings: the
connected ChatGPT/IDE surface remains authoritative about model availability.
The queue does not authorize provider spend or credentials.

`transport-factory` is the complete transport-only production entrypoint. It
resolves 67 archetypes into era-specific gameplay records, creates separate
`base`, `damaged`, and `wrecked` mesh-reference jobs, emits ChatGPT To Codex
image and asset-build queues, publishes the Unity-readable gameplay catalog,
and emits a shared disaster VFX contract. Do not generate distinct meshes for
day/night, breakdown smoke, leaks, fire, sparks, explosion, wake, contrail, or
rotor wash; those are runtime material/VFX states attached to semantic sockets.
Reject generated references unless PNG bytes, SHA-256, and exact 2048x2048
dimensions are validated; smaller ChatGPT UI outputs are visual QA only.

## Scalable catalog architecture

- One skill owns the pipeline. Do not create one skill per asset.
- Recipes own reusable topology: road chassis, rail bogies, fuselages, rotors,
  hulls, industrial kits, urban floor plates, stations, and spline networks.
- Archetypes are data. Buses, motorcycles, helicopters, fighters, UFOs,
  tankers, factories, mines, and buildings are catalog rows referencing recipes.
- Twelve regional profiles own livery, signage, drive-side, gauge, and climate
  overlays without duplicating geometry. Era profiles own shape language and technology changes. Geometry is generated
  once per active era and mesh axis; liveries and weathering share that mesh.
- Unity Addressables should load the current and adjacent eras only. Use GPU
  instancing, texture arrays, and LODs rather than unique materials per object.

The bundled topology catalog is `catalogs/transport_tycoon_1950_2999.json` and
the complete gameplay/economy/failure baselines are in
`catalogs/transport_gameplay_profiles.json`.
It currently covers 81 archetypes through 19 recipes across road,
micromobility, rail, air/UAM, water, cable, space, buildings, industries, and
infrastructure. Unknown future forms are added as catalog data when they fit a
recipe, or as one reusable recipe when their topology is genuinely new.

## Blender access

- `blender` and `blender-python` provide deterministic headless CLI access.
- `blender-mcp-secure` is the interactive MCP bridge. It binds to localhost,
  requires its generated bearer token, and leaves `execute_python` disabled.
- Codex and Claude use the global MCP name `blender`. Blender must be open for
  interactive MCP calls; batch generation does not require the GUI or MCP.

## Supporting material

- Read [references/providers.md](references/providers.md) before selecting or
  installing a provider.
- The Blender scripts are the canonical geometry and optimization logic.
- `scripts/install.sh` installs this skill for both Claude and Codex-compatible
  personal skill discovery and installs the global CLI wrapper.
- Read [references/blender-mcp.md](references/blender-mcp.md) before changing
  the interactive bridge or enabling scripting.

## Safety

- Preserve source images and raw meshes; write generated output separately.
- Do not overwrite a Unity project file unless `--force` was explicitly used.
- Do not accept model licenses, authenticate, spend provider credits, or upload
  user images without explicit approval.
- Do not claim visual fidelity from triangle counts alone; inspect the Unity
  render.
- Keep Blender MCP on loopback with token auth. Never enable unrestricted
  `execute_python` without a separate, explicit user approval.
