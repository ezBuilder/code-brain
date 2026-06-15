# Code Brain

[한국어](ko.md) · [English](../../README.md) · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

English is now the default root README: [../../README.md](../../README.md).

This compatibility page is kept for old links. The root README contains the full install guide, public-repo upgrade flow, `/cb-upgrade`, proof points, security notes, and architecture map.

Quick install:

```bash
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

Upgrade an installed project:

```bash
.ai/bin/ai upgrade latest --json
```

Inside an agent session, run `/cb-upgrade`, then open a new session.
