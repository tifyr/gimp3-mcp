# gimp3-mcp

<!-- mcp-name: io.github.tifyr/gimp3-mcp -->

[![PyPI](https://img.shields.io/pypi/v/gimp3-mcp.svg)](https://pypi.org/project/gimp3-mcp/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Works with Claude Desktop](https://img.shields.io/badge/Works%20with-Claude%20Desktop-7B2CBF.svg)](https://claude.ai/desktop)
[![GIMP 3.2](https://img.shields.io/badge/GIMP-3.2-orange.svg)](https://gimp.org)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io)

gimp3-mcp lets an AI assistant work in a running GIMP 3.2. You describe what you want; the assistant creates or opens images, paints, edits, and exports them, and looks at the image between steps to check its work.

It has two parts: a GIMP plugin that runs inside GIMP, and an [MCP](https://modelcontextprotocol.io) server that your AI client (Claude Desktop, Claude Code, or any other MCP client) starts. Together they give the assistant 80 tools, from brush strokes and color adjustments to layers, text, and batch export.

gimp3-mcp started as a fork of [gimp-mcp](https://github.com/maorcc/gimp-mcp) by maorcc and its contributors, and is now developed separately. See [Credits](#credits) and [How it differs from gimp-mcp](#how-it-differs-from-gimp-mcp).


---

## What you can ask for

Each example is a prompt you can give the assistant, followed by the tools it will typically use.

**Paint a picture**
```text
Create an 800×800 canvas and paint a ginger cat sitting front-on, in loose painterly strokes.
Look at the canvas after each round of strokes.
```
`new_canvas` → `paint_stroke` in rounds (textured brushes, tapered strokes, smudging) → `get_state_snapshot` or the `preview` option to look → `sample_color` to pick up colors already on the canvas.

**Touch up a photo**
```text
Open ~/Pictures/harbor.jpg, auto-level it, add a little contrast and warmth,
sharpen slightly, and export a 2048-pixel-wide JPEG to ~/Pictures/harbor-edit.jpg.
```
`open_image` → `auto_levels` → `adjust_brightness_contrast` → `adjust_color_balance` → `sharpen` → `scale_to_fit` → `export_image`.

**Make a graphic**
```text
Make a 1200×630 banner with a dark navy background (#1b2a4a) and the title "Summer Sale"
in large white text with a soft drop shadow, then export it for the web.
```
`new_canvas` → `add_text` → `apply_drop_shadow` → `export_web_optimized`.

**Inspect details**
```text
Show me the top-left 300×300 pixels of the current image, enlarged.
```
`get_state_snapshot` with a `region`.

**Work with many images**
```text
Export every open image as WebP into ~/Desktop/export.
```
`list_images` → `batch_export`.

**Anything the tools don't cover**
```text
Use GIMP's Python API to list every path in the image and how many strokes each has.
```
`call_api` runs Python inside GIMP.

### What tool calls look like

The assistant writes these calls, not you, but they show what the tools accept. Each block is the arguments for the named tool.

`get_state_snapshot`: look at part of an image (coordinates are image pixels; the result is scaled to fit `max_size`):
```json
{"image_index": 0, "max_size": 512,
 "region": {"x": 140, "y": 80, "width": 240, "height": 300}}
```

`paint_stroke`: paint a round of strokes and get a preview back:
```json
{"preview": true, "strokes": [
  {"points": [[100, 400], [300, 330], [520, 380]], "brush": "Oils 01", "size": 120, "color": "#3b6e8f"},
  {"points": [[140, 420], [300, 370]], "brush": "Bristles 02", "size": 40, "color": "#d9a441", "opacity": 80},
  {"points": [[250, 360], [330, 350]], "tool": "smudge", "size": 50, "pressure": "none", "strength": 40}
]}
```

`call_api`: evaluate Python inside GIMP:
```json
{"api_path": "exec", "args": ["pyGObject-eval", ["len(Gimp.get_images())"]]}
```

---

## How it works

```text
AI client (Claude Desktop, Claude Code, ...)
      │  MCP over stdio
      ▼
gimp_mcp_server.py        MCP server: tool definitions and descriptions
      │  JSON over TCP, localhost:9877
      ▼
gimp-mcp-plugin.py        GIMP plugin: runs inside GIMP
      │  PyGObject
      ▼
GIMP 3.2
```

The client starts the MCP server. The server turns each tool call into a JSON command and sends it to the plugin, which carries it out with GIMP's Python API and returns the result. The plugin runs one command at a time.

---

## Requirements

- **GIMP 3.2** (developed and tested with 3.2.6 on macOS)
- **[uv](https://docs.astral.sh/uv/)**, which provides `uvx` and fetches Python 3.11 or newer when needed
- **An MCP client**, such as Claude Desktop or Claude Code

---

## Installation

### 1. Install the GIMP plugin

Start GIMP once so it creates its settings folder, then run:

```bash
uvx gimp3-mcp install-plugin
```

This copies the plugin into `plug-ins/gimp-mcp-plugin/` in the settings folder of the newest GIMP 3.x it finds: `~/Library/Application Support/GIMP/3.2` on macOS, `~/.config/GIMP/3.2` on Linux (Snap and Flatpak included) and `%APPDATA%\GIMP\3.2` on Windows. Pass `--gimp-dir` to choose another folder; GIMP shows the one it uses under **Edit > Preferences > Folders > Plug-ins**. GIMP creates a new settings folder for each minor version, so run the command again after upgrading GIMP. Restart GIMP after installing.

### 2. Start the plugin's server in GIMP

Choose **Tools > MCP > Start MCP Server**. No image needs to be open. The server listens on `localhost:9877` until GIMP quits, so start it again each time you start GIMP. The same menu has **Check MCP Server** and **Restart MCP Server**.

### 3. Connect your AI client

**Claude Desktop.** Add the server to `claude_desktop_config.json`, which is at `~/Library/Application Support/Claude/` on macOS and `%APPDATA%\Claude\` on Windows:

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

Quit and reopen Claude Desktop. Claude Desktop lets you turn a connector's tools on and off individually. If the assistant says a tool such as `new_canvas` isn't available, enable it in the gimp connector's tool settings and start a new chat.

**Claude Code.**

```bash
claude mcp add --scope user gimp -- uvx gimp3-mcp
```

**Other MCP clients.** Configure a stdio server with the command `uvx` and the argument `gimp3-mcp`.

### 4. Check the connection

Ask the assistant to "check the GIMP connection". It calls `check_server`, which reports whether the plugin is reachable and which GIMP version is running.

---

## Tools

The server provides 80 tools. Each tool's own description, which the assistant sees, documents its parameters and results.

### Seeing the image
| Tool | What it does |
|---|---|
| `get_state_snapshot` | PNG of an image or a region, scaled to fit `max_size`; the assistant's main way to look |
| `get_image_bitmap` | The same image data with separate `max_width`/`max_height` and `origin_x`/`origin_y` region keys |
| `get_image_metadata` | Size, color mode, resolution, layers, file |
| `sample_color` | Color at a point of the visible image, as hex, optionally averaged over a radius |
| `get_pixel_color` | Color at one pixel of the visible image or of a single layer |
| `get_histogram` | Mean, median, and spread of a channel of a layer |
| `get_selection_bounds` | Bounding box of the current selection |
| `list_images` | Open images in index order, with names and sizes |
| `list_layers` | Layers from top to bottom, with the active layer marked |

### Images and files
| Tool | What it does |
|---|---|
| `new_canvas` | Create a blank image in a new window |
| `open_image` | Open an image file in a new window |
| `save_xcf` | Save as GIMP's XCF format, keeping layers |
| `export_image` | Export to PNG, JPEG, WebP, or TIFF |
| `close_image` | Close an image opened with `new_canvas` or `open_image` (optionally saving XCF first) |
| `set_active_image` | Bring an image's window to the front |
| `convert_color_mode` | Convert to RGB, grayscale, or indexed color |

### Painting and drawing
| Tool | What it does |
|---|---|
| `paint_stroke` | Paint batches of brush strokes: textured brushes, tapered or pressure-varied strokes, airbrush, smudge, eraser |
| `list_brushes` | Brush names for `paint_stroke` |
| `draw_line` | Straight line with the pencil or paintbrush |
| `draw_rectangle` / `draw_ellipse` | Rectangle or ellipse outline |
| `fill_rectangle` / `fill_ellipse` | Filled rectangle or ellipse |
| `fill_layer` | Fill a whole layer with a color |
| `fill_selection` | Fill the selection with a color, GIMP's foreground or background color, a pattern, or transparency |
| `gradient_fill` | Linear or radial gradient |
| `set_colors` | Set GIMP's foreground and background colors |

### Adjustments
| Tool | What it does |
|---|---|
| `adjust_brightness_contrast` | Brightness and contrast |
| `adjust_curves` | Curves by channel, with presets or custom points |
| `adjust_hue_saturation` | Hue, saturation, and lightness, for all colors or one color range |
| `adjust_color_balance` | Color balance for shadows, midtones, or highlights |
| `auto_levels` | Stretch the tonal range automatically |
| `desaturate` | Convert a layer to grayscale |
| `invert_colors` | Invert a layer's colors |

### Filters and effects
| Tool | What it does |
|---|---|
| `blur` / `apply_gaussian_blur` | Gaussian blur (`blur` takes separate horizontal and vertical radii) |
| `sharpen` | Unsharp mask |
| `denoise` | Noise reduction |
| `apply_noise` | Add noise or grain |
| `apply_pixelate` | Block mosaic |
| `apply_emboss` | Emboss |
| `apply_vignette` | Darken the edges |
| `apply_drop_shadow` | Soft drop shadow below a layer |
| `warp_region` | Disabled: on GIMP 3.2 it erased the layer instead of warping, so it now returns an error |

### Transforms
| Tool | What it does |
|---|---|
| `scale_image` | Scale to exact dimensions |
| `scale_to_fit` | Scale to fit a box, keeping the aspect ratio |
| `crop_to_rect` | Crop to a rectangle |
| `crop_to_selection` | Crop to the selection, or auto-crop |
| `rotate_image` | Rotate by 90, 180, 270, or any angle |
| `flip_image` | Flip horizontally or vertically |
| `resize_canvas` | Change the canvas size without scaling the content |

### Selections
| Tool | What it does |
|---|---|
| `select_rectangle` / `select_ellipse` | Rectangle or ellipse selection, optionally feathered |
| `select_by_color` | Select pixels similar to a color |
| `select_all` / `select_none` | Select everything or nothing |
| `invert_selection` | Invert the selection |
| `modify_selection` | Grow, shrink, feather, border, or sharpen the selection |

### Layers
| Tool | What it does |
|---|---|
| `create_layer` | New layer, empty or filled |
| `duplicate_layer` | Copy a layer |
| `delete_layer` | Delete a layer |
| `rename_layer` | Rename a layer |
| `reorder_layer` | Move a layer in the stack |
| `set_layer_properties` | Opacity, blend mode, visibility |
| `merge_visible_layers` | Merge the visible layers |
| `flatten_image` | Flatten all layers |

### Text
| Tool | What it does |
|---|---|
| `add_text` | Add a text layer |
| `edit_text` | Change a text layer's text, font, size, or color |
| `list_fonts` | Installed fonts, filtered by name |

### Batch and export pipelines
| Tool | What it does |
|---|---|
| `batch_export` | Export all open images, or one, to a folder |
| `batch_resize` | Resize all open images |
| `export_web_optimized` | Export JPEG and PNG and report which is smaller |
| `export_social_media_kit` | Export sizes for Instagram, Twitter/X, Facebook, and YouTube |
| `export_icon_sizes` | Export Android or iOS icon sets |
| `export_sprite_sheet` | Combine layers or open images into a sprite sheet |

### Connection and advanced
| Tool | What it does |
|---|---|
| `check_server` | Check that the plugin is reachable |
| `restart_server` | Reconnect after GIMP was restarted |
| `get_gimp_info` | GIMP version, folders, and environment |
| `get_context_state` | GIMP's current colors, brush, opacity, and mode |
| `call_api` | Run Python inside GIMP, for anything the tools don't cover |

The server also offers two MCP prompts, `gimp_best_practices` and `gimp_iterative_workflow`, which clients that support prompts can add to a conversation. They are also in [docs/best_practices.md](docs/best_practices.md) and [docs/iterative_workflow.md](docs/iterative_workflow.md).

---

## How the tools behave

The assistant receives these rules when it connects. They also help when you read its tool calls or debug a result.

- **Image order.** `image_index` 0 is the most recently opened or created image, and indices shift when images open or close. `list_images` shows the current order.
- **Layers.** Tools with an optional `layer_name` act on the active layer. A layer made by `create_layer` or `duplicate_layer` becomes active. In `list_layers`, index 0 is the top layer.
- **Colors.** Colors are hex (`"#8b4513"`, `"#fff"`, `"#8b451380"`) or one of the 16 basic CSS names: black, white, gray, silver, red, maroon, yellow, olive, lime, green, aqua, teal, blue, navy, fuchsia, purple. Other names and `rgb()` values are rejected, because GIMP would turn them into the wrong color.
- **Snapshots.** `get_state_snapshot` scales the image or region to fit `max_size`, so small regions come back enlarged. Region coordinates are image pixels.
- **Errors instead of guesses.** Options with a fixed list of values, such as blend modes, interpolation, or anchors, reject anything not on the list, and the error names the valid values. Filters report an error if GIMP can't apply them.
- **Closing images.** `close_image` can close images opened with `new_canvas` or `open_image`. GIMP 3 gives plugins no way to close windows opened from GIMP's own menus; close those in GIMP.
- **Undo.** There is no undo tool, because GIMP 3 doesn't let plugins step through undo history. Each tool call, including each `paint_stroke` batch, is one step you can undo with Ctrl+Z in GIMP. To let the assistant discard work, have it paint on a separate layer and delete that layer.

---

## Security

The plugin runs any Python code it receives through `call_api`. While the server is started, any program on your computer that can connect to `localhost:9877` can control GIMP and run code with your user's permissions. The plugin only accepts connections from your own computer. Start the server when you want to use it, and quit GIMP to stop it.

---

## Example scripts

These scripts talk to the plugin directly, without an AI client. Start the plugin's server in GIMP first. Usage is at the top of each file.

| Script | What it does |
|---|---|
| [`bg_remove.py`](bg_remove.py) | Remove a background with a single fuzzy-select pass |
| [`bg_remove_iterative.py`](bg_remove_iterative.py) | Remove a background in repeated passes, checking snapshots between them |
| [`agent_edit_demo.py`](agent_edit_demo.py) | Open, remove the background, warp the mouth into a smile, check snapshots, export (the warp step fails while `warp_region` is disabled) |

---

## Troubleshooting

**"Could not connect to GIMP"**
- Start GIMP and choose **Tools > MCP > Start MCP Server**. This is needed again after every GIMP restart.
- If GIMP was restarted while your AI client kept running, ask the assistant to run `restart_server`.

**The MCP menu is missing in GIMP**
- Look under **Tools > MCP**.
- Run `uvx gimp3-mcp install-plugin` again and restart GIMP. The plugin belongs in `plug-ins/gimp-mcp-plugin/gimp-mcp-plugin.py` inside the settings folder for your current GIMP version (**Edit > Preferences > Folders > Plug-ins**), and GIMP uses a new folder after each minor upgrade.
- On macOS and Linux, make sure the file is executable (`chmod +x`), then restart GIMP.
- Plugin errors appear in GIMP's **Windows > Dockable Dialogs > Error Console**.

**The assistant says a tool doesn't exist**
- Restart the AI client after updating gimp3-mcp.
- In Claude Desktop, check that the tool is switched on in the gimp connector's tool settings.

**Changes to the plugin have no effect**
- GIMP loads the plugin when it starts. After updating gimp3-mcp, run `uvx gimp3-mcp@latest install-plugin`, restart GIMP and start the server again. `uvx` keeps a cached copy, and `@latest` makes it fetch the newest release.

---

## Development

Install the development dependencies and the pre-commit hook, which runs `ruff` on each commit:

```bash
uv sync
uv run pre-commit install
```

Run the same lint check as CI (the version is pinned in `.github/workflows/lint.yml` and `.pre-commit-config.yaml`):

```bash
uvx ruff@0.15.11 check .
```

The tests talk to a running GIMP, so start the plugin's server first. They create or open their own images, but `run_tests.py` edits whichever image is most recently opened, so save your work before running it.

```bash
python run_tests.py                   # 85 checks across the tool categories
python tests/test_paint_stroke.py     # paint_stroke, list_brushes, sample_color
python tests/test_add_text_metadata.py
```

`run_tests.py` changes GIMP's foreground and background colors and leaves its test canvas open. `tests/continuous_edit_test/continuous_edit_test.py` closes every open image, so don't run it in a session with work you want to keep.

To run from a checkout instead of PyPI:

```bash
git clone https://github.com/tifyr/gimp3-mcp.git
cd gimp3-mcp
uv sync
uv run gimp3-mcp install-plugin
```

Then point your client at the command `uv` with the arguments `run --directory /full/path/to/gimp3-mcp gimp_mcp_server.py`. Claude Code started in the folder offers the same server from `.mcp.json`. After changing `gimp-mcp-plugin.py`, run `uv run gimp3-mcp install-plugin` and restart GIMP. After changing `gimp_mcp_server.py`, restart your AI client.

To release, set the new version in `pyproject.toml` and in both places in `server.json`, describe it in `CHANGELOG.md`, and push a tag such as `v0.2.0`. The Release workflow checks that the versions match, then publishes to PyPI and the MCP Registry.


---

## How it differs from gimp-mcp

gimp3-mcp keeps gimp-mcp's design, a GIMP plugin plus an MCP server, and most of its tools. Since the fork it has gained:

- **Painting tools:** `paint_stroke`, `list_brushes` and `sample_color`, for brushwork with GIMP's own brushes.
- **A fix for GIMP freezing on macOS:** the plugin runs one command at a time.
- **No silent failures:** tools that reported success without doing anything, such as `auto_levels` and `gradient_fill`, now work, and failed GIMP calls return an error.
- **No destroyed work:** `resize_canvas` keeps your layers, `close_image` never overwrites another file, tools leave GIMP's colors and your selection as they were, and `warp_region`, which erased layers, is disabled.
- **A more reliable server:** it no longer loses replies or stalls every request during a slow GIMP call.
- **Tests that check results:** `run_tests.py` looks at pixels, colors and selections, not only at the status a tool reports.
- **Installation from PyPI:** `uvx gimp3-mcp`, with a command that installs the plugin.

[CHANGELOG.md](CHANGELOG.md) has the details.

---

## Limitations

- GIMP must be running with its MCP server started (**Tools > MCP > Start MCP Server**); gimp3-mcp can't start GIMP for you.
- It is developed and tested with GIMP 3.2.6 on macOS. Linux and Windows should work but haven't been tested for this release.
- `warp_region` is disabled, because GIMP 3.2 gives plugins no working warp operation.
- The plugin runs one command at a time, so a slow operation holds up the next call.
- `call_api` runs any Python code inside GIMP. See [Security](#security).

---

## Credits

gimp3-mcp is based on [gimp-mcp](https://github.com/maorcc/gimp-mcp), created by [maorcc](https://github.com/maorcc) with contributions from tomer, jmagdalena, Victor (Viesar Lab), AnthonyHerman and others. The plugin, the server and most of the tools come from their work, and their commits are kept in this repository's history. gimp3-mcp is maintained separately by [tifyr](https://github.com/tifyr) and is not an official release of gimp-mcp.

---

## License

GPL-3.0, the same license as gimp-mcp. See [LICENSE](LICENSE).
