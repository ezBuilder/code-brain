# Blender MCP and CLI contract

## Installed baseline

- Blender 5.2 LTS CLI: `/Applications/Blender.app/Contents/MacOS/Blender`
- Headless aliases: `blender`, `blender-python`
- Interactive bridge: `blender-mcp-secure`
- MCP client name: `blender` in Codex and Claude Code
- Add-on source: `zorak1103/blender-mcp` v0.5.0, pinned commit
  `6c67ce5989bbb3dda7c8628ab0c989e5bc6ddd32`
- Transport: Streamable HTTP on `127.0.0.1:8400`, wrapped as stdio for clients
- Authentication: generated bearer token stored owner-only by the add-on
- `execute_python`: disabled; the normal typed object/material/render tools are
  sufficient for routine interactive work

The local stdio launcher includes a compatibility fix that forwards the
required Streamable HTTP `Accept`, protocol-version, and session headers and
unwraps SSE response messages. Do not replace it with an unverified upstream
launcher.

## Routing

- Use MCP for live scene inspection, object edits, materials, cameras, and
  viewport/render feedback while Blender is open.
- Use `blender --background --factory-startup --python ... -- ...` for
  deterministic asset batches and CI.
- Use `ai-unity-asset optimize` for provider meshes and `fleet` for the bundled
  cream/teal reference family.
- Keep provider upload, authentication, and paid generation outside MCP unless
  the user explicitly authorizes it.

## Doctor proof

The accepted live probe is: initialize succeeds, `tools/list` returns typed
tools, core scene/create/render tools exist, and `execute_python` is absent.
