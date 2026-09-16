# Changelog

## 0.2.0 — 2026-09-16

First release as gimp3-mcp. It continues [gimp-mcp](https://github.com/maorcc/gimp-mcp) by maorcc and contributors, whose history is kept in this repository.

### Added
- `paint_stroke`, `list_brushes` and `sample_color`, for painting with GIMP's own brushes.
- Installation from PyPI: `uvx gimp3-mcp` runs the server and `uvx gimp3-mcp install-plugin` copies the plugin into GIMP.
- An MCP Registry entry, `io.github.tifyr/gimp3-mcp`, and a release workflow that publishes to PyPI and the registry.

### Fixed
- GIMP freezing on macOS when commands arrived together; the plugin now runs one command at a time.
- Tools that always failed on GIMP 3.2: `convert_color_mode` to indexed, `edit_text` size and font, `export_sprite_sheet`, `close_image` and `set_active_image`.
- Tools that reported success without doing anything, such as `auto_levels` and `gradient_fill`, and tool calls whose GIMP procedure failed.
- Tools that destroyed work: `resize_canvas` with a fill color flattened the image, `close_image(save_first=True)` could overwrite another XCF file, and `warp_region` erased the layer. `warp_region` is disabled until it can be rebuilt.
- The MCP server losing replies when it printed to stdout, and blocking every request during a slow GIMP call.
- Snapshots now show the requested image with all visible layers; pixel sampling, export and `call_api` output are fixed.
- Tools no longer change GIMP's colors or your selection when they only need them briefly.

### Changed
- Tool descriptions match what the tools do, and inputs GIMP would misread (unknown colors or options, out-of-range values) are rejected with an error.
- `list_layers` and `get_context_state` report blend mode names, `[x, y]` offsets and sRGB hex colors.
- The undo and redo tools are removed, because GIMP 3 does not let plugins step through undo history.
- `run_tests.py` checks what tools did, not only the status they reported.
