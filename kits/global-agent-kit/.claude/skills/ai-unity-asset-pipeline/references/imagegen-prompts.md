# ImageGen asset prompts

Use `RF-IMAGEGEN-MULTIVIEW-1.2` for bulk 3D-source generation. Generate one
asset and one permanent geometry state per image; never combine vehicle
variants or states in one reconstruction sheet. Keep the output
immutable and record its prompt fingerprint before mesh generation.

For GPT Image 2, set `size=2048x2048`, `quality=high`, `output_format=png`, and
an opaque background in the API or client controls. Resolution and quality are
output parameters, not reliable prompt-only instructions. Do not request
4096x4096: GPT Image 2 currently caps each edge at 3840 pixels.
The ChatGPT UI may still return a smaller square. Treat that result as visual
QA only: the production queue requires validated PNG bytes, SHA-256, and exact
2048x2048 dimensions before mesh generation.

## Generate a fixed prompt

```bash
ai-unity-asset prompt \
  --asset-id road-city-bus-1988-kr \
  --subject "full-size two-axle low-floor city bus" \
  --year 1988 \
  --region "South Korea, late-1980s industrial design" \
  --technology "rear diesel engine, two axles, right-hand traffic" \
  --livery "unbranded cream upper body and deep teal lower body" \
  --details "two passenger doors, seated cabin, roof HVAC, steel bumpers" \
  --asset-state base \
  --output /absolute/prompts/road-city-bus-1988-kr.txt
```

The command writes the exact prompt and a JSON sidecar containing its SHA-256
fingerprint. Use the same fixed fields for retries; change one field at a time
and issue a new asset ID for accepted variants.

## 1950-2999 era lock

| Era | Years | Shape/technology lock | Bus / truck target |
| --- | ---: | --- | ---: |
| postwar | 1950-1969 | riveted steel, analog controls, upright utility forms | 44 / 38 km/h |
| mass-transit | 1970-1989 | boxy bodies, high visibility, diesel packaging | 64 / 56 km/h |
| digital | 1990-2029 | aerodynamic composites, digital systems, cleaner glazing | 82 / 72 km/h |
| green | 2030-2069 | electric drive, solar auxiliaries, recycled alloy | 105 / 94 km/h |
| autonomous | 2070-2149 | driverless cabin, sensor arrays, modular bodies | 135 / 120 km/h |
| fusion | 2150-2249 | compact fusion, active aero, smart surfaces | 165 / 145 km/h |
| orbital | 2250-2399 | radiation shielding, orbital interoperability, field assist | 185 / 165 km/h |
| interplanetary | 2400-2599 | self-repairing vacuum-rated structures | 210 / 185 km/h |
| post-scarcity | 2600-2799 | programmable matter, morphing zero-emission forms | 235 / 210 km/h |
| singularity | 2800-2999 | field propulsion, translucent alloy, nonhuman packaging | 260 / 230 km/h |

An asset uses the midpoint of its active era as `DESIGN_YEAR`. Never copy a
modern cabin, lamp, wheel, door, sensor, or propulsion package into an earlier
era. Future assets must still expose plausible load paths, access panels,
ground clearance, service points, and neutral moving-part positions.

## Build a bulk queue

```bash
ai-unity-asset prompt-batch \
  --start-year 1950 --end-year 2999 \
  --domain road \
  --region east-asia \
  --output-dir /absolute/asset-image-batch
```

Use `--archetype city-bus`, `--archetype dump-truck`, or `--max-prompts 10`
for bounded batches. The command writes deterministic prompt files, SHA-256
sidecars, `asset-image-batch.json`, and a
`chatgpt2codex-image-queue.jsonl` queue. Every queue row requests ChatGPT image
mode `very-high`, then requests `gpt-5.6-sol` with `xhigh` reasoning for the
mesh/Blender/Unity build lane when that model option is available.

For the complete transport-only factory, including gameplay values and
reconstructable disaster states:

```bash
ai-unity-asset transport-factory \
  --start-year 1950 --end-year 2999 \
  --region global-neutral \
  --output-dir /absolute/transport-factory \
  --unity-project /absolute/unity/project
```

The output contains separate `base`, `damaged`, and `wrecked` image jobs.
Day/night uses emissive materials and runtime lights. Breakdown, smoke, leaks,
fire, sparks, electrical arcs, explosions, wakes, contrails, and rotor wash use
shared Unity VFX packs and must not be baked into reconstruction images.

## Bulk rules

- Use the stable ID pattern `domain-archetype-year-region`.
- Use one subject, one design, one livery, and four orthographic views per image.
- Use one permanent geometry state (`base`, `damaged`, or `wrecked`) per image.
- Keep the year and technology physically compatible.
- Keep brand names, logos, registration plates, text, people, scenery, and
  dramatic lighting out of reconstruction references.
- Require fully visible circular wheels with restrained tread depth and
  recessed rims for wheeled vehicles.
- Preserve the source image, prompt text, and sidecar JSON together.
- Reject a sheet if any view changes axle count, proportions, doors, windows,
  cargo, fixtures, or wheel placement.
- Reject a sheet if it violates its era's materials, propulsion, safety,
  lighting, glazing, wheel, sensor, or operator-layout constraints.
- Reject damaged or wrecked sheets that change the underlying model identity.
- Reject any reconstruction sheet containing active smoke, fire, sparks,
  explosion, fluid spray, dramatic night lighting, or debris clouds.

For aircraft, ships, buildings, or space vehicles, retain the fixed layout and
replace wheel-specific details with the subject's symmetry-critical features.
Use [chatgpt2codex-asset-factory.md](chatgpt2codex-asset-factory.md) for the
generation-to-Unity handoff.
