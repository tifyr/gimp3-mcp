# GIMP MCP

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Works with Claude Desktop](https://img.shields.io/badge/Works%20with-Claude%20Desktop-7B2CBF.svg)](https://claude.ai/desktop)
[![GIMP 3.2](https://img.shields.io/badge/GIMP-3.2-orange.svg)](https://gimp.org)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io)

GIMP MCP lets an AI assistant work in a running GIMP 3.2. You describe what you want; the assistant creates or opens images, paints, edits, and exports them, and looks at the image between steps to check its work.

It has two parts: a GIMP plugin that runs inside GIMP, and an [MCP](https://modelcontextprotocol.io) server that your AI client (Claude Desktop, Claude Code, or any other MCP client) starts. Together they give the assistant 80 tools, from brush strokes and color adjustments to layers, text, and batch export.

![GIMP MCP in action: an AI agent driving GIMP through natural language](docs/mcpInAction.gif)

Full demo with audio: [docs/demo.mp4](https://github.com/maorcc/gimp-mcp/raw/main/docs/demo.mp4)

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
- **Python 3.11 or newer**
- **[uv](https://docs.astral.sh/uv/)**, for example `pip install uv`
- **An MCP client**, such as Claude Desktop or Claude Code

---

## Installation

### 1. Get the code

```bash
git clone https://github.com/maorcc/gimp-mcp.git
cd gimp-mcp
uv sync
```

### 2. Install the GIMP plugin

Copy `gimp-mcp-plugin.py` into its own folder inside GIMP's `plug-ins` directory, then restart GIMP.

GIMP keeps its settings in a folder named after its major.minor version (`3.0`, `3.2`, ...) and creates a new one when you upgrade, so the plugin must be reinstalled after a minor upgrade. GIMP shows the active folder under **Edit > Preferences > Folders > Plug-ins**. Start GIMP once before installing so the folder exists.

**macOS and Linux:**
```bash
# Pick the base directory for your platform:
BASE="$HOME/Library/Application Support/GIMP"     # macOS
# BASE="$HOME/.config/GIMP"                        # Linux
# BASE="$HOME/snap/gimp/current/.config/GIMP"      # Linux (Snap)

# Use the newest GIMP 3.x settings folder:
GIMP_DIR="$(ls -d "$BASE"/3.* 2>/dev/null | sort -V | tail -1)"
if [ -z "$GIMP_DIR" ]; then
  echo "No GIMP 3.x settings folder under $BASE; start GIMP once, then run this again." >&2
  exit 1
fi
mkdir -p "$GIMP_DIR/plug-ins/gimp-mcp-plugin"
cp gimp-mcp-plugin.py "$GIMP_DIR/plug-ins/gimp-mcp-plugin/"
chmod +x "$GIMP_DIR/plug-ins/gimp-mcp-plugin/gimp-mcp-plugin.py"
echo "Installed into: $GIMP_DIR/plug-ins/gimp-mcp-plugin"
```

**Windows:** copy the file to
```text
%APPDATA%\GIMP\3.2\plug-ins\gimp-mcp-plugin\gimp-mcp-plugin.py
```
using your GIMP's major.minor version in place of `3.2`.

### 3. Start the plugin's server in GIMP

Choose **Tools > MCP > Start MCP Server**. No image needs to be open. The server listens on `localhost:9877` until GIMP quits, so start it again each time you start GIMP. The same menu has **Check MCP Server** and **Restart MCP Server**.

### 4. Connect your AI client

**Claude Desktop.** Add the server to `claude_desktop_config.json`, which is at `~/Library/Application Support/Claude/` on macOS and `%APPDATA%\Claude\` on Windows:

```json
{
  "mcpServers": {
    "gimp": {
      "command": "uv",
      "args": ["run", "--directory", "/full/path/to/gimp-mcp", "gimp_mcp_server.py"]
    }
  }
}
```

Quit and reopen Claude Desktop. Claude Desktop lets you turn a connector's tools on and off individually. If the assistant says a tool such as `new_canvas` isn't available, enable it in the gimp connector's tool settings and start a new chat.

**Claude Code.** Start Claude Code in the `gimp-mcp` folder and approve the project's MCP server from `.mcp.json`, or add it for all projects:

```bash
claude mcp add --scope user gimp -- uv run --directory /full/path/to/gimp-mcp gimp_mcp_server.py
```

**Other MCP clients.** Configure a stdio server with the command `uv` and the arguments `run --directory /full/path/to/gimp-mcp gimp_mcp_server.py`.

### 5. Check the connection

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
| `warp_region` | Push pixels in a direction, like the Warp Transform tool |

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
| [`agent_edit_demo.py`](agent_edit_demo.py) | Open, remove the background, warp the mouth into a smile, check snapshots, export |

<img src="gimp-screenshot1.png" alt="A face and a sheep drawn in GIMP through GIMP MCP" width="400">

*"Draw me a face and a sheep", drawn entirely through GIMP MCP.*

---

## Troubleshooting

**"Could not connect to GIMP"**
- Start GIMP and choose **Tools > MCP > Start MCP Server**. This is needed again after every GIMP restart.
- If GIMP was restarted while your AI client kept running, ask the assistant to run `restart_server`.

**The MCP menu is missing in GIMP**
- Look under **Tools > MCP**.
- Check that the plugin is in `plug-ins/gimp-mcp-plugin/gimp-mcp-plugin.py` inside the settings folder for your current GIMP version (**Edit > Preferences > Folders > Plug-ins**). Reinstall it after upgrading GIMP from one minor version to the next.
- On macOS and Linux, make sure the file is executable (`chmod +x`), then restart GIMP.
- Plugin errors appear in GIMP's **Windows > Dockable Dialogs > Error Console**.

**The assistant says a tool doesn't exist**
- Restart the AI client after updating GIMP MCP.
- In Claude Desktop, check that the tool is switched on in the gimp connector's tool settings.

**Changes to the plugin have no effect**
- GIMP loads the plugin when the server starts. After copying a new `gimp-mcp-plugin.py` into the `plug-ins` folder, restart GIMP and start the server again.

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
python run_tests.py                   # 62 checks across the tool categories
python tests/test_paint_stroke.py     # paint_stroke, list_brushes, sample_color
python tests/test_add_text_metadata.py
```

After changing `gimp-mcp-plugin.py`, copy it into GIMP's `plug-ins` folder and restart GIMP. After changing `gimp_mcp_server.py`, restart your AI client.

Contributions are welcome: open an issue or a pull request.

---

## License

GPL-3.0. See [LICENSE](LICENSE).
