# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is gimp3-mcp, a Model Context Protocol integration that drives a running GIMP 3.2 from Claude Desktop and other MCP clients. It is published on PyPI as `gimp3-mcp` and in the MCP Registry as `io.github.tifyr/gimp3-mcp`, and continues [gimp-mcp](https://github.com/maorcc/gimp-mcp) by maorcc and its contributors. The system consists of two main components:

1. **GIMP Plugin** (`gimp-mcp-plugin.py`): A GIMP 3.2 plugin that starts a socket server inside GIMP
2. **MCP Server** (`gimp_mcp_server.py`): An MCP server that connects to the GIMP plugin and exposes GIMP functionality

## Architecture

The system uses a client-server architecture:
- GIMP Plugin creates a socket server (localhost:9877) that accepts Python-Fu commands
- MCP Server connects to this socket and exposes a `call_api` tool for MCP clients
- Commands are executed in GIMP's Python-Fu environment with access to the full GIMP 3.2 API

## Installation & Setup

### GIMP Plugin Installation
Use the installer rather than copying by hand:

```bash
uv run gimp3-mcp install-plugin      # from a checkout
uvx gimp3-mcp install-plugin         # from the released package
```

It copies the plugin into `plug-ins/gimp-mcp-plugin/` in the newest GIMP 3.x settings folder it finds,
and `--gimp-dir` overrides that choice. GIMP's per-user config dir is named after its **major.minor**
version (`3.0`, `3.2`, `3.4`, …) and a new one is created on each minor upgrade, so the plugin has to be
installed again after an upgrade; the active path is shown in **Edit > Preferences > Folders > Plug-ins**.
Base dirs per platform:
- Linux: `~/.config/GIMP/<VER>` (Snap: `~/snap/gimp/current/.config/GIMP/<VER>`, Flatpak: `~/.var/app/org.gimp.GIMP/config/GIMP/<VER>`)
- macOS: `~/Library/Application Support/GIMP/<VER>`
- Windows: `%APPDATA%\GIMP\<VER>`

Then start it from **Tools > MCP > Start MCP Server** in GIMP. GIMP loads the plugin when it starts, so
restart GIMP after installing a new version.

### MCP Server Configuration
Add to the Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):
```json
{
  "mcpServers": {
    "gimp": {
      "command": "uvx",
      "args": ["gimp3-mcp"]
    }
  }
}
```
To run a checkout instead of the released package, use `"command": "uv"` with
`["run", "--directory", "/path/to/gimp3-mcp", "gimp_mcp_server.py"]`.

## Development Commands

- **Lint:** `uvx ruff@0.15.11 check .` (the same ruff version CI uses). `uv sync && uv run pre-commit install` enables a hook that runs `ruff check --fix` on each commit.
- **Tests:** `python run_tests.py` is an integration suite (85 checks) that talks to a running GIMP on `localhost:9877`, so start the plugin in GIMP first. It cannot run in CI. `tests/test_paint_stroke.py` and `tests/test_add_text_metadata.py` are narrower suites. `run_tests.py` changes GIMP's foreground/background colors and leaves its canvas open, and `tests/continuous_edit_test/continuous_edit_test.py` closes every open image, so don't run it in a session with work worth keeping.
- **Assert on results, not status:** tools used to return `{"status": "success"}` while doing nothing, so checks compare pixels, colors and selections.
- **After changing the plugin:** install it again and restart GIMP. The running plug-in process keeps the code it started with, so an edited file has no effect until then.

### CI / Lint workflow

- `.github/workflows/lint.yml` runs ruff via `astral-sh/ruff-action` only on pushes to `main` and on pull requests. Pushing a feature branch does not trigger it; open a PR to get a Lint result.
- The ruff version is pinned in the workflow (`version: "0.15.11"`) and must match the `rev` in `.pre-commit-config.yaml`. Change both together, including after `uv run pre-commit autoupdate`, which bumps only the hook.
- Without the pin, the action installs the latest ruff. Ruff 0.16 expanded its default rule set, so Lint started failing on unchanged code (441 errors, mostly `BLE001`, `TRY002` and `S110` in `gimp_mcp_server.py` and `gimp-mcp-plugin.py`).
- `pyproject.toml` does not set `select`, so the project relies on ruff's defaults. Before moving to ruff ≥ 0.16, either add `[tool.ruff.lint] select = ["E4", "E7", "E9", "F"]` (the pre-0.16 defaults, which pass on the current code) or fix the new findings.

### Packaging & release

