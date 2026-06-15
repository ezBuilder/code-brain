# Code Brain

[한국어](ko.md) · [English](../../README.md) · [中文](zh-CN.md) · [日本語](ja.md) · Español · [Français](fr.md) · [Deutsch](de.md)

Code Brain es infraestructura local al repositorio para agentes de codigo. Claude Code, Codex CLI y Google Antigravity comparten la misma memoria `.ai/`, busqueda BM25, politica de hooks, herramientas MCP, auditoria y ruta de actualizacion.

## Por que destaca

- Varios agentes comparten un mismo brain local al repo.
- El perfil MCP `usage` expone solo las herramientas frecuentes y reduce presion de tokens.
- BM25/FTS5 y `context_pack` ayudan a acotar codigo antes de leer demasiado.
- `code_read_hashline` aporta anclas line+sha antes de editar.
- Los hooks bloquean git destructivo, secretos, grep/find demasiado amplios y salidas enormes.
- Los JSONL/log/evidence generados tienen limites y `doctor` los valida.
- `/cb-upgrade` o `.ai/bin/ai upgrade latest --json` actualizan desde el repo publico de GitHub.

## Instalacion

```bash
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

En una shell interactiva de macOS/Linux, el instalador tambien propone por defecto el kit global de Claude/Codex. Los archivos existentes `~/.claude/CLAUDE.md` y `~/.codex/AGENTS.md` se respaldan y conservan; Code Brain solo agrega o actualiza su bloque administrado. En CI o instalaciones no interactivas se omiten escrituras globales salvo que pases `--global`.

Windows:

```powershell
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

Despues, abre una nueva sesion de Claude/Codex/Antigravity.

## Actualizacion

```bash
.ai/bin/ai upgrade latest --json
```

Dentro del agente, ejecuta `/cb-upgrade`. Al terminar, abre una nueva sesion.

## Pruebas reproducibles

```bash
make lint
.ai/bin/ai upgrade latest --dry-run --json
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

License: Apache-2.0.
