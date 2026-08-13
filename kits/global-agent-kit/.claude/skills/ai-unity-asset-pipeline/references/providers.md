# Provider routing

## Adopted local baseline

- Blender 5.2 LTS: canonical retopology, pivot, scale, material, LOD, FBX, and
  GLB stage.
- glTF Transform 4.4: optional GLB inspection and post-export optimization.
- Unity 6.5: final import, material assignment, test, and render proof.
- ImageGen: controlled orthographic reference generation when available.

## AI reconstruction providers

| Provider | Route | Status | Gate |
| --- | --- | --- | --- |
| Tripo/Meshy | Export GLB, then run `optimize` | supported handoff | API account, upload, and credits need approval |
| Stable Fast 3D | Local GLB, then run `optimize` | optional | gated model license and Hugging Face auth need approval |
| TRELLIS.2 | Remote Linux/NVIDIA worker | optional | official baseline requires Linux, CUDA, and 24 GB NVIDIA VRAM |
| Bundled fleet recipe | Direct FBX/GLB + LODs | installed | premium six-vehicle reference family |
| Era catalog factory | Recipe and provider job planning | installed | 81 archetypes, 19 reusable recipes, 12 regions, 1950-2999 |

Do not install an unsupported macOS fork as the default TRELLIS.2 provider.
The official repository remains the authority for platform requirements.

## Reference-image contract

- orthographic front, rear, side, and top views;
- identical scale and alignment;
- white or transparent background;
- no cast shadow, text, logos, people, overlap, or perspective;
- one asset per reconstruction input;
- immutable source checksum recorded in the output manifest.

## Game-ready contract

- bottom-center pivot and meters;
- preserved or regenerated UV0;
- PBR-compatible material slots;
- LOD0 visual source, LOD1 gameplay mesh, LOD2 distant mesh;
- target budgets default to 6,000 / 2,000 / 700 triangles;
- both FBX for Unity and GLB for interchange;
- manifest with source hash, bounds, triangle counts, and tool versions.

Do not multiply scripts by asset count. Add catalog rows for variants that fit
an existing recipe, add shared modules for new body systems, and create a new
generator recipe only for a fundamentally new topology family.