- `pyproject.toml` uses hatchling. `[tool.hatch.build.targets.wheel.force-include]` maps the root `gimp_mcp_server.py`, `gimp-mcp-plugin.py` and the two `docs/*.md` prompt files into the `gimp3_mcp` package, so the files stay at the repo root while the wheel ships them inside the package. The prompt docs must land next to `server.py`, which reads them relative to `__file__`.
- `mcp` is capped below 2, whose FastMCP API differs. Tested against 1.10.1 and 1.30.0.
- To release: put the same version in `pyproject.toml` and both places in `server.json`, date the `CHANGELOG.md` entry, then push a `vX.Y.Z` tag. `.github/workflows/release.yml` checks that the versions agree, publishes to PyPI through trusted publishing (environment `pypi`) and registers the server with `mcp-publisher`.
- Registry ownership rests on the `<!-- mcp-name: io.github.tifyr/gimp3-mcp -->` marker in `README.md`; keep it.
- `astral-sh/setup-uv` publishes no moving major tag, so its `uses:` reference must name an exact release.
- The README is also the PyPI page: links must be absolute, or they break there.

## API Usage

### Core MCP Tool
The main interface is the `call_api` tool with parameters:
- `api_path`: "exec" for Python-Fu execution
- `args`: Array containing procedure name and code/expressions

### Common Command Patterns

**Execute Python Commands:**
```json
{
  "api_path": "exec",
  "args": ["exec", ["print('hello world')"]]
}
```

### GIMP 3.2 API Key Points

- Use `Gimp.get_images()` instead of deprecated `Gimp.list_images()`
- Access layers via `image.get_layers()` instead of `Gimp.get_active_layer()`
- Colors are created with `Gegl.Color.new('color_name')`
  or with color RGB values, e.g. `Gegl.Color.new("rgb(1.0, 0.647, 0.0)")`. Notice, each RGB value is in the range 0-1
- Always call `Gimp.displays_flush()` after drawing operations

### Essential Initialization Pattern
Most GIMP operations should start with this initialization:
```python
images = Gimp.get_images()
image = images[0]  # or image1 = images[0]
layers = image.get_layers()
layer = layers[0]  # or layer1 = layers[0]
drawable = layer   # or drawable1 = layer
```

### Common Operations

**Drawing a line:**
```python
Gimp.pencil(drawable, [x1, y1, x2, y2])
Gimp.displays_flush()
```

**Setting colors:**
```python
red_color = Gegl.Color.new("red")
Gimp.context_set_foreground(red_color)
```

**Creating shapes:**
```python
Gimp.Image.select_ellipse(image, Gimp.ChannelOps.REPLACE, x, y, width, height)
Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)
Gimp.Selection.none(image)
Gimp.displays_flush()
```

## Important Notes

- Commands execute in a persistent Python context - imports and variables persist between calls
- GIMP 3.2 API differs significantly from 2.x - consult https://developer.gimp.org/api/3.0/libgimp/
- Always verify API calls work before building complex operations
- The `gimpfu` module is not available in GIMP 3.2
- Use proper error handling as socket connections can fail
- `warp_region` is disabled: GIMP 3.2 offers plug-ins no working warp, and the old code erased the layer

## File Structure

- `gimp-mcp-plugin.py`: GIMP plugin with socket server and command execution
- `gimp_mcp_server.py`: MCP server that bridges socket to MCP protocol
- `gimp3_mcp/`: the package published to PyPI — `cli.py` provides the `gimp3-mcp` command and `install-plugin`; the wheel also carries the server, the plugin and the prompt docs
- `server.json`: MCP Registry entry (`io.github.tifyr/gimp3-mcp`); its versions must match `pyproject.toml`
- `CHANGELOG.md`: release notes, dated per version
- `docs/best_practices.md`: Best practices, common recipes, self-critique checklist, and guidelines exposed via MCP prompts
- `docs/iterative_workflow.md`: Professional iterative workflow guidance for building complex images with layer management and validation
- `GIMP_MCP_PROTOCOL.md`: Detailed API documentation and examples
- `README.md`: Installation and setup instructions
- `run_tests.py`, `tests/`: Integration tests that require a running GIMP with the plugin started
- `.github/workflows/lint.yml`: CI ruff check (version pinned; see CI / Lint workflow above)
- `.github/workflows/release.yml`: publishes to PyPI and the MCP Registry from a `v*` tag
- `.pre-commit-config.yaml`: Local ruff hook; keep its `rev` in sync with the CI pin
