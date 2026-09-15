#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GIMP MCP Plugin - Model Context Protocol integration for GIMP
Provides bitmap extraction and metadata access functionality
"""

import gi
gi.require_version('Gimp', '3.0')

from gi.repository import Gimp
from gi.repository import GLib
from gi.repository import GObject

import io
import sys
import json
import socket
import traceback
import threading
import base64
import tempfile
import os
import platform
import signal
import math
import contextlib

# Constants for configuration and thresholds
LARGE_SCALING_THRESHOLD = 4.0  # Warn if scaling ratio exceeds this value
MAX_REGION_SIZE = 8192  # Maximum region dimension in pixels
DEFAULT_TIMEOUT_SECONDS = 30  # Default timeout for operations
LISTEN_BACKLOG = 16  # Pending connections kept while commands run one at a time
PAINT_MAX_STROKES = 50  # Strokes accepted by one paint_stroke call
PAINT_MAX_POINTS = 2000  # Points accepted per paint_stroke stroke
PAINT_DEFAULT_BRUSH = "2. Hardness 050"
PAINT_METHODS = {  # paint_stroke tool name -> GIMP paint method
    "paintbrush": "gimp-paintbrush",
    "pencil":     "gimp-pencil",
    "airbrush":   "gimp-airbrush",
    "eraser":     "gimp-eraser",
    "smudge":     "gimp-smudge",
}
PAINT_STROKE_KEYS = {
    "points", "tool", "brush", "size", "color", "opacity", "hardness", "angle", "aspect_ratio",
    "spacing", "pressure", "taper_affects", "smooth", "mode", "strength",
}
IMAGE_NAME_PARASITE = "gimp-mcp-name"  # GIMP images have no settable name; new_canvas stores it here
IMAGE_DISPLAY_PARASITE = "gimp-mcp-display"  # ids of windows this plugin opened for an image
# The only color names Gegl.Color.new() parses; every other name silently becomes translucent cyan.
BASIC_COLOR_NAMES = ("black", "white", "gray", "silver", "red", "maroon", "yellow", "olive",
                     "lime", "green", "aqua", "teal", "blue", "navy", "fuchsia", "purple")
BLEND_MODES = {  # names set_layer_properties accepts; list_layers and get_context_state report them
    "NORMAL":      Gimp.LayerMode.NORMAL,
    "MULTIPLY":    Gimp.LayerMode.MULTIPLY,
    "SCREEN":      Gimp.LayerMode.SCREEN,
    "OVERLAY":     Gimp.LayerMode.OVERLAY,
    "DARKEN":      Gimp.LayerMode.DARKEN_ONLY,
    "LIGHTEN":     Gimp.LayerMode.LIGHTEN_ONLY,
    "DODGE":       Gimp.LayerMode.DODGE,
    "BURN":        Gimp.LayerMode.BURN,
    "HARD_LIGHT":  Gimp.LayerMode.HARDLIGHT,
    "SOFT_LIGHT":  Gimp.LayerMode.SOFTLIGHT,
    "DIFFERENCE":  Gimp.LayerMode.DIFFERENCE,
    "HUE":         Gimp.LayerMode.HSV_HUE,
    "SATURATION":  Gimp.LayerMode.HSV_SATURATION,
    "COLOR":       Gimp.LayerMode.HSL_COLOR,
    "LUMINOSITY":  Gimp.LayerMode.HSV_VALUE,
    "DISSOLVE":    Gimp.LayerMode.DISSOLVE,
}


def N_(message): return message
def _(message): return GLib.dgettext(None, message)


class _ThreadStdout:
    """sys.stdout that sends print() output from a thread running call_api code to that call's buffer.

    Replacing sys.stdout with the buffer also captured what the socket threads printed
    meanwhile (such as "Connected to client: ..."), and that ended up in call_api results.
    """

    def __init__(self, stream):
        self._stream = stream
        self.local = threading.local()

    def _target(self):
        buffer = getattr(self.local, "buffer", None)
        return self._stream if buffer is None else buffer

    def write(self, text):
        target = self._target()
        return len(text) if target is None else target.write(text)

    def flush(self):
        target = self._target()
        if target is not None:
            target.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def exec_and_get_results(command, context):
    stdout = sys.stdout
    if not isinstance(stdout, _ThreadStdout):
        stdout = sys.stdout = _ThreadStdout(stdout)
    buffer = io.StringIO()
    stdout.local.buffer = buffer
    try:
        exec(command, context)
    finally:
        # Stop capturing even when the command raises, so later output isn't swallowed.
        stdout.local.buffer = None
    return buffer.getvalue()


class MCPPlugin(Gimp.PlugIn):
    def __init__(self, host='localhost', port=9877):
        super().__init__()
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        self.context = {}
        exec("from gi.repository import Gimp", self.context)
        self.auto_disconnect_client = True
        # libgimp talks to GIMP over a single unlocked pipe. Two threads making
        # PDB calls at once interleave messages, which kills the plug-in and on
        # macOS leaves GIMP spinning at 100% CPU. Run one command at a time.
        self._exec_lock = threading.Lock()

    def do_set_i18n(self, procname):
        # Plugin has no translations; tell GIMP so it stops logging
        # "catalog directory does not exist" for every registered procedure.
        return False

    def do_query_procedures(self):
        """Register the plugin procedures."""
        return ["plug-in-mcp-server", "plug-in-mcp-check", "plug-in-mcp-restart"]

    def do_create_procedure(self, name):
        """Define the procedure properties."""
        if name == "plug-in-mcp-check":
            procedure = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PLUGIN, self._run_check, None)
            procedure.set_menu_label(_("Check MCP Server"))
            procedure.set_documentation(_("Check whether the MCP server is running"),
                                        _("Prints MCP server status to the GIMP console"),
                                        name)
            procedure.set_attribution("Viesar Lab", "Viesar Lab", "2026")
            procedure.add_enum_argument("run-mode", _("Run mode"), _("The run mode"),
                                        Gimp.RunMode, Gimp.RunMode.INTERACTIVE,
                                        GObject.ParamFlags.READWRITE)
            procedure.add_menu_path('<Image>/Tools/MCP')
            return procedure

        if name == "plug-in-mcp-restart":
            procedure = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PLUGIN, self._run_restart, None)
            procedure.set_menu_label(_("Restart MCP Server"))
            procedure.set_documentation(_("Restart the MCP server socket"),
                                        _("Drops and re-binds the MCP server socket on port 9877"),
                                        name)
            procedure.set_attribution("Viesar Lab", "Viesar Lab", "2026")
            procedure.add_enum_argument("run-mode", _("Run mode"), _("The run mode"),
                                        Gimp.RunMode, Gimp.RunMode.INTERACTIVE,
                                        GObject.ParamFlags.READWRITE)
            procedure.add_menu_path('<Image>/Tools/MCP')
            return procedure

        # Default: plug-in-mcp-server
        procedure = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PLUGIN, self.run, None)
        procedure.set_menu_label(_("Start MCP Server"))
        procedure.set_documentation(_("Starts an MCP server to control GIMP externally"),
                                    _("Starts an MCP server to control GIMP externally"),
                                    name)
        procedure.set_attribution("Viesar Lab", "Viesar Lab", "2026")
        procedure.add_enum_argument("run-mode", _("Run mode"), _("The run mode"),
                                    Gimp.RunMode, Gimp.RunMode.INTERACTIVE,
                                    GObject.ParamFlags.READWRITE)
        procedure.add_menu_path('<Image>/Tools/MCP')
        return procedure

    def _run_check(self, procedure, config, run_data):
        """Menu action: print server status."""
        status = "RUNNING" if self.running else "STOPPED"
        print(f"[MCP] Server status: {status} on port {self.port}")
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    def _run_restart(self, procedure, config, run_data):
        """Menu action: restart the server socket."""
        result = self._restart_server()
        print(f"[MCP] Restart result: {result}")
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    def shutdown_server(self, signum=None, frame=None):
        """Gracefully shutdown the server."""
        print(f"Shutdown signal received (signal: {signum}), closing MCP server...")
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        if hasattr(self, '_glib_loop') and self._glib_loop:
            self._glib_loop.quit()

    def _start_server_thread(self):
        """Core server loop — runs in a background thread."""
        self.running = True
        try:
            print("Creating socket...")
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.settimeout(1.0)
            self.socket.bind((self.host, self.port))
            self.socket.listen(LISTEN_BACKLOG)
            print(f"GimpMCP server started on {self.host}:{self.port}")

            while self.running:
                try:
                    client, address = self.socket.accept()
                    print(f"Connected to client: {address}")
                except socket.timeout:
                    continue
                except OSError:
                    break
                client_thread = threading.Thread(target=self._handle_client, args=(client,))
                client_thread.daemon = True
                client_thread.start()

            print("MCP server shutting down...")
            if self.socket:
                try:
                    self.socket.close()
                except Exception:
                    pass
                self.socket = None
            print("MCP server stopped")
        except Exception as e:
            print(f"Error in MCP server thread: {str(e)}")
            self.running = False

    def run(self, procedure, config, run_data):
        """Menu handler: start the server."""
        if self.running:
            print("MCP Server is already running")
            return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

        signal.signal(signal.SIGTERM, self.shutdown_server)
        signal.signal(signal.SIGINT, self.shutdown_server)

        # Server socket runs in a background thread
        server_thread = threading.Thread(target=self._start_server_thread, daemon=True)
        server_thread.start()

        # GLib main loop runs in the main thread — required for GIMP API calls
        # (all Gimp.* calls go over the wire protocol which needs GLib to dispatch)
        self._glib_loop = GLib.MainLoop()
        self._glib_loop.run()

        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    @staticmethod
    def _client_gone(client):
        """True if the client has closed its end of the connection."""
        try:
            client.setblocking(False)
            return client.recv(1, socket.MSG_PEEK) == b''
        except (BlockingIOError, InterruptedError):
            return False  # connected, nothing extra to read
        except OSError:
            return True
        finally:
            try:
                client.setblocking(True)
            except OSError:
                pass

    def _handle_client(self, client):
        """Handle connected client"""
        # print("Client handler started")
        buffer = b''

        # Receive data in chunks to handle larger payloads
        while True:
            data = client.recv(4096)
            # print(f"Received data: {data}")
            if not data:
                break
            buffer += data
            
            # Check if we have a complete message
            # For simplicity, assume messages end with newline or are complete JSON
            try:
                if isinstance(buffer, (bytes, bytearray)):
                    request = buffer.decode('utf-8')
                else:
                    request = str(buffer)
                
                # Try to parse as JSON to see if complete
                if request.strip():
                    json.loads(request)  # This will raise if incomplete
                    break
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Continue receiving if JSON is incomplete
                continue
        
        if not buffer:
            print("Client disconnected")
            return

        if isinstance(buffer, (bytes, bytearray)):
            request = buffer.decode('utf-8')
        else:
            request = str(buffer)
        
        # print(f"Parsed request: {request}")
        with self._exec_lock:
            # A client that gave up while waiting for the lock has closed its
            # socket; running its command now would apply it out of order.
            if self._client_gone(client):
                print("Client disconnected before its command ran; skipping")
                client.close()
                return
            response = self.execute_command(request)
        print(f"response type: {type(response)}")
        
        if isinstance(response, dict):
            response_str = json.dumps(response)
        else:
            response_str = str(response)
            
        # Send response in chunks for large data
        response_bytes = response_str.encode('utf-8')
        bytes_sent = 0
        while bytes_sent < len(response_bytes):
            chunk = response_bytes[bytes_sent:bytes_sent + 8192]
            client.sendall(chunk)
            bytes_sent += len(chunk)
            
        if self.auto_disconnect_client:
            client.close()
        return

    def execute_command(self, request):
        """Execute commands in GIMP's main thread."""
        try:
            if request == "disable_auto_disconnect":
                self.auto_disconnect_client = False
                return {"status": "success", "results": "OK"}
            j = json.loads(request)
            if "type" in j and j["type"] == "get_image_bitmap":
                params = j.get("params", {})
                return self._get_current_image_bitmap(params)
            elif "type" in j and j["type"] == "get_image_metadata":
                return self._get_current_image_metadata(j.get("params", {}))
            elif "type" in j and j["type"] == "get_gimp_info":
                return self._get_gimp_info()
            elif "type" in j and j["type"] == "get_context_state":
                return self._get_context_state()
            elif "type" in j and j["type"] == "check_server":
                return {"status": "success", "results": {"running": True, "port": self.port}}
            elif "type" in j and j["type"] == "restart_server":
                return self._restart_server()
            elif "type" in j and j["type"] == "new_canvas":
                params = j.get("params", {})
                return self._new_canvas(params)
            # ── Category 1: File Operations ──────────────────────────────────
            elif "type" in j and j["type"] == "open_image":
                return self._open_image(j.get("params", {}))
            elif "type" in j and j["type"] == "save_xcf":
                return self._save_xcf(j.get("params", {}))
            elif "type" in j and j["type"] == "export_image":
                return self._export_image(j.get("params", {}))
            elif "type" in j and j["type"] == "batch_export":
                return self._batch_export(j.get("params", {}))
            # ── Category 2: Image Adjustments ────────────────────────────────
            elif "type" in j and j["type"] == "auto_levels":
                return self._auto_levels(j.get("params", {}))
            elif "type" in j and j["type"] == "adjust_curves":
                return self._adjust_curves(j.get("params", {}))
            elif "type" in j and j["type"] == "adjust_brightness_contrast":
                return self._adjust_brightness_contrast(j.get("params", {}))
            elif "type" in j and j["type"] == "adjust_hue_saturation":
                return self._adjust_hue_saturation(j.get("params", {}))
            elif "type" in j and j["type"] == "adjust_color_balance":
                return self._adjust_color_balance(j.get("params", {}))
            elif "type" in j and j["type"] == "sharpen":
                return self._sharpen(j.get("params", {}))
            elif "type" in j and j["type"] == "blur":
                return self._blur(j.get("params", {}))
            elif "type" in j and j["type"] == "denoise":
                return self._denoise(j.get("params", {}))
            elif "type" in j and j["type"] == "desaturate":
                return self._desaturate(j.get("params", {}))
            elif "type" in j and j["type"] == "invert_colors":
                return self._invert_colors(j.get("params", {}))
            # ── Category 3: Resize & Transform ───────────────────────────────
            elif "type" in j and j["type"] == "scale_image":
                return self._scale_image(j.get("params", {}))
            elif "type" in j and j["type"] == "scale_to_fit":
                return self._scale_to_fit(j.get("params", {}))
            elif "type" in j and j["type"] == "crop_to_selection":
                return self._crop_to_selection(j.get("params", {}))
            elif "type" in j and j["type"] == "crop_to_rect":
                return self._crop_to_rect(j.get("params", {}))
            elif "type" in j and j["type"] == "rotate_image":
                return self._rotate_image(j.get("params", {}))
            elif "type" in j and j["type"] == "flip_image":
                return self._flip_image(j.get("params", {}))
            elif "type" in j and j["type"] == "resize_canvas":
                return self._resize_canvas(j.get("params", {}))
            # ── Category 4: Selections ────────────────────────────────────────
            elif "type" in j and j["type"] == "select_rectangle":
                return self._select_rectangle(j.get("params", {}))
            elif "type" in j and j["type"] == "select_ellipse":
                return self._select_ellipse(j.get("params", {}))
            elif "type" in j and j["type"] == "select_by_color":
                return self._select_by_color(j.get("params", {}))
            elif "type" in j and j["type"] == "select_all":
                return self._select_all(j.get("params", {}))
            elif "type" in j and j["type"] == "select_none":
                return self._select_none(j.get("params", {}))
            elif "type" in j and j["type"] == "invert_selection":
                return self._invert_selection(j.get("params", {}))
            elif "type" in j and j["type"] == "modify_selection":
                return self._modify_selection(j.get("params", {}))
            # ── Category 5: Layer Operations ──────────────────────────────────
            elif "type" in j and j["type"] == "create_layer":
                return self._create_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "duplicate_layer":
                return self._duplicate_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "delete_layer":
                return self._delete_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "rename_layer":
                return self._rename_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "set_layer_properties":
                return self._set_layer_properties(j.get("params", {}))
            elif "type" in j and j["type"] == "reorder_layer":
                return self._reorder_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "flatten_image":
                return self._flatten_image(j.get("params", {}))
            elif "type" in j and j["type"] == "merge_visible_layers":
                return self._merge_visible_layers(j.get("params", {}))
            elif "type" in j and j["type"] == "list_layers":
                return self._list_layers(j.get("params", {}))
            # ── Category 6: Color & Paint ─────────────────────────────────────
            elif "type" in j and j["type"] == "fill_layer":
                return self._fill_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "fill_selection":
                return self._fill_selection(j.get("params", {}))
            elif "type" in j and j["type"] == "set_colors":
                return self._set_colors(j.get("params", {}))
            elif "type" in j and j["type"] == "draw_line":
                return self._draw_line(j.get("params", {}))
            elif "type" in j and j["type"] == "draw_rectangle":
                return self._draw_rectangle(j.get("params", {}))
            elif "type" in j and j["type"] == "draw_ellipse":
                return self._draw_ellipse(j.get("params", {}))
            elif "type" in j and j["type"] == "fill_rectangle":
                return self._fill_rectangle(j.get("params", {}))
            elif "type" in j and j["type"] == "fill_ellipse":
                return self._fill_ellipse(j.get("params", {}))
            elif "type" in j and j["type"] == "gradient_fill":
                return self._gradient_fill(j.get("params", {}))
            elif "type" in j and j["type"] == "paint_stroke":
                return self._paint_stroke(j.get("params", {}))
            elif "type" in j and j["type"] == "list_brushes":
                return self._list_brushes(j.get("params", {}))
            elif "type" in j and j["type"] == "sample_color":
                return self._sample_color(j.get("params", {}))
            # ── Category 7: Text ──────────────────────────────────────────────
            elif "type" in j and j["type"] == "add_text":
                return self._add_text(j.get("params", {}))
            elif "type" in j and j["type"] == "edit_text":
                return self._edit_text(j.get("params", {}))
            elif "type" in j and j["type"] == "list_fonts":
                return self._list_fonts(j.get("params", {}))
            # ── Category 8: Filters & Effects ────────────────────────────────
            elif "type" in j and j["type"] == "apply_drop_shadow":
                return self._apply_drop_shadow(j.get("params", {}))
            elif "type" in j and j["type"] == "apply_gaussian_blur":
                return self._apply_gaussian_blur(j.get("params", {}))
            elif "type" in j and j["type"] == "apply_pixelate":
                return self._apply_pixelate(j.get("params", {}))
            elif "type" in j and j["type"] == "apply_emboss":
                return self._apply_emboss(j.get("params", {}))
            elif "type" in j and j["type"] == "apply_vignette":
                return self._apply_vignette(j.get("params", {}))
            elif "type" in j and j["type"] == "apply_noise":
                return self._apply_noise(j.get("params", {}))
            # ── Category 9: Export Pipelines ──────────────────────────────────
            elif "type" in j and j["type"] == "export_icon_sizes":
                return self._export_icon_sizes(j.get("params", {}))
            elif "type" in j and j["type"] == "export_web_optimized":
                return self._export_web_optimized(j.get("params", {}))
            elif "type" in j and j["type"] == "batch_resize":
                return self._batch_resize(j.get("params", {}))
            elif "type" in j and j["type"] == "export_sprite_sheet":
                return self._export_sprite_sheet(j.get("params", {}))
            elif "type" in j and j["type"] == "export_social_media_kit":
                return self._export_social_media_kit(j.get("params", {}))
            # ── Category 10: Utility ──────────────────────────────────────────
            elif "type" in j and j["type"] == "list_images":
                return self._list_images(j.get("params", {}))
            elif "type" in j and j["type"] == "set_active_image":
                return self._set_active_image(j.get("params", {}))
            elif "type" in j and j["type"] == "convert_color_mode":
                return self._convert_color_mode(j.get("params", {}))
            elif "type" in j and j["type"] == "close_image":
                return self._close_image(j.get("params", {}))
            elif "type" in j and j["type"] == "get_selection_bounds":
                return self._get_selection_bounds(j.get("params", {}))
            elif "type" in j and j["type"] == "get_pixel_color":
                return self._get_pixel_color(j.get("params", {}))
            elif "type" in j and j["type"] == "get_histogram":
                return self._get_histogram(j.get("params", {}))
            elif "type" in j and j["type"] == "warp_region":
                return self._warp_region(j.get("params", {}))
            elif "cmds" in j:
                a = ['python-fu-exec', j["cmds"]]
            else:
                p = j["params"]
                a = p['args']

            # Protect against empty args list
            if len(a) == 0:
                return {
                    "status": "error",
                    "error": "No command arguments provided"
                }

            # pyGObject-eval is the documented name; python-fu-eval is the original one.
            if a[0] in ('pyGObject-eval', 'python-fu-eval'):
                if len(a) > 0:
                    print(f"evaluating exprs: {a[1]}")
                    vals = [str(eval(e)) for e in a[1]]
                    results = {
                        "status": "success",
                        "results": vals
                    }
                else:
                    results = {
                    "status": "success",
                    "results": "[NULL]"
                }
                print(f"expression result: {results}")
                return results
            else:
                outputs = ["OK"]
                if len(a) > 0:
                    print(f"Executing commands: {a[1]}")
                    outputs = [exec_and_get_results(c, self.context) for c in a[1]]
                else:
                    print("no command to execute")
                result = {
                    "status": "success",
                    "results": outputs
                }

                print(f"Command result: {result}")
                return result

        except Exception as e:
            error_msg = f"Error executing command: {str(e)}\n{traceback.format_exc()}"
            print(error_msg)
            return {
                "status": "error",
                "error": str(e),
                "traceback": traceback.format_exc()
            }

    def _get_current_image_bitmap(self, params=None):
        """Get the current image as a base64-encoded bitmap with optional scaling and region selection."""
        try:
            if params is None:
                params = {}
                
            print(f"Getting current image bitmap with params: {params}")

            # Extract parameters
            max_width = params.get("max_width")
            max_height = params.get("max_height")
            
            # Extract region parameters if provided
            region = params.get("region", {})
            
            # Validate region parameters if provided
            if region:
                unknown = sorted(set(region) - {"origin_x", "origin_y", "width", "height", "max_width", "max_height"})
                if unknown:
                    return {
                        "status": "error",
                        "error": f"Unknown region keys {unknown}; use origin_x, origin_y, width, height "
                                 f"and optionally max_width, max_height"
                    }
                # Validate region parameter types
                for key, expected_type in [("origin_x", int), ("origin_y", int), 
                                         ("width", int), ("height", int),
                                         ("max_width", int), ("max_height", int)]:
                    if key in region and region[key] is not None:
                        if not isinstance(region[key], expected_type):
                            return {
                                "status": "error",
                                "error": f"Region parameter '{key}' must be of type {expected_type.__name__}, got {type(region[key]).__name__}"
                            }
                        if region[key] < 0:
                            return {
                                "status": "error", 
                                "error": f"Region parameter '{key}' must be non-negative, got {region[key]}"
                            }
            
            origin_x = region.get("origin_x")
            origin_y = region.get("origin_y")
            region_width = region.get("width")
            region_height = region.get("height")
            scaled_to_width = region.get("max_width")  # Region scaling uses max_width/max_height
            scaled_to_height = region.get("max_height")

            # Snapshot the requested image; images[0] is only the most recently opened one.
            original_image = self._get_image(int(params.get("image_index", 0)))
            
            # Get original image dimensions
            orig_img_width = original_image.get_width()
            orig_img_height = original_image.get_height()
            
            # Determine working image and region
            working_image = None
            should_delete_working = False
            
            # Case 1: Region selection
            if any(param is not None for param in [origin_x, origin_y, region_width, region_height]):
                print("Processing region extraction...")
                
                # Validate region parameters
                if origin_x is None or origin_y is None or region_width is None or region_height is None:
                    return {
                        "status": "error",
                        "error": "For region selection, all parameters are required: origin_x, origin_y, width, height"
                    }
                
                # Validate region bounds
                if (origin_x < 0 or origin_y < 0 or 
                    origin_x + region_width > orig_img_width or 
                    origin_y + region_height > orig_img_height):
                    return {
                        "status": "error",
                        "error": f"Region bounds invalid. Image size: {orig_img_width}x{orig_img_height}, "
                               f"requested region: ({origin_x},{origin_y}) {region_width}x{region_height}"
                    }
                
                # Crop a duplicate so the region shows every visible layer (not just the
                # top one) and the user's selection and clipboard are left untouched.
                working_image = original_image.duplicate()
                should_delete_working = True
                working_image.crop(region_width, region_height, origin_x, origin_y)
                
            else:
                # Case 2: Full image
                print("Processing full image...")
                working_image = original_image
                should_delete_working = False
            
            # Now handle scaling if needed
            final_image = working_image
            should_delete_final = should_delete_working
            
            # Calculate target dimensions
            current_width = working_image.get_width()
            current_height = working_image.get_height()
            target_width = current_width
            target_height = current_height
            
            # Determine scaling target
            if scaled_to_width is not None and scaled_to_height is not None:
                # Region scaling - use scaled_to dimensions
                max_w, max_h = scaled_to_width, scaled_to_height
            elif max_width is not None and max_height is not None:
                # Full image scaling - use max dimensions
                max_w, max_h = max_width, max_height
            else:
                max_w = max_h = None
            
            # Apply center inside scaling if target dimensions provided
            if max_w is not None and max_h is not None:
                # Calculate center inside scaling
                aspect_ratio = current_width / current_height
                max_aspect_ratio = max_w / max_h
                
                if aspect_ratio > max_aspect_ratio:
                    # Width is the limiting factor
                    target_width = max_w
                    target_height = int(max_w / aspect_ratio)
                else:
                    # Height is the limiting factor
                    target_height = max_h
                    target_width = int(max_h * aspect_ratio)
                
                print(f"Scaling from {current_width}x{current_height} to {target_width}x{target_height}")
                
                # Scale the image if dimensions changed
                if target_width != current_width or target_height != current_height:
                    # Create scaled image
                    final_image = working_image.duplicate()
                    should_delete_final = True
                    
                    # Scale the image with timeout consideration for large operations
                    scaling_ratio = (target_width * target_height) / (current_width * current_height)
                    if scaling_ratio > LARGE_SCALING_THRESHOLD:  # Scaling up significantly
                        print(f"Warning: Large scaling operation detected (ratio: {scaling_ratio:.2f}). This may take time.")
                    
                    try:
                        final_image.scale(target_width, target_height)
                    except (RuntimeError) as scale_error:
                        # Clean up and return error for scaling failures
                        if should_delete_final:
                            try:
                                final_image.delete()
                            except (AttributeError, RuntimeError):
                                pass
                        raise RuntimeError(f"Failed to scale image from {current_width}x{current_height} to {target_width}x{target_height}: {scale_error}")
        
            # Create a temporary file for export
            temp_fd, temp_path = tempfile.mkstemp(suffix='.png')
            os.close(temp_fd)  # Close the file descriptor as GIMP will handle the file
            
            try:
                # Export the final image as PNG
                # Get all layers - we'll export the flattened image
                layers = final_image.get_layers()
                if not layers:
                    return {
                        "status": "error", 
                        "error": "No layers found in the processed image"
                    }
                
                # For PNG export, we can use all layers or the active layer
                try:
                    drawable = (final_image.get_selected_layers() or final_image.get_layers() or [None])[0]
                except (AttributeError, RuntimeError):
                    # If get_active_layer doesn't exist or fails, use the first layer
                    drawable = layers[0]
                
                if not drawable:
                    drawable = layers[0]
                
                # Export the image to PNG
                try:
                    # In GIMP 3.0, use the simplified export approach
                    from gi.repository import Gio
                    file_obj = Gio.File.new_for_path(temp_path)
                    
                    # Use file-png-export with the correct parameters for GIMP 3.0
                    export_proc = Gimp.get_pdb().lookup_procedure('file-png-export')
                    if not export_proc:
                        return {
                            "status": "error",
                            "error": "PNG export procedure not found"
                        }
                    
                    export_config = export_proc.create_config()
                    export_config.set_property('image', final_image)
                    export_config.set_property('file', file_obj)
                    # Try different property names that might exist
                    try:
                        export_config.set_property('drawable', drawable)
                    except Exception:
                        try:
                            export_config.set_property('drawables', [drawable])
                        except Exception:
                            # Some export procedures might not need drawable specification
                            pass
                    
                    result = export_proc.run(export_config)
                    print(f"Export result: {result}")
                    if result.index(0) != Gimp.PDBStatusType.SUCCESS:
                        raise RuntimeError(f"PNG export failed: {Gimp.get_pdb().get_last_error()}")
                    
                except Exception as export_error:
                    print(f"Export error: {export_error}")
                    # Fallback: try using the PDB directly with correct arguments
                    try:
                        from gi.repository import Gio
                        file_obj = Gio.File.new_for_path(temp_path)
                        
                        # Try alternative approach using Gimp.file_save with correct number of arguments
                        Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, final_image, file_obj)
                        print("Fallback export successful")
                    except Exception as fallback_error:
                        print(f"Fallback export error: {fallback_error}")
                        # Try another fallback using gimp-file-save PDB procedure
                        try:
                            pdb = Gimp.get_pdb()
                            save_proc = pdb.lookup_procedure('gimp-file-save')
                            if save_proc:
                                save_config = save_proc.create_config()
                                save_config.set_property('image', final_image)
                                save_config.set_property('file', file_obj)
                                save_result = save_proc.run(save_config)
                                print(f"PDB save result: {save_result}")
                            else:
                                return {
                                    "status": "error",
                                    "error": f"All export methods failed: {export_error}, fallback: {fallback_error}"
                                }
                        except Exception as pdb_error:
                            return {
                                "status": "error",
                                "error": f"All export methods failed: {export_error}, fallback: {fallback_error}, PDB: {pdb_error}"
                            }
                
                # Read the exported file and encode as base64
                with open(temp_path, 'rb') as f:
                    image_data = f.read()
                if not image_data:
                    return {"status": "error", "error": "Exporting the snapshot produced an empty file"}
                encoded_image = base64.b64encode(image_data).decode('utf-8')
                
                # Get final image metadata
                final_width = final_image.get_width()
                final_height = final_image.get_height()
                
                return {
                    "status": "success",
                    "results": {
                        "image_data": encoded_image,
                        "format": "png",
                        "width": final_width,
                        "height": final_height,
                        "original_width": orig_img_width,
                        "original_height": orig_img_height,
                        "encoding": "base64",
                        "processing_applied": {
                            "region_extracted": any(param is not None for param in [origin_x, origin_y, region_width, region_height]),
                            "scaled": target_width != current_width or target_height != current_height,
                            "region_coords": {"x": origin_x, "y": origin_y, "w": region_width, "h": region_height} if origin_x is not None else None
                        }
                    }
                }
                
            finally:
                # Clean up temporary images
                if should_delete_final and final_image != working_image:
                    try:
                        final_image.delete()
                    except (AttributeError, RuntimeError) as e:
                        print(f"Warning: Failed to delete final temporary image: {e}")
                if should_delete_working and working_image != original_image:
                    try:
                        working_image.delete()
                    except (AttributeError, RuntimeError) as e:
                        print(f"Warning: Failed to delete working temporary image: {e}")
                        
                # Clean up the temporary file
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                    
        except (RuntimeError, AttributeError, OSError, ValueError) as e:
            return {
                "status": "error",
                "error": f"Processing error: {str(e)}",
                "traceback": traceback.format_exc()
            }

    def _get_current_image_metadata(self, params=None):
        """Get comprehensive metadata about the current image without bitmap data."""
        try:
            print("Getting current image metadata...")
            
            # Describe the requested image; images[0] is only the most recently opened one.
            image = self._get_image(int((params or {}).get("image_index", 0)))
            
            # Basic image properties
            width = image.get_width()
            height = image.get_height()
            
            # Get image type and base type
            base_type = image.get_base_type()
            base_type_str = self._base_type_to_string(base_type)
            
            # Get precision and color profile info
            precision = image.get_precision()
            precision_str = self._precision_to_string(precision)
            
            # Get layers information
            layers = image.get_layers()
            layers_info = []
            for i, layer in enumerate(layers):
                try:
                    layer_info = {
                        "name": layer.get_name(),
                        "visible": layer.get_visible(),
                        "opacity": layer.get_opacity(),
                        "width": layer.get_width(),
                        "height": layer.get_height(),
                        "has_alpha": layer.has_alpha(),
                        "is_group": hasattr(layer, 'get_children') and callable(getattr(layer, 'get_children')),
                        "layer_type": self._get_layer_type_string(layer)
                    }
                    # Try to get layer mode if available
                    try:
                        layer_info["blend_mode"] = str(layer.get_mode())
                    except Exception:
                        layer_info["blend_mode"] = "unknown"
                    
                    layers_info.append(layer_info)
                except Exception as layer_error:
                    print(f"Error getting layer {i} info: {layer_error}")
                    layers_info.append({
                        "name": f"Layer {i}",
                        "error": str(layer_error)
                    })
            
            # Get channels information
            channels = image.get_channels()
            channels_info = []
            for i, channel in enumerate(channels):
                try:
                    channel_info = {
                        "name": channel.get_name(),
                        "visible": channel.get_visible(),
                        "opacity": channel.get_opacity(),
                        "color": str(channel.get_color()) if hasattr(channel, 'get_color') else "unknown"
                    }
                    channels_info.append(channel_info)
                except Exception as channel_error:
                    print(f"Error getting channel {i} info: {channel_error}")
                    channels_info.append({
                        "name": f"Channel {i}",
                        "error": str(channel_error)
                    })
            
            # Get paths/vectors information
            paths = []
            try:
                image_paths = image.get_paths()
                for i, path in enumerate(image_paths):
                    try:
                        path_info = {
                            "name": path.get_name(),
                            "visible": path.get_visible(),
                            "num_strokes": len(path.get_strokes()) if hasattr(path, 'get_strokes') else 0
                        }
                        paths.append(path_info)
                    except Exception as path_error:
                        print(f"Error getting path {i} info: {path_error}")
                        paths.append({
                            "name": f"Path {i}",
                            "error": str(path_error)
                        })
            except Exception as paths_error:
                print(f"Error getting paths: {paths_error}")
            
            # Get file information if available
            file_info = {}
            try:
                image_file = image.get_file()
                if image_file:
                    file_info = {
                        "path": image_file.get_path() if hasattr(image_file, 'get_path') else None,
                        "uri": image_file.get_uri() if hasattr(image_file, 'get_uri') else None,
                        "basename": image_file.get_basename() if hasattr(image_file, 'get_basename') else None
                    }
            except Exception as file_error:
                print(f"Error getting file info: {file_error}")
                file_info = {"error": str(file_error)}
            
            # Get resolution information
            resolution_x = resolution_y = None
            try:
                _ok, resolution_x, resolution_y = image.get_resolution()
            except Exception as res_error:
                print(f"Error getting resolution: {res_error}")
            
            # Check if image has unsaved changes
            is_dirty = False
            try:
                is_dirty = image.is_dirty()
            except Exception as dirty_error:
                print(f"Error getting dirty status: {dirty_error}")
            
            metadata = {
                "basic": {
                    "width": width,
                    "height": height,
                    "base_type": base_type_str,
                    "precision": precision_str,
                    "resolution_x": resolution_x,
                    "resolution_y": resolution_y,
                    "is_dirty": is_dirty
                },
                "structure": {
                    "num_layers": len(layers),
                    "num_channels": len(channels),
                    "num_paths": len(paths),
                    "layers": layers_info,
                    "channels": channels_info,
                    "paths": paths
                },
                "file": file_info
            }
            
            return {
                "status": "success",
                "results": metadata
            }
            
        except Exception as e:
            error_msg = f"Error getting image metadata: {str(e)}\n{traceback.format_exc()}"
            print(error_msg)
            return {
                "status": "error",
                "error": str(e),
                "traceback": traceback.format_exc()
            }

    def _base_type_to_string(self, base_type):
        """Convert GIMP base type enum to string."""
        try:
            base_type_map = {
                Gimp.ImageBaseType.RGB: "RGB",
                Gimp.ImageBaseType.GRAY: "Grayscale",
                Gimp.ImageBaseType.INDEXED: "Indexed"
            }
            return base_type_map.get(base_type, f"Unknown ({base_type})")
        except Exception:
            return str(base_type)

    def _precision_to_string(self, precision):
        """Convert GIMP precision enum to readable string."""
        try:
            precision_map = {
                100: "u8",      # Gimp.Precision.U8_LINEAR
                150: "u8-gamma", # Gimp.Precision.U8_GAMMA  
                200: "u16",     # Gimp.Precision.U16_LINEAR
                250: "u16-gamma", # Gimp.Precision.U16_GAMMA
                300: "u32",     # Gimp.Precision.U32_LINEAR
                350: "u32-gamma", # Gimp.Precision.U32_GAMMA
                500: "half",    # Gimp.Precision.HALF_LINEAR
                550: "half-gamma", # Gimp.Precision.HALF_GAMMA
                600: "float",   # Gimp.Precision.FLOAT_LINEAR
                650: "float-gamma", # Gimp.Precision.FLOAT_GAMMA
                700: "double",  # Gimp.Precision.DOUBLE_LINEAR
                750: "double-gamma" # Gimp.Precision.DOUBLE_GAMMA
            }
            return precision_map.get(int(precision), f"precision-{precision}")
        except Exception:
            return str(precision)
            
    def _get_layer_type_string(self, layer):
        """Get layer type string with compatibility for different GIMP versions."""
        try:
            # Try different methods to get layer type
            if hasattr(layer, 'get_type'):
                return str(layer.get_type())
            elif hasattr(layer, 'get_image_type'):
                return str(layer.get_image_type())
            elif hasattr(layer, 'type'):
                return str(layer.type)
            else:
                # Fallback - determine from layer properties
                if layer.has_alpha():
                    return "RGBA"
                else:
                    return "RGB"
        except Exception as e:
            print(f"Warning: Could not determine layer type: {e}")
            return "unknown"

    def _get_gimp_info(self):
        """Get comprehensive information about GIMP installation and environment."""
        try:
            print("Getting GIMP environment information...")
            
            gimp_info = {}
            
            # Basic GIMP version and build information
            try:
                version_info = {}
                
                # Try different methods to get version info
                try:
                    # Try the version() method if it exists
                    if hasattr(Gimp, 'version'):
                        version_info["version_method"] = str(Gimp.version())
                except Exception as v_error:
                    version_info["version_method_error"] = str(v_error)
                
                # Try to get version from constants if they exist
                for attr in ['MAJOR_VERSION', 'MINOR_VERSION', 'MICRO_VERSION']:
                    try:
                        if hasattr(Gimp, attr):
                            version_info[attr.lower()] = getattr(Gimp, attr)
                    except Exception as attr_error:
                        version_info[f"{attr.lower()}_error"] = str(attr_error)
                
                # Get available version-related attributes
                version_attrs = [attr for attr in dir(Gimp) if 'version' in attr.lower()]
                if version_attrs:
                    version_info["available_version_attributes"] = version_attrs
                
                # Try to get version string from any available source
                version_string = "Unknown"
                try:
                    # Check if there's a version string constant
                    if hasattr(Gimp, 'VERSION'):
                        version_string = str(Gimp.VERSION)
                    elif hasattr(Gimp, 'version_string'):
                        version_string = str(Gimp.version_string())
                    elif hasattr(Gimp, 'get_version'):
                        version_string = str(Gimp.get_version())
                except Exception:
                    pass
                
                version_info["detected_version"] = version_string
                version_info["gimp_module_type"] = str(type(Gimp))
                
                gimp_info["version"] = version_info
                
            except Exception as version_error:
                print(f"Error getting version info: {version_error}")
                gimp_info["version"] = {"error": str(version_error)}
            
            # Installation and directory information
            try:
                directories = {}
                
                # Safely try each directory method
                directory_methods = [
                    ('user_directory', 'directory'),
                    ('system_data_directory', 'data_directory'), 
                    ('locale_directory', 'locale_directory'),
                    ('plugin_directory', 'plug_in_directory'),
                    ('sysconf_directory', 'sysconf_directory')
                ]
                
                for dir_name, method_name in directory_methods:
                    try:
                        if hasattr(Gimp, method_name):
                            method = getattr(Gimp, method_name)
                            if callable(method):
                                directories[dir_name] = str(method())
                            else:
                                directories[dir_name] = str(method)
                        else:
                            directories[f"{dir_name}_not_available"] = True
                    except Exception as method_error:
                        directories[f"{dir_name}_error"] = str(method_error)
                
                # List available directory-related methods
                dir_attrs = [attr for attr in dir(Gimp) if 'dir' in attr.lower()]
                directories["available_directory_methods"] = dir_attrs
                
                gimp_info["directories"] = directories
                
            except Exception as dir_error:
                print(f"Error getting directory info: {dir_error}")
                gimp_info["directories"] = {"error": str(dir_error)}
            
            # Current session information
            try:
                images = Gimp.get_images()
                gimp_info["session"] = {
                    "num_open_images": len(images),
                    "has_open_images": len(images) > 0,
                    "open_image_files": []
                }
                
                # Get file information for open images
                for i, image in enumerate(images):
                    try:
                        image_file = image.get_file()
                        file_info = {
                            "index": i,
                            "width": image.get_width(),
                            "height": image.get_height(),
                            "base_type": self._base_type_to_string(image.get_base_type()),
                            "is_dirty": image.is_dirty() if hasattr(image, 'is_dirty') else None
                        }
                        
                        if image_file:
                            file_info.update({
                                "path": image_file.get_path() if hasattr(image_file, 'get_path') else None,
                                "basename": image_file.get_basename() if hasattr(image_file, 'get_basename') else None
                            })
                        else:
                            file_info["path"] = "Untitled"
                            
                        gimp_info["session"]["open_image_files"].append(file_info)
                    except Exception as image_error:
                        print(f"Error getting image {i} info: {image_error}")
                        gimp_info["session"]["open_image_files"].append({
                            "index": i,
                            "error": str(image_error)
                        })
                        
            except Exception as session_error:
                print(f"Error getting session info: {session_error}")
                gimp_info["session"] = {"error": str(session_error)}
            
            # PDB (Procedure Database) information
            try:
                pdb = Gimp.get_pdb()
                pdb_info = {
                    "available": pdb is not None,
                    "type": str(type(pdb)) if pdb else None
                }
                
                # Try to get some example procedures
                if pdb:
                    sample_procedures = []
                    try:
                        # Test some common procedures
                        test_procs = ['file-png-export', 'gimp-file-save', 'gimp-image-new', 'python-fu-console']
                        for proc_name in test_procs:
                            try:
                                proc = pdb.lookup_procedure(proc_name)
                                sample_procedures.append({
                                    "name": proc_name,
                                    "available": proc is not None,
                                    "type": str(type(proc)) if proc else None
                                })
                            except Exception:
                                sample_procedures.append({
                                    "name": proc_name,
                                    "available": False,
                                    "error": "lookup_failed"
                                })
                    except Exception as proc_error:
                        print(f"Error testing procedures: {proc_error}")
                    
                    pdb_info["sample_procedures"] = sample_procedures
                
                gimp_info["pdb"] = pdb_info
                
            except Exception as pdb_error:
                print(f"Error getting PDB info: {pdb_error}")
                gimp_info["pdb"] = {"error": str(pdb_error)}
            
            # Context and environment information
            try:
                context_info = {}
                
                # Try to get current context information
                try:
                    fg_color = Gimp.context_get_foreground()
                    context_info["foreground_color"] = str(fg_color) if fg_color else None
                except Exception:
                    context_info["foreground_color"] = "unavailable"
                
                try:
                    bg_color = Gimp.context_get_background()
                    context_info["background_color"] = str(bg_color) if bg_color else None
                except Exception:
                    context_info["background_color"] = "unavailable"
                
                try:
                    brush_size = Gimp.context_get_brush_size()
                    context_info["brush_size"] = brush_size if brush_size else None
                except Exception:
                    context_info["brush_size"] = "unavailable"
                
                gimp_info["context"] = context_info
                
            except Exception as context_error:
                print(f"Error getting context info: {context_error}")
                gimp_info["context"] = {"error": str(context_error)}
            
            # Capabilities and features
            try:
                capabilities = {
                    "has_python_console": True,  # We're running Python
                    "mcp_server_running": True,  # We're responding to MCP requests
                    "supports_image_export": True,  # We have the bitmap export function
                    "supports_metadata_export": True,  # We have the metadata function
                    "supports_gimp_info": True,  # We have the gimp info function
                    "api_version": "3.0+",
                    "python_version": sys.version,
                    "available_modules": [],
                    "gimp_module_attributes": len(dir(Gimp)),
                    "gimp_methods": [attr for attr in dir(Gimp) if callable(getattr(Gimp, attr, None))][:20]  # First 20 methods
                }
                
                # Test for available Python modules
                test_modules = ['gi.repository.Gimp', 'gi.repository.Gegl', 'gi.repository.Gio', 'json', 'base64', 'tempfile']
                for module_name in test_modules:
                    try:
                        if module_name == 'gi.repository.Gimp':
                            # Already imported
                            capabilities["available_modules"].append({"name": module_name, "available": True})
                        elif module_name == 'gi.repository.Gegl':
                            from gi.repository import Gegl  # noqa: F401
                            capabilities["available_modules"].append({"name": module_name, "available": True})
                        elif module_name == 'gi.repository.Gio':
                            from gi.repository import Gio  # noqa: F401
                            capabilities["available_modules"].append({"name": module_name, "available": True})
                        else:
                            __import__(module_name)
                            capabilities["available_modules"].append({"name": module_name, "available": True})
                    except ImportError:
                        capabilities["available_modules"].append({"name": module_name, "available": False})
                    except Exception as mod_error:
                        capabilities["available_modules"].append({"name": module_name, "available": False, "error": str(mod_error)})
                
                gimp_info["capabilities"] = capabilities
                
            except Exception as cap_error:
                print(f"Error getting capabilities: {cap_error}")
                gimp_info["capabilities"] = {"error": str(cap_error)}
            
            # System and platform information
            try:
                
                system_info = {
                    "platform": platform.platform(),
                    "system": platform.system(),
                    "machine": platform.machine(),
                    "python_version": platform.python_version(),
                    "environment_vars": {
                        "HOME": os.environ.get("HOME"),
                        "USER": os.environ.get("USER"), 
                        "GIMP_PLUG_IN_DIR": os.environ.get("GIMP_PLUG_IN_DIR"),
                        "GIMP_DATA_DIR": os.environ.get("GIMP_DATA_DIR")
                    }
                }
                
                gimp_info["system"] = system_info
                
            except Exception as sys_error:
                print(f"Error getting system info: {sys_error}")
                gimp_info["system"] = {"error": str(sys_error)}
            
            return {
                "status": "success",
                "results": gimp_info
            }
            
        except Exception as e:
            error_msg = f"Error getting GIMP info: {str(e)}\n{traceback.format_exc()}"
            return {
                "status": "error",
                "error": error_msg,
                "traceback": traceback.format_exc()
            }

    def _get_context_state(self):
        """Get current GIMP context state (colors, brush, tool settings)."""
        try:
            print("Getting GIMP context state...")

            context_state = {}

            # Get foreground and background colors
            try:
                fg_color = Gimp.context_get_foreground()
                bg_color = Gimp.context_get_background()

                # get_rgba() is linear light; report sRGB hex so values match the colors tools accept.
                for key, color, label in (("foreground_color", fg_color, "Current foreground color"),
                                          ("background_color", bg_color, "Current background color")):
                    srgb = color.get_rgba_with_space(None)
                    rgb = tuple(max(0, min(255, round(v * 255))) for v in (srgb.red, srgb.green, srgb.blue))
                    context_state[key] = {
                        "hex": "#%02x%02x%02x" % rgb,
                        "alpha": round(srgb.alpha, 4),
                        "description": label,
                    }

            except Exception as color_err:
                context_state["colors_error"] = str(color_err)

            # Get brush information
            try:
                brush = Gimp.context_get_brush()
                if brush:
                    context_state["brush"] = {
                        "name": brush.get_name() if hasattr(brush, 'get_name') else str(brush),
                        "description": "Current brush"
                    }
            except Exception as brush_err:
                context_state["brush_error"] = str(brush_err)

            # Get opacity
            try:
                opacity = Gimp.context_get_opacity()
                context_state["opacity"] = {
                    "value": opacity,  # Already in percentage (0-100)
                    "description": "Current opacity percentage (0-100)"
                }
            except Exception as opacity_err:
                context_state["opacity_error"] = str(opacity_err)

            # Get paint mode
            try:
                paint_mode = Gimp.context_get_paint_mode()
                context_state["paint_mode"] = {
                    "value": self._blend_mode_name(paint_mode),
                    "description": "Current paint/blend mode"
                }
            except Exception as mode_err:
                context_state["paint_mode_error"] = str(mode_err)

            # Get feather setting (if available)
            try:
                feather = Gimp.context_get_feather()
                _ok, radius_x, radius_y = Gimp.context_get_feather_radius()
                context_state["feather"] = {
                    "enabled": feather,
                    "radius": [radius_x, radius_y],
                    "description": "Selection feathering state"
                }
            except Exception:
                context_state["feather_note"] = "Feather settings not available in context"

            # Get antialias setting
            try:
                antialias = Gimp.context_get_antialias()
                context_state["antialias"] = {
                    "enabled": antialias,
                    "description": "Antialiasing state for selections"
                }
            except Exception:
                context_state["antialias_note"] = "Antialias setting not available"

            return {
                "status": "success",
                "results": context_state
            }

        except Exception as e:
            error_msg = f"Error getting context state: {str(e)}\n{traceback.format_exc()}"
            return {
                "status": "error",
                "error": error_msg,
                "traceback": traceback.format_exc()
            }

    def _restart_server(self):
        """Gracefully restart the MCP socket server in-place."""
        try:
            print("Restarting MCP server socket...")
            # Close existing socket to force reconnect on next client call
            if self.socket:
                try:
                    self.socket.close()
                except Exception:
                    pass
                self.socket = None

            # Re-bind a fresh socket
            import time
            time.sleep(0.3)
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.settimeout(1.0)
            self.socket.bind((self.host, self.port))
            self.socket.listen(LISTEN_BACKLOG)
            print(f"MCP server restarted on {self.host}:{self.port}")
            return {
                "status": "success",
                "results": {"restarted": True, "host": self.host, "port": self.port}
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"Restart failed: {str(e)}",
                "traceback": traceback.format_exc()
            }

    def _new_canvas(self, params):
        """Create a new blank canvas and open it in a GIMP display window."""
        try:
            width  = int(params.get("width", 1024))
            height = int(params.get("height", 1024))
            name   = str(params.get("name", "Untitled"))
            color_mode = str(params.get("color_mode", "RGB")).upper()
            fill   = str(params.get("fill", "white"))
            resolution = int(params.get("resolution", 72))

            mode_map = {
                "RGB":   Gimp.ImageBaseType.RGB,
                "RGBA":  Gimp.ImageBaseType.RGB,
                "GRAY":  Gimp.ImageBaseType.GRAY,
                "GRAYA": Gimp.ImageBaseType.GRAY,
            }
            layer_type_map = {
                "RGB":   Gimp.ImageType.RGB_IMAGE,
                "RGBA":  Gimp.ImageType.RGBA_IMAGE,
                "GRAY":  Gimp.ImageType.GRAY_IMAGE,
                "GRAYA": Gimp.ImageType.GRAYA_IMAGE,
            }
            base_type  = self._choice(color_mode, mode_map, "color_mode")
            layer_type = self._choice(color_mode, layer_type_map, "color_mode")
            # Check the fill before creating anything, so a bad color leaves no image behind.
            fill_color = None if fill.strip().lower() == "transparent" else self._parse_color(fill, "fill")

            image = Gimp.Image.new(width, height, base_type)
            image.set_resolution(resolution, resolution)

            layer = Gimp.Layer.new(image, name, width, height, layer_type, 100, Gimp.LayerMode.NORMAL)
            image.insert_layer(layer, None, 0)
            image.attach_parasite(Gimp.Parasite.new(
                IMAGE_NAME_PARASITE, Gimp.PARASITE_PERSISTENT, name.encode("utf-8")))

            if fill_color is None:
                layer.add_alpha()
                Gimp.Drawable.edit_fill(layer, Gimp.FillType.TRANSPARENT)
            else:
                Gimp.context_push()  # keep the user's background color
                try:
                    Gimp.context_set_background(fill_color)
                    Gimp.Drawable.edit_fill(layer, Gimp.FillType.BACKGROUND)
                finally:
                    Gimp.context_pop()

            self._remember_display(image, Gimp.Display.new(image))
            Gimp.displays_flush()

            print(f"New canvas created: {width}x{height} {color_mode} fill={fill}")
            return {
                "status": "success",
                "results": {
                    "image_id": image.get_id(),
                    "name": name,
                    "width": width,
                    "height": height,
                    "color_mode": color_mode,
                    "fill": fill,
                    "resolution": resolution,
                    "display_opened": True
                }
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"new_canvas failed: {str(e)}",
                "traceback": traceback.format_exc()
            }


    # =========================================================================
    # SHARED HELPERS
    # =========================================================================

    def _get_image(self, image_index):
        """Return the image at image_index from Gimp.get_images(), raise if none open."""
        images = Gimp.get_images()
        if not images:
            raise RuntimeError("No images are currently open in GIMP")
        if image_index >= len(images):
            raise RuntimeError(f"image_index {image_index} out of range (only {len(images)} images open)")
        return images[image_index]

    def _resolve_layer(self, image, layer_name, layer_index):
        """Resolve a layer by name, index, or fall back to the active layer."""
        if layer_name is not None:
            layers = image.get_layers()
            for layer in layers:
                if layer.get_name() == layer_name:
                    return layer
            raise RuntimeError(f"Layer '{layer_name}' not found")
        if layer_index is not None:
            layers = image.get_layers()
            if layer_index >= len(layers):
                raise RuntimeError(f"layer_index {layer_index} out of range")
            return layers[layer_index]
        layer = (image.get_selected_layers() or image.get_layers() or [None])[0]
        if layer is None:
            layers = image.get_layers()
            if not layers:
                raise RuntimeError("No layers in image")
            return layers[0]
        return layer

    @staticmethod
    def _choice(value, choices, label):
        """Look up a case-insensitive option, raising an error that lists the valid ones."""
        key = str(value).strip().lower()
        for name, result in choices.items():
            if name.lower() == key:
                return result
        raise ValueError(f"{label} must be one of {', '.join(choices)} (got {value!r})")

    @staticmethod
    def _check_range(value, low, high, label):
        """Return value, or raise an error naming label if it is outside low..high."""
        if not low <= value <= high:
            raise ValueError(f"{label} must be between {low:g} and {high:g} (got {value:g})")
        return value

    @staticmethod
    def _check_ok(ok, what):
        """Raise GIMP's last error when a libgimp call returned False instead of doing its work."""
        if not ok:
            raise RuntimeError(f"{what} failed: {Gimp.get_pdb().get_last_error()}")

    @staticmethod
    @contextlib.contextmanager
    def _kept_selection(image):
        """Put back the user's selection after a tool selects a shape to fill or stroke it."""
        saved = None if Gimp.Selection.is_empty(image) else Gimp.Selection.save(image)
        try:
            yield
        finally:
            if saved is None:
                Gimp.Selection.none(image)
            else:
                image.get_selection().combine_masks(saved, Gimp.ChannelOps.REPLACE, 0, 0)
                image.remove_channel(saved)

    @staticmethod
    def _parse_color(value, label):
        """Parse a hex color or one of the 16 basic CSS names into a Gegl.Color.

        Gegl.Color.new() turns other names into a translucent cyan without an
        error and reads rgb() floats as linear light, so everything else is rejected.
        """
        from gi.repository import Gegl
        text = value.strip() if isinstance(value, str) else ""
        digits = text[1:] if text.startswith("#") else None
        if digits is not None and len(digits) in (3, 6, 8) and all(c in "0123456789abcdefABCDEF" for c in digits):
            return Gegl.Color.new(text)
        if text.lower() in BASIC_COLOR_NAMES:
            return Gegl.Color.new(text.lower())
        raise ValueError(f"{label} must be hex like '#8b4513' or a basic color name "
                         f"({', '.join(BASIC_COLOR_NAMES)}); got {value!r}")

    def _channel_ops_from_string(self, op):
        """Map operation string to Gimp.ChannelOps enum value."""
        return self._choice(op, {
            "replace":   Gimp.ChannelOps.REPLACE,
            "add":       Gimp.ChannelOps.ADD,
            "subtract":  Gimp.ChannelOps.SUBTRACT,
            "intersect": Gimp.ChannelOps.INTERSECT,
        }, "operation")

    def _interp_from_string(self, interp):
        """Map interpolation string to Gimp.InterpolationType."""
        return self._choice(interp, {
            "cubic":  Gimp.InterpolationType.CUBIC,
            "linear": Gimp.InterpolationType.LINEAR,
            "none":   Gimp.InterpolationType.NONE,
        }, "interpolation")

    def _export_to_path(self, image, file_path, fmt, quality, flatten, compression=None):
        """Export image to file_path in the given format. Returns file size in bytes."""
        from gi.repository import Gio
        if flatten:
            image = image.duplicate()
            image.flatten()
            should_delete = True
        else:
            should_delete = False
        try:
            gio_file = Gio.File.new_for_path(file_path)
            pdb = Gimp.get_pdb()
            fmt_lower = fmt.lower()
            proc_name_map = {
                "png":  "file-png-export",
                "jpeg": "file-jpeg-export",
                "jpg":  "file-jpeg-export",
                "webp": "file-webp-export",
                "tiff": "file-tiff-export",
            }
            if fmt_lower not in proc_name_map:
                raise ValueError(f"Unsupported export format {fmt!r}; use png, jpeg, webp or tiff")
            proc = pdb.lookup_procedure(proc_name_map[fmt_lower])
            if proc is None:
                raise RuntimeError(f"GIMP has no {proc_name_map[fmt_lower]} procedure")
            else:
                cfg = proc.create_config()
                cfg.set_property("image", image)
                cfg.set_property("file", gio_file)
                try:
                    layers = image.get_layers()
                    drawable = (image.get_selected_layers() or layers or [None])[0]
                    try:
                        cfg.set_property("drawable", drawable)
                    except Exception:
                        pass
                    if fmt_lower in ("jpeg", "jpg"):
                        try:
                            cfg.set_property("quality", quality / 100.0)
                        except Exception:
                            pass
                    if fmt_lower == "webp":
                        try:
                            cfg.set_property("quality", float(quality))
                        except Exception:
                            pass
                except Exception:
                    pass
                if fmt_lower == "png" and compression is not None:
                    cfg.set_property("compression", int(compression))
                result = proc.run(cfg)
                # Without this check a failed export reported the size of an older file at the path.
                if result.index(0) != Gimp.PDBStatusType.SUCCESS:
                    raise RuntimeError(f"Export to {file_path} failed: {pdb.get_last_error()}")
            return os.path.getsize(file_path)
        finally:
            if should_delete:
                try:
                    image.delete()
                except Exception:
                    pass

    def _apply_gegl_filter(self, image, drawable, op_name, props):
        """Apply a GEGL operation destructively to a drawable.

        Raises instead of silently doing nothing: an unknown operation or
        property, an out-of-range value, or a choice value GEGL does not accept
        becomes an error that the calling tool returns to the client.
        """
        try:
            filtr = Gimp.DrawableFilter.new(drawable, op_name, op_name)
        except TypeError:
            filtr = None
        if filtr is None:
            raise RuntimeError(f"GEGL operation {op_name!r} is not available in this GIMP")
        try:
            config = filtr.get_config()
            for key, value in props.items():
                pspec = config.find_property(key)
                if pspec is None:
                    raise RuntimeError(f"{op_name} has no property {key!r}")
                if pspec.value_type in (GObject.TYPE_INT, GObject.TYPE_UINT):
                    value = int(round(value))
                elif pspec.value_type == GObject.TYPE_DOUBLE:
                    value = float(value)
                low, high = getattr(pspec, "minimum", None), getattr(pspec, "maximum", None)
                if low is not None and high is not None and not low <= value <= high:
                    raise ValueError(f"{op_name} {key} must be between {low:g} and {high:g} (got {value:g})")
                config.set_property(key, value)
                # Choice properties keep their old value on an unknown choice instead of raising.
                if pspec.value_type == GObject.TYPE_STRING and config.get_property(key) != value:
                    raise ValueError(f"{op_name} {key} does not accept {value!r}")
        except Exception:
            filtr.delete()
            raise
        drawable.merge_filter(filtr)

    # =========================================================================
    # CATEGORY 1 — File Operations
    # =========================================================================

    def _open_image(self, params):
        """Open an image file, create a display, return metadata."""
        try:
            from gi.repository import Gio
            file_path = params.get("file_path", "")
            gio_file = Gio.File.new_for_path(file_path)
            image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, gio_file)
            if image is None:
                return {"status": "error", "error": f"Could not open file: {file_path}"}
            display = Gimp.Display.new(image)
            self._remember_display(image, display)
            Gimp.displays_flush()
            base_type = image.get_base_type()
            mode_map = {
                Gimp.ImageBaseType.RGB:     "RGB",
                Gimp.ImageBaseType.GRAY:    "Grayscale",
                Gimp.ImageBaseType.INDEXED: "Indexed",
            }
            return {
                "status": "success",
                "results": {
                    "image_id":      image.get_id(),
                    "width":         image.get_width(),
                    "height":        image.get_height(),
                    "color_mode":    mode_map.get(base_type, str(base_type)),
                    "num_layers":    len(image.get_layers()),
                    "display_opened": display is not None,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _save_xcf(self, params):
        """Save image as XCF."""
        try:
            from gi.repository import Gio
            image_index = int(params.get("image_index", 0))
            file_path = params.get("file_path", "")
            image = self._get_image(image_index)
            gio_file = Gio.File.new_for_path(file_path)
            pdb = Gimp.get_pdb()
            proc = pdb.lookup_procedure("gimp-xcf-save")
            cfg = proc.create_config()
            cfg.set_property("image", image)
            cfg.set_property("file", gio_file)
            if proc.run(cfg).index(0) != Gimp.PDBStatusType.SUCCESS:
                raise RuntimeError(f"Saving {file_path} failed: {pdb.get_last_error()}")
            return {"status": "success", "results": {"status": "success", "file_path": file_path}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _export_image(self, params):
        """Export image to raster format."""
        try:
            image_index = int(params.get("image_index", 0))
            file_path   = params.get("file_path", "")
            fmt         = params.get("format", "png")
            quality     = int(params.get("quality", 90))
            flatten     = bool(params.get("flatten", True))
            image = self._get_image(image_index)
            file_size = self._export_to_path(image, file_path, fmt, quality, flatten)
            Gimp.displays_flush()
            return {
                "status": "success",
                "results": {
                    "status": "success",
                    "file_path": file_path,
                    "format": fmt,
                    "file_size_bytes": file_size,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _batch_export(self, params):
        """Export all (or one) open images to output_dir."""
        try:
            output_dir   = params.get("output_dir", "")
            fmt          = params.get("format", "png")
            quality      = int(params.get("quality", 90))
            name_pattern = params.get("name_pattern", "{name}")
            image_index  = params.get("image_index", None)

            images = Gimp.get_images()
            if not images:
                return {"status": "error", "error": "No images open"}

            targets = [(i, img) for i, img in enumerate(images)]
            if image_index is not None:
                targets = [(image_index, images[int(image_index)])]

            os.makedirs(output_dir, exist_ok=True)
            exported = []
            errors = []

            for idx, image in targets:
                try:
                    gio_file = image.get_file()
                    raw_name = gio_file.get_basename().rsplit(".", 1)[0] if gio_file else f"image_{idx}"
                    filename = name_pattern.format(name=raw_name, index=idx) + f".{fmt}"
                    out_path = os.path.join(output_dir, filename)
                    self._export_to_path(image, out_path, fmt, quality, True)
                    exported.append({
                        "file_path": out_path,
                        "name": raw_name,
                        "width": image.get_width(),
                        "height": image.get_height(),
                    })
                except Exception as ex:
                    errors.append({"index": idx, "error": str(ex)})

            return {
                "status": "success",
                "results": {"exported": exported, "count": len(exported), "errors": errors}
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 2 — Image Adjustments
    # =========================================================================

    def _auto_levels(self, params):
        """Auto-stretch levels on a drawable."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                # GIMP 3 has no gimp-levels-stretch; the old fallback applied identity levels and changed nothing.
                self._check_ok(drawable.levels_stretch(), "Auto levels")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _adjust_curves(self, params):
        """Adjust tonal curves."""
        try:
            PRESETS = {
                "s_curve":  [0, 0, 64, 50, 192, 210, 255, 255],
                "lighten":  [0, 0, 128, 180, 255, 255],
                "darken":   [0, 0, 128, 75, 255, 255],
                "contrast": [0, 0, 64, 40, 192, 215, 255, 255],
            }
            CHANNEL_MAP = {
                "value": Gimp.HistogramChannel.VALUE,
                "red":   Gimp.HistogramChannel.RED,
                "green": Gimp.HistogramChannel.GREEN,
                "blue":  Gimp.HistogramChannel.BLUE,
                "alpha": Gimp.HistogramChannel.ALPHA,
            }
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            preset      = params.get("preset", "s_curve")
            custom_pts  = params.get("points", None)
            channel_str = params.get("channel", "value")

            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            channel  = self._choice(channel_str, CHANNEL_MAP, "channel")

            if custom_pts is not None:
                # Flatten [[in,out],...] -> [in,out,in,out,...]
                if custom_pts and isinstance(custom_pts[0], (list, tuple)):
                    flat = []
                    for pt in custom_pts:
                        flat.extend(pt)
                    control_pts = flat
                else:
                    control_pts = list(custom_pts)
            else:
                control_pts = self._choice(preset, PRESETS, "preset")
            if len(control_pts) < 4 or len(control_pts) % 2:
                raise ValueError("points needs at least two [input, output] pairs")
            for p in control_pts:
                self._check_range(float(p), 0, 255, "curve point values")

            image.undo_group_start()
            try:
                pts_normalized = [p / 255.0 for p in control_pts]
                self._check_ok(drawable.curves_spline(channel, pts_normalized), "Curves")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _adjust_brightness_contrast(self, params):
        """Adjust brightness and contrast."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            brightness  = self._check_range(float(params.get("brightness", 0)), -127, 127, "brightness")
            contrast    = self._check_range(float(params.get("contrast", 0)), -127, 127, "contrast")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._check_ok(drawable.brightness_contrast(brightness / 127.0, contrast / 127.0), "Brightness-contrast")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _adjust_hue_saturation(self, params):
        """Adjust hue, saturation, lightness."""
        try:
            HUE_RANGE_MAP = {
                "all":     Gimp.HueRange.ALL,
                "red":     Gimp.HueRange.RED,
                "yellow":  Gimp.HueRange.YELLOW,
                "green":   Gimp.HueRange.GREEN,
                "cyan":    Gimp.HueRange.CYAN,
                "blue":    Gimp.HueRange.BLUE,
                "magenta": Gimp.HueRange.MAGENTA,
            }
            image_index  = int(params.get("image_index", 0))
            layer_name   = params.get("layer_name", None)
            hue          = self._check_range(float(params.get("hue", 0)), -180, 180, "hue")
            saturation   = self._check_range(float(params.get("saturation", 0)), -100, 100, "saturation")
            lightness    = self._check_range(float(params.get("lightness", 0)), -100, 100, "lightness")
            color_range  = params.get("color_range", "all")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            hue_range = self._choice(color_range, HUE_RANGE_MAP, "color_range")
            image.undo_group_start()
            try:
                self._check_ok(drawable.hue_saturation(hue_range, hue, lightness, saturation, 0.0), "Hue-saturation")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _adjust_color_balance(self, params):
        """Adjust color balance for shadows/midtones/highlights."""
        try:
            RANGE_MAP = {
                "shadows":    Gimp.TransferMode.SHADOWS,
                "midtones":   Gimp.TransferMode.MIDTONES,
                "highlights": Gimp.TransferMode.HIGHLIGHTS,
            }
            image_index    = int(params.get("image_index", 0))
            layer_name     = params.get("layer_name", None)
            cyan_red       = self._check_range(float(params.get("cyan_red", 0)), -100, 100, "cyan_red")
            magenta_green  = self._check_range(float(params.get("magenta_green", 0)), -100, 100, "magenta_green")
            yellow_blue    = self._check_range(float(params.get("yellow_blue", 0)), -100, 100, "yellow_blue")
            range_str      = params.get("range", "midtones")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            color_range = self._choice(range_str, RANGE_MAP, "range")
            image.undo_group_start()
            try:
                self._check_ok(drawable.color_balance(color_range, True, cyan_red, magenta_green, yellow_blue),
                               "Color balance")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _sharpen(self, params):
        """Sharpen using unsharp mask."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            amount      = float(params.get("amount", 50.0))
            radius      = float(params.get("radius", 3.0))
            threshold   = int(params.get("threshold", 0))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:unsharp-mask", {
                    "std-dev":   radius,
                    "scale":     amount / 100.0,
                    "threshold": threshold / 255.0,
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _blur(self, params):
        """Gaussian blur."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            radius_x    = float(params.get("radius_x", 5.0))
            radius_y    = float(params.get("radius_y", 5.0))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:gaussian-blur", {
                    "std-dev-x": radius_x,
                    "std-dev-y": radius_y,
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _denoise(self, params):
        """Noise reduction."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            strength    = int(params.get("strength", 50))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:noise-reduction", {
                    "iterations": max(1, strength // 20),
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _desaturate(self, params):
        """Desaturate a layer."""
        try:
            # GIMP 3.2: LUMINOSITY was renamed to LUMINANCE
            MODE_MAP = {
                "luminosity": Gimp.DesaturateMode.LUMINANCE,
                "luminance":  Gimp.DesaturateMode.LUMINANCE,
                "luma":       Gimp.DesaturateMode.LUMA,
                "average":    Gimp.DesaturateMode.AVERAGE,
                "lightness":  Gimp.DesaturateMode.LIGHTNESS,
            }
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            mode_str    = params.get("mode", "luminosity")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            mode = self._choice(mode_str, MODE_MAP, "mode")
            image.undo_group_start()
            try:
                self._check_ok(drawable.desaturate(mode), "Desaturate")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _invert_colors(self, params):
        """Invert all colors in a layer."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._check_ok(drawable.invert(False), "Invert")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 3 — Resize & Transform
    # =========================================================================

    def _scale_image(self, params):
        """Scale image to exact dimensions."""
        try:
            image_index   = int(params.get("image_index", 0))
            width         = int(params.get("width"))
            height        = int(params.get("height"))
            interpolation = params.get("interpolation", "cubic")
            image  = self._get_image(image_index)
            interp = self._interp_from_string(interpolation)
            image.undo_group_start()
            try:
                Gimp.context_push()
                try:
                    Gimp.context_set_interpolation(interp)
                    image.scale(width, height)
                finally:
                    Gimp.context_pop()
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "width": width, "height": height}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _scale_to_fit(self, params):
        """Scale image to fit within a bounding box preserving aspect ratio."""
        try:
            image_index   = int(params.get("image_index", 0))
            max_width     = int(params.get("max_width"))
            max_height    = int(params.get("max_height"))
            interpolation = params.get("interpolation", "cubic")
            image  = self._get_image(image_index)
            interp = self._interp_from_string(interpolation)
            src_w  = image.get_width()
            src_h  = image.get_height()
            aspect = src_w / src_h
            max_aspect = max_width / max_height
            if aspect > max_aspect:
                new_w = max_width
                new_h = max(1, int(max_width / aspect))
            else:
                new_h = max_height
                new_w = max(1, int(max_height * aspect))
            image.undo_group_start()
            try:
                Gimp.context_push()
                try:
                    Gimp.context_set_interpolation(interp)
                    image.scale(new_w, new_h)
                finally:
                    Gimp.context_pop()
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "width": new_w, "height": new_h}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _crop_to_selection(self, params):
        """Crop image to selection bounds."""
        try:
            image_index = int(params.get("image_index", 0))
            autocrop    = bool(params.get("autocrop", False))
            image = self._get_image(image_index)
            image.undo_group_start()
            try:
                if autocrop:
                    self._check_ok(image.autocrop(None), "Autocrop")
                else:
                    _ok, non_empty, x1, y1, x2, y2 = Gimp.Selection.bounds(image)
                    if non_empty:
                        image.crop(x2 - x1, y2 - y1, x1, y1)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _crop_to_rect(self, params):
        """Crop image to explicit rectangle."""
        try:
            image_index = int(params.get("image_index", 0))
            x      = int(params.get("x", 0))
            y      = int(params.get("y", 0))
            width  = int(params.get("width"))
            height = int(params.get("height"))
            image = self._get_image(image_index)
            image.undo_group_start()
            try:
                image.crop(width, height, x, y)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "x": x, "y": y, "width": width, "height": height}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _rotate_image(self, params):
        """Rotate image by angle."""
        try:
            image_index = int(params.get("image_index", 0))
            angle       = float(params.get("angle", 90))
            image = self._get_image(image_index)
            image.undo_group_start()
            try:
                rot_map = {
                    90.0:  Gimp.RotationType.DEGREES90,
                    180.0: Gimp.RotationType.DEGREES180,
                    270.0: Gimp.RotationType.DEGREES270,
                    -90.0: Gimp.RotationType.DEGREES270,
                }
                if angle in rot_map:
                    image.rotate(rot_map[angle])
                else:
                    # gimp-item-transform-rotate-default is gone in GIMP 3: rotate each
                    # layer about the image center, grow the canvas to fit, then flatten.
                    rad = math.radians(angle)
                    center_x, center_y = image.get_width() / 2, image.get_height() / 2
                    for layer in image.get_layers():
                        layer.transform_rotate(rad, False, center_x, center_y)
                    image.resize_to_layers()
                    image.flatten()
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "angle": angle}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _flip_image(self, params):
        """Flip image horizontally or vertically."""
        try:
            image_index = int(params.get("image_index", 0))
            direction   = params.get("direction", "horizontal").lower()
            image = self._get_image(image_index)
            orient = Gimp.OrientationType.HORIZONTAL if direction == "horizontal" else Gimp.OrientationType.VERTICAL
            image.undo_group_start()
            try:
                image.flip(orient)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "direction": direction}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _resize_canvas(self, params):
        """Resize canvas without scaling content."""
        try:
            image_index = int(params.get("image_index", 0))
            new_w  = int(params.get("width"))
            new_h  = int(params.get("height"))
            anchor = params.get("anchor", "center").lower()
            fill   = params.get("fill", "transparent")
            image  = self._get_image(image_index)
            src_w  = image.get_width()
            src_h  = image.get_height()
            # Compute offset based on anchor
            dx = (new_w - src_w) // 2
            dy = (new_h - src_h) // 2
            anchor_offsets = {
                "center":       (dx,         dy),
                "top-left":     (0,          0),
                "top":          (dx,         0),
                "top-right":    (new_w - src_w, 0),
                "left":         (0,          dy),
                "right":        (new_w - src_w, dy),
                "bottom-left":  (0,          new_h - src_h),
                "bottom":       (dx,         new_h - src_h),
                "bottom-right": (new_w - src_w, new_h - src_h),
            }
            off_x, off_y = self._choice(anchor, anchor_offsets, "anchor")
            fill_color = None if str(fill).strip().lower() == "transparent" else self._parse_color(fill, "fill")
            image.undo_group_start()
            try:
                image.resize(new_w, new_h, off_x, off_y)
                fill_layer = None
                covered = off_x <= 0 and off_y <= 0 and off_x + src_w >= new_w and off_y + src_h >= new_h
                if fill_color is not None and not covered:
                    # Paint the new areas on a bottom layer of their own; flattening merged every layer into one.
                    layer_type = {Gimp.ImageBaseType.RGB:  Gimp.ImageType.RGBA_IMAGE,
                                  Gimp.ImageBaseType.GRAY: Gimp.ImageType.GRAYA_IMAGE}.get(
                                      image.get_base_type(), Gimp.ImageType.INDEXEDA_IMAGE)
                    selected = image.get_selected_layers()
                    fill_layer = Gimp.Layer.new(image, "Canvas fill", new_w, new_h, layer_type, 100, Gimp.LayerMode.NORMAL)
                    image.insert_layer(fill_layer, None, len(image.get_layers()))
                    fill_layer.fill(Gimp.FillType.TRANSPARENT)
                    image.set_selected_layers(selected)
                    Gimp.context_push()
                    try:
                        Gimp.context_set_feather(False)
                        Gimp.context_set_foreground(fill_color)
                        with self._kept_selection(image):
                            image.select_rectangle(Gimp.ChannelOps.REPLACE, 0, 0, new_w, new_h)
                            image.select_rectangle(Gimp.ChannelOps.SUBTRACT, off_x, off_y, src_w, src_h)
                            fill_layer.edit_fill(Gimp.FillType.FOREGROUND)
                    finally:
                        Gimp.context_pop()
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "width": new_w, "height": new_h, "offset_x": off_x, "offset_y": off_y,
                                                     "fill_layer": fill_layer.get_name() if fill_layer else None}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 4 — Selections
    # =========================================================================

    def _select_rectangle(self, params):
        """Create a rectangular selection."""
        try:
            image_index = int(params.get("image_index", 0))
            x       = int(params.get("x", 0))
            y       = int(params.get("y", 0))
            width   = int(params.get("width"))
            height  = int(params.get("height"))
            operation = params.get("operation", "replace")
            feather   = float(params.get("feather", 0))
            image = self._get_image(image_index)
            op = self._channel_ops_from_string(operation)
            image.select_rectangle(op, x, y, width, height)
            if feather > 0:
                Gimp.Selection.feather(image, feather)
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _select_ellipse(self, params):
        """Create an elliptical selection."""
        try:
            image_index = int(params.get("image_index", 0))
            x       = int(params.get("x", 0))
            y       = int(params.get("y", 0))
            width   = int(params.get("width"))
            height  = int(params.get("height"))
            operation = params.get("operation", "replace")
            feather   = float(params.get("feather", 0))
            image = self._get_image(image_index)
            op = self._channel_ops_from_string(operation)
            image.select_ellipse(op, x, y, width, height)
            if feather > 0:
                Gimp.Selection.feather(image, feather)
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _select_by_color(self, params):
        """Select by color similarity."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            color_str   = params.get("color", "white")
            threshold   = int(params.get("threshold", 15))
            operation   = params.get("operation", "replace")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            op = self._channel_ops_from_string(operation)
            color = self._parse_color(color_str, "color")
            pdb = Gimp.get_pdb()
            proc = pdb.lookup_procedure("gimp-image-select-color")
            if proc is None:
                raise RuntimeError(
                    "PDB procedure 'gimp-image-select-color' not found"
                )
            Gimp.context_push()
            try:
                Gimp.context_set_antialias(True)
                Gimp.context_set_feather(False)
                Gimp.context_set_sample_threshold_int(threshold)
                Gimp.context_set_sample_merged(False)
                Gimp.context_set_sample_transparent(False)
                cfg = proc.create_config()
                cfg.set_property("image",     image)
                cfg.set_property("drawable",  drawable)
                cfg.set_property("color",     color)
                cfg.set_property("operation", op)
                proc.run(cfg)
            finally:
                Gimp.context_pop()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _select_all(self, params):
        """Select entire canvas."""
        try:
            image = self._get_image(int(params.get("image_index", 0)))
            Gimp.Selection.all(image)
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _select_none(self, params):
        """Remove all selections."""
        try:
            image = self._get_image(int(params.get("image_index", 0)))
            Gimp.Selection.none(image)
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _invert_selection(self, params):
        """Invert selection."""
        try:
            image = self._get_image(int(params.get("image_index", 0)))
            Gimp.Selection.invert(image)
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _modify_selection(self, params):
        """Grow/shrink/feather/border/sharpen selection."""
        try:
            image_index = int(params.get("image_index", 0))
            operation   = params.get("operation", "grow").lower()
            amount      = float(params.get("amount", 0))
            image = self._get_image(image_index)
            OP_MAP = {
                "grow":    Gimp.Selection.grow,
                "shrink":  Gimp.Selection.shrink,
                "feather": Gimp.Selection.feather,
                "border":  Gimp.Selection.border,
                "sharpen": Gimp.Selection.sharpen,
            }
            fn = OP_MAP.get(operation)
            if fn is None:
                return {"status": "error", "error": f"Unknown selection operation: {operation}"}
            if operation == "sharpen":
                fn(image)
            else:
                fn(image, amount)
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 5 — Layer Operations
    # =========================================================================

    def _blend_mode_from_string(self, mode_str):
        """Map blend mode name string to Gimp.LayerMode."""
        return self._choice(mode_str, BLEND_MODES, "blend mode")

    @staticmethod
    def _blend_mode_name(mode):
        """Name for a Gimp.LayerMode that set_layer_properties accepts, else GIMP's own enum name."""
        return next((name for name, value in BLEND_MODES.items() if value == mode), mode.name)

    def _create_layer(self, params):
        """Create and insert a new layer."""
        try:
            image_index = int(params.get("image_index", 0))
            name        = params.get("name", "New Layer")
            opacity     = float(params.get("opacity", 100))
            blend_mode  = params.get("blend_mode", "NORMAL")
            position    = int(params.get("position", -1))
            fill        = params.get("fill", "transparent")
            image = self._get_image(image_index)
            width  = int(params.get("width")  or image.get_width())
            height = int(params.get("height") or image.get_height())
            mode   = self._blend_mode_from_string(blend_mode)
            fill_color = None if str(fill).strip().lower() == "transparent" else self._parse_color(fill, "fill")
            # Determine layer type
            base_type  = image.get_base_type()
            layer_type = Gimp.ImageType.RGBA_IMAGE if base_type == Gimp.ImageBaseType.RGB else Gimp.ImageType.GRAYA_IMAGE
            image.undo_group_start()
            try:
                layer = Gimp.Layer.new(image, name, width, height, layer_type, opacity, mode)
                image.insert_layer(layer, None, position)
                Gimp.context_push()
                try:
                    if fill_color is None:
                        layer.add_alpha()
                        Gimp.Drawable.edit_fill(layer, Gimp.FillType.TRANSPARENT)
                    else:
                        Gimp.context_set_background(fill_color)
                        Gimp.Drawable.edit_fill(layer, Gimp.FillType.BACKGROUND)
                finally:
                    Gimp.context_pop()
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {
                "status": "success",
                "results": {
                    "layer_name": layer.get_name(),
                    "layer_id":   layer.get_id(),
                    "width":      width,
                    "height":     height,
                    "position":   position,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _duplicate_layer(self, params):
        """Duplicate a layer."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            image    = self._get_image(image_index)
            layer    = self._resolve_layer(image, layer_name, None)
            layers   = image.get_layers()
            position = layers.index(layer) if layer in layers else 0
            image.undo_group_start()
            try:
                new_layer = layer.copy()
                image.insert_layer(new_layer, None, position)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"layer_name": new_layer.get_name(), "layer_id": new_layer.get_id()}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _delete_layer(self, params):
        """Delete a layer."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            layer_index = params.get("layer_index", None)
            if layer_index is not None:
                layer_index = int(layer_index)
            image = self._get_image(image_index)
            layer = self._resolve_layer(image, layer_name, layer_index)
            image.undo_group_start()
            try:
                image.remove_layer(layer)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _rename_layer(self, params):
        """Rename a layer."""
        try:
            image_index = int(params.get("image_index", 0))
            old_name    = params.get("old_name", None)
            layer_index = params.get("layer_index", None)
            new_name    = params.get("new_name", "")
            if layer_index is not None:
                layer_index = int(layer_index)
            image = self._get_image(image_index)
            layer = self._resolve_layer(image, old_name, layer_index)
            prev_name = layer.get_name()
            layer.set_name(new_name)
            Gimp.displays_flush()
            return {"status": "success", "results": {"old_name": prev_name, "new_name": new_name}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _set_layer_properties(self, params):
        """Set layer opacity, blend mode, and/or visibility."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            layer_index = params.get("layer_index", None)
            opacity     = params.get("opacity", None)
            blend_mode  = params.get("blend_mode", None)
            visible     = params.get("visible", None)
            if layer_index is not None:
                layer_index = int(layer_index)
            image = self._get_image(image_index)
            layer = self._resolve_layer(image, layer_name, layer_index)
            mode  = self._blend_mode_from_string(blend_mode) if blend_mode is not None else None
            image.undo_group_start()
            try:
                if opacity is not None:
                    layer.set_opacity(float(opacity))
                if mode is not None:
                    layer.set_mode(mode)
                if visible is not None:
                    layer.set_visible(bool(visible))
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _reorder_layer(self, params):
        """Move a layer to a new stack position."""
        try:
            image_index  = int(params.get("image_index", 0))
            layer_name   = params.get("layer_name", None)
            layer_index  = params.get("layer_index", None)
            new_position = int(params.get("new_position", 0))
            if layer_index is not None:
                layer_index = int(layer_index)
            image = self._get_image(image_index)
            layer = self._resolve_layer(image, layer_name, layer_index)
            image.undo_group_start()
            try:
                image.reorder_item(layer, None, new_position)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _flatten_image(self, params):
        """Flatten all layers."""
        try:
            image = self._get_image(int(params.get("image_index", 0)))
            image.undo_group_start()
            try:
                image.flatten()
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _merge_visible_layers(self, params):
        """Merge visible layers."""
        try:
            image = self._get_image(int(params.get("image_index", 0)))
            image.undo_group_start()
            try:
                merged = image.merge_visible_layers(Gimp.MergeType.CLIP_TO_IMAGE)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"layer_name": merged.get_name(), "layer_id": merged.get_id()}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _list_layers(self, params):
        """List all layers with properties."""
        try:
            image  = self._get_image(int(params.get("image_index", 0)))
            layers = image.get_layers()
            active_ids = {lyr.get_id() for lyr in image.get_selected_layers()}
            layer_list = []
            for i, layer in enumerate(layers):
                try:
                    layer_list.append({
                        "index":      i,
                        "name":       layer.get_name(),
                        "id":         layer.get_id(),
                        "visible":    layer.get_visible(),
                        "opacity":    layer.get_opacity(),
                        "blend_mode": self._blend_mode_name(layer.get_mode()),
                        "width":      layer.get_width(),
                        "height":     layer.get_height(),
                        "has_alpha":  layer.has_alpha(),
                        "active":     layer.get_id() in active_ids,
                        "offsets":    list(layer.get_offsets())[1:],  # drop the leading success flag
                    })
                except Exception as ex:
                    layer_list.append({"index": i, "error": str(ex)})
            return {"status": "success", "results": {"layers": layer_list, "count": len(layer_list)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 6 — Color & Paint
    # =========================================================================

    def _fill_layer(self, params):
        """Fill entire layer with color."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            color_str   = params.get("color", "white")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            fill_color = self._parse_color(color_str, "color")
            image.undo_group_start()
            Gimp.context_push()
            try:
                with self._kept_selection(image):
                    Gimp.Selection.all(image)
                    Gimp.context_set_foreground(fill_color)
                    Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _fill_selection(self, params):
        """Fill the current selection with a color, GIMP's foreground/background, a pattern, or transparency."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            fill_type   = params.get("fill_type")
            color_str   = params.get("color")
            if fill_type is None and color_str is None:
                raise ValueError("Give a color, or a fill_type of foreground, background, pattern or transparent")
            fill_type = self._choice(fill_type or "color", {
                "color": "color", "foreground": "foreground", "background": "background",
                "pattern": "pattern", "transparent": "transparent"}, "fill_type")
            fill_color = self._parse_color(color_str, "color") if fill_type == "color" else None
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            Gimp.context_push()
            try:
                if fill_type == "transparent":
                    # Ensure layer has alpha channel before deleting pixels
                    if not drawable.has_alpha():
                        drawable.add_alpha()
                    Gimp.Drawable.edit_clear(drawable)
                elif fill_type == "background":
                    Gimp.Drawable.edit_fill(drawable, Gimp.FillType.BACKGROUND)
                elif fill_type == "pattern":
                    Gimp.Drawable.edit_fill(drawable, Gimp.FillType.PATTERN)
                else:
                    # An explicit color, or GIMP's current foreground for fill_type="foreground"
                    if fill_color is not None:
                        Gimp.context_set_foreground(fill_color)
                    Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _set_colors(self, params):
        """Set foreground and/or background color."""
        try:
            fg_str = params.get("foreground", None)
            bg_str = params.get("background", None)
            fg = self._parse_color(fg_str, "foreground") if fg_str is not None else None
            bg = self._parse_color(bg_str, "background") if bg_str is not None else None
            if fg is not None:
                Gimp.context_set_foreground(fg)
            if bg is not None:
                Gimp.context_set_background(bg)
            return {"status": "success", "results": {"foreground": fg_str, "background": bg_str}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _draw_line(self, params):
        """Draw a straight line."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            x1 = float(params.get("x1", 0))
            y1 = float(params.get("y1", 0))
            x2 = float(params.get("x2", 0))
            y2 = float(params.get("y2", 0))
            color_str  = params.get("color", None)
            line_width = float(params.get("width", 2.0))
            tool       = params.get("tool", "pencil").lower()
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            self._choice(tool, {"pencil": None, "paintbrush": None}, "tool")
            stroke_color = self._parse_color(color_str, "color") if color_str else None
            image.undo_group_start()
            Gimp.context_push()
            try:
                if stroke_color is not None:
                    Gimp.context_set_foreground(stroke_color)
                Gimp.context_set_brush_size(line_width)
                Gimp.context_set_opacity(100.0)
                coords = [x1, y1, x2, y2]
                if tool == "paintbrush":
                    Gimp.paintbrush_default(drawable, coords)
                else:
                    Gimp.pencil(drawable, coords)
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _draw_rectangle(self, params):
        """Draw a rectangle outline."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            x          = int(params.get("x", 0))
            y          = int(params.get("y", 0))
            width      = int(params.get("width"))
            height     = int(params.get("height"))
            color_str  = params.get("color", None)
            line_width = float(params.get("line_width", 2.0))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            stroke_color = self._parse_color(color_str, "color") if color_str else None
            image.undo_group_start()
            Gimp.context_push()
            try:
                if stroke_color is not None:
                    Gimp.context_set_foreground(stroke_color)
                Gimp.context_set_stroke_method(Gimp.StrokeMethod.LINE)
                Gimp.context_set_line_width(line_width)
                Gimp.context_set_opacity(100.0)
                with self._kept_selection(image):
                    image.select_rectangle(Gimp.ChannelOps.REPLACE, x, y, width, height)
                    self._check_ok(drawable.edit_stroke_selection(), "Stroking the rectangle")
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _draw_ellipse(self, params):
        """Draw an ellipse outline."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            x          = int(params.get("x", 0))
            y          = int(params.get("y", 0))
            width      = int(params.get("width"))
            height     = int(params.get("height"))
            color_str  = params.get("color", None)
            line_width = float(params.get("line_width", 2.0))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            stroke_color = self._parse_color(color_str, "color") if color_str else None
            image.undo_group_start()
            Gimp.context_push()
            try:
                if stroke_color is not None:
                    Gimp.context_set_foreground(stroke_color)
                Gimp.context_set_stroke_method(Gimp.StrokeMethod.LINE)
                Gimp.context_set_line_width(line_width)
                Gimp.context_set_opacity(100.0)
                with self._kept_selection(image):
                    image.select_ellipse(Gimp.ChannelOps.REPLACE, x, y, width, height)
                    self._check_ok(drawable.edit_stroke_selection(), "Stroking the ellipse")
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _fill_rectangle(self, params):
        """Fill a rectangular region with color."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            x       = int(params.get("x", 0))
            y       = int(params.get("y", 0))
            width   = int(params.get("width"))
            height  = int(params.get("height"))
            color_str = params.get("color", "white")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            fill_color = self._parse_color(color_str, "color")
            image.undo_group_start()
            Gimp.context_push()
            try:
                with self._kept_selection(image):
                    image.select_rectangle(Gimp.ChannelOps.REPLACE, x, y, width, height)
                    Gimp.context_set_foreground(fill_color)
                    Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _fill_ellipse(self, params):
        """Fill an elliptical region with color."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            x       = int(params.get("x", 0))
            y       = int(params.get("y", 0))
            width   = int(params.get("width"))
            height  = int(params.get("height"))
            color_str = params.get("color", "white")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            fill_color = self._parse_color(color_str, "color")
            image.undo_group_start()
            Gimp.context_push()
            try:
                with self._kept_selection(image):
                    image.select_ellipse(Gimp.ChannelOps.REPLACE, x, y, width, height)
                    Gimp.context_set_foreground(fill_color)
                    Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _gradient_fill(self, params):
        """Fill a layer, or the selection on it, with GIMP's own gradient fill.

        The previous hand-built GEGL graph set properties GEGL does not have
        (x0/y0/x1/y1 instead of start-x/start-y/end-x/end-y) inside a swallowed
        try block, so it reported success without drawing the gradient.
        """
        try:
            image_index   = int(params.get("image_index", 0))
            layer_name    = params.get("layer_name", None)
            gradient_type = self._choice(params.get("gradient_type", "linear"),
                                         {"linear": Gimp.GradientType.LINEAR, "radial": Gimp.GradientType.RADIAL},
                                         "gradient_type")
            start_color   = self._parse_color(params.get("color1", "black"), "color1")
            end_color     = self._parse_color(params.get("color2", "white"), "color2")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)

            def coord(names, default):
                # An explicit 0 is a real coordinate, so test for None rather than truthiness.
                for name in names:
                    if params.get(name) is not None:
                        return float(params[name])
                return float(default)

            x1 = coord(("x1", "start_x"), 0)
            y1 = coord(("y1", "start_y"), 0)
            x2 = coord(("x2", "end_x"), image.get_width())
            y2 = coord(("y2", "end_y"), image.get_height())
            if (x1, y1) == (x2, y2):
                raise ValueError("The gradient's start and end points must differ")

            call = self._call_checked
            image.undo_group_start()
            Gimp.context_push()
            try:
                call(Gimp.context_set_foreground, start_color)
                call(Gimp.context_set_background, end_color)
                call(Gimp.context_set_gradient_fg_bg_rgb)
                call(Gimp.context_set_gradient_repeat_mode, Gimp.RepeatMode.NONE)
                call(Gimp.context_set_gradient_reverse, False)
                call(Gimp.context_set_opacity, 100.0)
                call(Gimp.context_set_paint_mode, Gimp.LayerMode.NORMAL)
                # edit_gradient_fill takes layer coordinates; the tool's x/y are image coordinates.
                _ok, off_x, off_y = drawable.get_offsets()
                call(drawable.edit_gradient_fill, gradient_type, 0.0, False, 1, 0.0, True,
                     x1 - off_x, y1 - off_y, x2 - off_x, y2 - off_y)
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # ── Painting: paint_stroke / list_brushes / sample_color ─────────────────

    @staticmethod
    def _paint_number(value, lo, hi, label):
        """Return value as a float within [lo, hi], or raise ValueError naming label."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a number (got {value!r})") from None
        if not lo <= number <= hi:
            raise ValueError(f"{label} must be between {lo:g} and {hi:g} (got {value!r})")
        return number

    def _validate_paint_stroke(self, raw, index, warnings):
        """Check one paint_stroke entry and resolve it into ready-to-use values.

        Every stroke is checked before anything is painted, so a bad stroke
        never leaves half a batch on the canvas.
        """
        where = f"stroke {index}"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: must be an object")
        unknown = sorted(set(raw) - PAINT_STROKE_KEYS)
        if unknown:
            raise ValueError(f"{where}: unknown keys {unknown}")

        points = raw.get("points")
        if not isinstance(points, list) or not points:
            raise ValueError(f"{where}: points must be a non-empty list of [x, y] pairs")
        if len(points) > PAINT_MAX_POINTS:
            raise ValueError(f"{where}: at most {PAINT_MAX_POINTS} points per stroke (got {len(points)})")
        coords = []
        for point in points:
            if not (isinstance(point, (list, tuple)) and len(point) == 2):
                raise ValueError(f"{where}: each point must be [x, y] (got {point!r})")
            coords.append((self._paint_number(point[0], -1e6, 1e6, f"{where}: x"),
                           self._paint_number(point[1], -1e6, 1e6, f"{where}: y")))

        tool = str(raw.get("tool", "paintbrush")).lower()
        if tool not in PAINT_METHODS:
            raise ValueError(f"{where}: tool must be one of {', '.join(PAINT_METHODS)} (got {tool!r})")

        brush_name = str(raw.get("brush") or PAINT_DEFAULT_BRUSH)
        brush = Gimp.Brush.get_by_name(brush_name)
        if brush is None:
            raise ValueError(f"{where}: brush {brush_name!r} not found; call list_brushes for names")

        color = None
        if raw.get("color") is not None:
            color = self._parse_color(raw["color"], f"{where}: color")
            if tool in ("eraser", "smudge"):
                warnings.append(f"{where}: color is ignored by the {tool}")

        try:
            mode = self._blend_mode_from_string(raw.get("mode", "normal"))
        except ValueError as e:
            raise ValueError(f"{where}: {e}") from None

        pressure = raw.get("pressure", "taper")
        if isinstance(pressure, list):
            if len(pressure) < 2:
                raise ValueError(f"{where}: a pressure list needs at least 2 values")
            if tool not in ("paintbrush", "pencil", "airbrush"):
                raise ValueError(f"{where}: a pressure list works with paintbrush, pencil and airbrush; "
                                 f"use 'taper' or 'none' for the {tool}")
            pressure = [self._paint_number(v, 0, 1, f"{where}: pressure value") for v in pressure]
        else:
            pressure = str(pressure).lower()
            if pressure not in ("taper", "none"):
                raise ValueError(f"{where}: pressure must be 'taper', 'none' or a list of 0-1 values "
                                 f"(got {raw.get('pressure')!r})")

        taper_affects = str(raw.get("taper_affects", "size")).lower()
        if taper_affects not in ("size", "opacity"):
            raise ValueError(f"{where}: taper_affects must be 'size' or 'opacity' (got {taper_affects!r})")

        smooth = raw.get("smooth", True)
        if not isinstance(smooth, bool):
            raise ValueError(f"{where}: smooth must be true or false (got {smooth!r})")

        if "strength" in raw and pressure == "taper" and tool in ("airbrush", "smudge"):
            warnings.append(f"{where}: strength is ignored with pressure 'taper'; use pressure 'none' to control it")

        def optional(key, lo, hi):
            return None if raw.get(key) is None else self._paint_number(raw[key], lo, hi, f"{where}: {key}")

        return {
            "points":        coords,
            "tool":          tool,
            "brush":         brush,
            "color":         color,
            "size":          self._paint_number(raw.get("size", 20), 1, 10000, f"{where}: size"),
            "opacity":       self._paint_number(raw.get("opacity", 100), 0, 100, f"{where}: opacity"),
            "hardness":      optional("hardness", 0, 1),
            "angle":         self._paint_number(raw.get("angle", 0), -180, 180, f"{where}: angle"),
            "aspect_ratio":  self._paint_number(raw.get("aspect_ratio", 0), -20, 20, f"{where}: aspect_ratio"),
            "spacing":       optional("spacing", 0.01, 50),
            "strength":      self._paint_number(raw.get("strength", 50), 0, 100, f"{where}: strength"),
            "pressure":      pressure,
            "taper_affects": taper_affects,
            "smooth":        smooth,
            "mode":          mode,
        }

    @staticmethod
    def _call_checked(fn, *args):
        """Call a libgimp function and raise if it reports failure.

        Wrapped PDB calls return False instead of raising (GIMP shows the error
        in a dialog), so an unchecked failure never reaches the MCP client.
        """
        if fn(*args) is False:
            name = getattr(fn, "__name__", repr(fn))
            raise RuntimeError(f"{name} failed: {Gimp.get_pdb().get_last_error()}")

    def _apply_paint_context(self, spec):
        """Set brush, color and paint options for one stroke on the pushed context.

        Every option is set explicitly so results don't depend on whatever the
        user last picked in GIMP's tool options.
        """
        call = self._call_checked
        if spec["color"] is not None:
            call(Gimp.context_set_foreground, spec["color"])
        call(Gimp.context_set_brush, spec["brush"])
        call(Gimp.context_set_brush_size, spec["size"])
        call(Gimp.context_set_brush_angle, spec["angle"])
        call(Gimp.context_set_brush_aspect_ratio, spec["aspect_ratio"])
        if spec["hardness"] is None:
            call(Gimp.context_set_brush_default_hardness)
        else:
            call(Gimp.context_set_brush_hardness, spec["hardness"])
        if spec["spacing"] is None:
            call(Gimp.context_set_brush_default_spacing)
        else:
            call(Gimp.context_set_brush_spacing, spec["spacing"])
        call(Gimp.context_set_opacity, spec["opacity"])
        call(Gimp.context_set_paint_mode, spec["mode"])
        # GIMP 3 has no "Dynamics Off" preset; dynamics are switched off instead.
        call(Gimp.context_enable_dynamics, False)
        call(Gimp.context_set_emulate_brush_dynamics, False)

    @staticmethod
    def _catmull_rom_controls(points):
        """Bezier control points (in-handle, anchor, out-handle) for a smooth curve through points."""
        controls = []
        last = len(points) - 1
        for i, (x, y) in enumerate(points):
            px, py = points[max(i - 1, 0)]
            nx, ny = points[min(i + 1, last)]
            dx, dy = (nx - px) / 6.0, (ny - py) / 6.0
            controls += [x - dx, y - dy, x, y, x + dx, y + dy]
        return controls

    def _build_paint_path(self, image, spec):
        """Insert a temporary path for the stroke; return (path, stroke_id). Caller removes it."""
        points = spec["points"]
        path = Gimp.Path.new(image, "paint_stroke")
        image.insert_path(path, None, 0)
        if spec["smooth"] and len(points) > 2:
            stroke_id = path.stroke_new_from_points(
                Gimp.PathStrokeType.BEZIER, self._catmull_rom_controls(points), False)
        else:
            stroke_id = path.bezier_stroke_new_moveto(*points[0])
            for x, y in points[1:]:
                path.bezier_stroke_lineto(stroke_id, x, y)
        return path, stroke_id

    def _stroke_coords(self, image, spec):
        """Flat [x0, y0, x1, y1, ...] along the stroke, densified along the curve when smoothing."""
        points = spec["points"]
        if not (spec["smooth"] and len(points) > 2):
            return [c for point in points for c in point]
        path, stroke_id = self._build_paint_path(image, spec)
        try:
            coords, _closed = path.stroke_interpolate(stroke_id, 1.0)
            return list(coords)
        finally:
            image.remove_path(path)

    def _paint_points(self, drawable, tool, coords, strength):
        """Paint along coords (image coordinates) with GIMP's plain paint procedures."""
        call = self._call_checked
        if tool == "paintbrush":
            call(Gimp.paintbrush, drawable, 0, coords, Gimp.PaintApplicationMode.CONSTANT, 0)
        elif tool == "pencil":
            call(Gimp.pencil, drawable, coords)
        elif tool == "airbrush":
            call(Gimp.airbrush, drawable, strength, coords)
        elif tool == "eraser":
            call(Gimp.eraser, drawable, coords, Gimp.BrushApplicationMode.SOFT, Gimp.PaintApplicationMode.CONSTANT)
        else:
            call(Gimp.smudge, drawable, strength, coords)

    def _paint_tapered(self, image, drawable, spec):
        """Stroke a path with emulated brush dynamics, which tapers both ends.

        The plain paint procedures take no pressure, so pressure-based dynamics
        do nothing there; stroking a path with emulation on is what tapers.
        """
        call = self._call_checked
        call(Gimp.context_enable_dynamics, True)
        call(Gimp.context_set_dynamics_name,
             "Pressure Opacity" if spec["taper_affects"] == "opacity" else "Pressure Size")
        call(Gimp.context_set_emulate_brush_dynamics, True)
        call(Gimp.context_set_stroke_method, Gimp.StrokeMethod.PAINT_METHOD)
        call(Gimp.context_set_paint_method, PAINT_METHODS[spec["tool"]])
        path, _stroke_id = self._build_paint_path(image, spec)
        try:
            call(drawable.edit_stroke_item, path)
        finally:
            image.remove_path(path)

    def _paint_with_pressure(self, image, drawable, spec, coords):
        """Paint segment by segment, scaling brush size by the pressure curve.

        Segments overlap at their joins, which shows as dark beads when opacity
        is below 100 or a blend mode is used. In that case paint on a buffer
        layer at full strength and merge it down with the stroke's opacity and
        mode. Returns the layer to keep painting on (merging replaces it).
        """
        points = list(zip(coords[0::2], coords[1::2]))
        dist = [0.0]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            dist.append(dist[-1] + math.hypot(x1 - x0, y1 - y0))
        total = dist[-1] or 1.0
        curve = spec["pressure"]
        steps = len(curve) - 1

        def pressure_at(t):
            pos = t * steps
            i = min(int(pos), steps - 1)
            return curve[i] + (curve[i + 1] - curve[i]) * (pos - i)

        buffered = spec["opacity"] < 100 or spec["mode"] != Gimp.LayerMode.NORMAL
        target = drawable
        if buffered:
            layer_type = (Gimp.ImageType.RGBA_IMAGE if image.get_base_type() == Gimp.ImageBaseType.RGB
                          else Gimp.ImageType.GRAYA_IMAGE)
            target = Gimp.Layer.new(image, "paint_stroke buffer", drawable.get_width(), drawable.get_height(),
                                    layer_type, 100, Gimp.LayerMode.NORMAL)
            image.insert_layer(target, drawable.get_parent(), image.get_item_position(drawable))
            _ok, off_x, off_y = drawable.get_offsets()
            target.set_offsets(off_x, off_y)
            Gimp.context_set_opacity(100)
            Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)

        for i in range(len(points) - 1):
            t = (dist[i] + dist[i + 1]) / 2 / total
            Gimp.context_set_brush_size(max(1.0, spec["size"] * pressure_at(t)))
            self._paint_points(target, spec["tool"], [*points[i], *points[i + 1]], spec["strength"])

        if not buffered:
            return drawable
        target.set_opacity(spec["opacity"])
        target.set_mode(spec["mode"])
        return image.merge_down(target, Gimp.MergeType.CLIP_TO_BOTTOM_LAYER)

    def _paint_one_stroke(self, image, drawable, spec):
        """Paint one validated stroke and return the layer to keep painting on."""
        self._apply_paint_context(spec)
        pressure = spec["pressure"]
        if len(spec["points"]) == 1 or pressure == "none":
            self._paint_points(drawable, spec["tool"], self._stroke_coords(image, spec), spec["strength"])
            return drawable
        if pressure == "taper":
            self._paint_tapered(image, drawable, spec)
            return drawable
        return self._paint_with_pressure(image, drawable, spec, self._stroke_coords(image, spec))

    @staticmethod
    def _paint_bbox(image, specs):
        """Region covering all strokes, clamped to the image, in get_state_snapshot's region format."""
        x0 = y0 = float("inf")
        x1 = y1 = float("-inf")
        for spec in specs:
            pad = spec["size"] / 2 + 2
            for x, y in spec["points"]:
                x0, y0 = min(x0, x - pad), min(y0, y - pad)
                x1, y1 = max(x1, x + pad), max(y1, y + pad)
        left, top = max(0, int(x0)), max(0, int(y0))
        right, bottom = min(image.get_width(), math.ceil(x1)), min(image.get_height(), math.ceil(y1))
        return {"x": left, "y": top, "width": max(0, right - left), "height": max(0, bottom - top)}

    def _paint_stroke(self, params):
        """Paint a batch of brush strokes on one layer as a single undo step."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            strokes     = params.get("strokes")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            if drawable.is_group():
                raise ValueError(f"Layer '{drawable.get_name()}' is a layer group; paint on a normal layer")
            if not isinstance(strokes, list) or not strokes:
                raise ValueError("strokes must be a non-empty list")
            if len(strokes) > PAINT_MAX_STROKES:
                raise ValueError(f"at most {PAINT_MAX_STROKES} strokes per call (got {len(strokes)})")

            warnings = []
            specs = [self._validate_paint_stroke(raw, i, warnings) for i, raw in enumerate(strokes)]
            if not Gimp.Selection.is_empty(image):
                warnings.append("A selection is active, so strokes are clipped to it; call select_none to paint everywhere")

            image.undo_group_start()
            Gimp.context_push()
            try:
                for i, spec in enumerate(specs):
                    try:
                        drawable = self._paint_one_stroke(image, drawable, spec)
                    except Exception as e:
                        raise RuntimeError(f"stroke {i} failed after {i} strokes were painted "
                                           f"(the batch is one undo step in GIMP): {e}") from e
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {
                "status": "success",
                "results": {
                    "strokes_painted": len(specs),
                    "layer":           drawable.get_name(),
                    "bbox":            self._paint_bbox(image, specs),
                    "warnings":        warnings,
                },
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _list_brushes(self, params):
        """List brush names, optionally filtered."""
        try:
            filt = params.get("filter", None) or ""
            raw = Gimp.brushes_get_list(filt)
            brushes = list(raw[1]) if isinstance(raw, tuple) else list(raw)
            names = [brush.get_name() for brush in brushes]
            return {"status": "success", "results": {"brushes": names, "count": len(names)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    @staticmethod
    def _pick_color(image, drawable, x, y, sample_merged, radius=0.0):
        """Color at image coordinates as ((r, g, b) sRGB 0-255, alpha 0-1)."""
        if not (0 <= x < image.get_width() and 0 <= y < image.get_height()):
            raise ValueError(f"({x:g}, {y:g}) is outside the {image.get_width()}x{image.get_height()} image")
        ok, color = image.pick_color([drawable], x, y, sample_merged, radius > 0, radius)
        if not ok or color is None:
            raise RuntimeError(f"Could not sample a color at ({x:g}, {y:g})")
        # get_rgba() is linear light; hex must be sRGB to round-trip with paint colors.
        rgba = color.get_rgba_with_space(None)
        rgb = tuple(max(0, min(255, round(v * 255))) for v in (rgba.red, rgba.green, rgba.blue))
        return rgb, rgba.alpha

    def _sample_color(self, params):
        """Pick the color at image coordinates, returned as sRGB hex."""
        try:
            image_index   = int(params.get("image_index", 0))
            layer_name    = params.get("layer_name", None)
            x             = float(params.get("x", 0))
            y             = float(params.get("y", 0))
            radius        = float(params.get("radius", 0))
            sample_merged = bool(params.get("sample_merged", True))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            (r, g, b), alpha = self._pick_color(image, drawable, x, y, sample_merged, radius)
            return {
                "status": "success",
                "results": {"color_hex": f"#{r:02x}{g:02x}{b:02x}", "alpha": round(alpha, 3)},
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 7 — Text
    # =========================================================================

    def _resolve_font(self, name):
        """Resolve a font name to a Gimp.Font."""
        if not (hasattr(Gimp, "Font") and hasattr(Gimp.Font, "get_by_name")):
            return None

        font_obj = Gimp.Font.get_by_name(name)
        if font_obj is not None:
            return font_obj

        # GIMP 3.2 dropped GIMP 2.x aliases; e.g. "Sans" no longer resolves,
        # the 3.2 equivalent is "Sans-serif".
        for alias in (name + "-serif", name + " Regular",
                      name.replace(" ", "-"),
                      "Sans-serif", "Serif", "Monospace"):
            font_obj = Gimp.Font.get_by_name(alias)
            if font_obj is not None:
                return font_obj

        raw = Gimp.fonts_get_list("")
        flist = list(raw[1]) if isinstance(raw, tuple) and len(raw) > 1 else list(raw)
        return flist[0] if flist else None

    def _create_text_layer_native(self, image, text, font_obj, size, x, y, color):
        """Create a text layer via Gimp.TextLayer.new."""
        if not hasattr(Gimp, "TextLayer"):
            return None

        try:
            tl = Gimp.TextLayer.new(image, text, font_obj, float(size), Gimp.Unit.pixel())
        except Exception:
            return None
        if tl is None:
            return None

        try:
            image.insert_layer(tl, None, 0)
        except Exception:
            return None

        # Layer is now in the image; swallow offset/color failures so the
        # caller does not fall through to the PDB fallback and insert a
        # second layer.
        try:
            tl.set_offsets(x, y)
        except Exception:
            pass
        try:
            pdb   = Gimp.get_pdb()
            cproc = pdb.lookup_procedure("gimp-text-layer-set-color")
            if cproc:
                ccfg = cproc.create_config()
                ccfg.set_property("layer", tl)
                ccfg.set_property("color", color)
                cproc.run(ccfg)
        except Exception:
            pass
        return tl

    def _create_text_layer_pdb(self, image, text, font_obj, size, x, y):
        """Create a text layer via the gimp-text-font PDB procedure."""
        proc = Gimp.get_pdb().lookup_procedure("gimp-text-font")
        if proc is None:
            return
        cfg = proc.create_config()
        cfg.set_property("image",     image)
        cfg.set_property("drawable",  None)
        cfg.set_property("x",         float(x))
        cfg.set_property("y",         float(y))
        cfg.set_property("text",      text)
        cfg.set_property("border",    0)
        cfg.set_property("antialias", True)
        cfg.set_property("size",      float(size))
        cfg.set_property("font",      font_obj)
        proc.run(cfg)

    def _find_new_layer(self, image, before_ids):
        """Return the first layer whose id is not in before_ids."""
        for lyr in image.get_layers():
            if lyr.get_id() not in before_ids:
                return lyr
        return None

    def _add_text(self, params):
        """Add a text layer."""
        try:
            image_index = int(params.get("image_index", 0))
            text_str    = params.get("text", "")
            x           = int(params.get("x", 0))
            y           = int(params.get("y", 0))
            font        = params.get("font", "Sans")
            size        = int(params.get("size", 24))
            color_str   = params.get("color", "black")

            image      = self._get_image(image_index)
            text_color = self._parse_color(color_str, "color")
            before_ids = {lyr.get_id() for lyr in image.get_layers()}
            text_layer = None

            image.undo_group_start()
            Gimp.context_push()
            try:
                Gimp.context_set_foreground(text_color)
                font_obj = self._resolve_font(font)
                if font_obj is not None:
                    text_layer = self._create_text_layer_native(
                        image, text_str, font_obj, size, x, y, text_color)
                    if text_layer is None:
                        self._create_text_layer_pdb(
                            image, text_str, font_obj, size, x, y)
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()

            if text_layer is None:
                text_layer = self._find_new_layer(image, before_ids)
            if text_layer is None:
                # Issue #15: return an explicit error instead of a placeholder
                # success so clients never chain ops on a fake handle.
                return {
                    "status": "error",
                    "error":  "add_text: no text layer was created (no PDB procedure succeeded)",
                }

            return {
                "status": "success",
                "results": {
                    "layer_name":  text_layer.get_name(),
                    "layer_id":    text_layer.get_id(),
                    "text_width":  text_layer.get_width(),
                    "text_height": text_layer.get_height(),
                    "position":    [x, y],
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _edit_text(self, params):
        """Edit an existing text layer."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", "")
            new_text    = params.get("text", None)
            new_font    = params.get("font", None)
            new_size    = params.get("size", None)
            new_color   = params.get("color", None)
            image    = self._get_image(image_index)
            layer    = self._resolve_layer(image, layer_name, None)
            if not layer.is_text_layer():
                raise ValueError(f"Layer '{layer.get_name()}' is not a text layer")
            layer    = Gimp.TextLayer.get_by_id(layer.get_id())
            # Check every value before changing anything, so a bad one leaves the layer as it was.
            color    = self._parse_color(new_color, "color") if new_color is not None else None
            font     = None
            if new_font is not None:
                font = self._resolve_font(str(new_font))  # the PDB takes a Gimp.Font, not a name
                if font is None:
                    raise ValueError(f"No font matches {new_font!r}; list_fonts shows the names")
            if new_size is not None:
                new_size = float(new_size)
                if new_size <= 0:
                    raise ValueError(f"size must be positive (got {new_size:g})")
            image.undo_group_start()
            try:
                if new_text is not None:
                    self._check_ok(layer.set_text(str(new_text)), "Setting the text")
                if font is not None:
                    self._check_ok(layer.set_font(font), "Setting the font")
                if new_size is not None:
                    self._check_ok(layer.set_font_size(new_size, Gimp.Unit.pixel()), "Setting the font size")
                if color is not None:
                    self._check_ok(layer.set_color(color), "Setting the color")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            size, _unit = layer.get_font_size()
            return {"status": "success", "results": {
                "layer_name": layer.get_name(), "text": layer.get_text(),
                "font": layer.get_font().get_name(), "size": size}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _warp_region(self, params):
        """Disabled: on GIMP 3.2 the GEGL warp graph used here erased the whole layer.

        The "stroke" was set from a Python list, which GEGL never stored, and the
        empty result was merged over every pixel. A real Gegl.Path stroke changes
        nothing, GIMP 3.2 will not create gegl:warp as a drawable filter, and
        plug-in-iwarp no longer exists, so there is no working warp to call.
        """
        return {
            "status": "error",
            "error": "warp_region is disabled: on GIMP 3.2 it erased the whole layer instead of warping it. "
                     "Use GIMP's Warp Transform tool by hand instead.",
        }

    def _list_fonts(self, params):
        """List available fonts."""
        try:
            filt  = params.get("filter", None) or ""
            limit = int(params.get("limit", 100))
            raw = Gimp.fonts_get_list(filt)
            # GIMP 3.2 returns (n, [Gimp.Font, ...]) or just [Gimp.Font, ...]
            if isinstance(raw, tuple):
                font_objs = list(raw[1]) if len(raw) > 1 else []
            else:
                font_objs = list(raw)
            names = []
            for f in font_objs[:limit]:
                if hasattr(f, "get_name"):
                    names.append(f.get_name())
                elif hasattr(f, "name"):
                    names.append(f.name)
                else:
                    names.append(str(f))
            return {"status": "success", "results": {
                "fonts": names, "count": len(names), "total": len(font_objs), "truncated": len(font_objs) > limit}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 8 — Filters & Effects
    # =========================================================================

    def _apply_drop_shadow(self, params):
        """Apply drop shadow via manual layer compositing (GIMP 3.2 compatible).

        gegl:drop-shadow and plug-in-drop-shadow are not reliably available in
        GIMP 3.2, so we build the shadow manually:
          1. Duplicate source layer → shadow_layer
          2. Fill it with shadow color (alpha-locked so shape is preserved)
          3. Set opacity and offset
          4. Gaussian-blur it with GEGL
        """
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            offset_x    = int(params.get("offset_x", 5))
            offset_y    = int(params.get("offset_y", 5))
            blur_radius = float(params.get("blur_radius", 10))
            color_str   = params.get("color", "black")
            opacity     = float(params.get("opacity", 60))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            shadow_color = self._parse_color(color_str, "color")
            image.undo_group_start()
            try:
                # 1. Duplicate source layer to use as shadow base
                shadow_layer = drawable.copy()
                src_pos = image.get_item_position(drawable)
                image.insert_layer(shadow_layer, None, src_pos + 1)

                # 2. Fill shadow layer with shadow color, preserving alpha shape
                Gimp.context_push()  # keep the user's foreground color
                try:
                    Gimp.context_set_foreground(shadow_color)
                    shadow_layer.set_lock_alpha(True)
                    Gimp.Drawable.edit_fill(shadow_layer, Gimp.FillType.FOREGROUND)
                    shadow_layer.set_lock_alpha(False)
                finally:
                    Gimp.context_pop()

                # 3. Set opacity and offset
                shadow_layer.set_opacity(opacity)
                offs = shadow_layer.get_offsets()
                shadow_layer.set_offsets(offs.offset_x + offset_x, offs.offset_y + offset_y)

                # 4. Soften it (plug-in-gauss no longer exists in GIMP 3)
                if blur_radius > 0:
                    self._apply_gegl_filter(image, shadow_layer, "gegl:gaussian-blur", {
                        "std-dev-x": blur_radius,
                        "std-dev-y": blur_radius,
                    })

                shadow_layer.set_name("Drop Shadow")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _apply_gaussian_blur(self, params):
        """Apply Gaussian blur."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            radius      = float(params.get("radius", 5.0))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:gaussian-blur", {
                    "std-dev-x": radius,
                    "std-dev-y": radius,
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _apply_pixelate(self, params):
        """Apply pixelate effect."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            block_size  = int(params.get("block_size", 10))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:pixelize", {
                    "size-x": block_size,
                    "size-y": block_size,
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _apply_emboss(self, params):
        """Apply emboss effect."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            azimuth     = float(params.get("azimuth", 315))
            elevation   = float(params.get("elevation", 45))
            depth       = int(round(float(params.get("depth", 2))))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:emboss", {
                    "type":      "emboss",
                    "azimuth":   azimuth,
                    "elevation": elevation,
                    "depth":     depth,
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _apply_vignette(self, params):
        """Apply vignette effect."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            softness    = float(params.get("softness", 0.8))
            shape       = str(params.get("shape", "circle")).lower()
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:vignette", {
                    "shape":    shape,
                    "softness": softness,
                    "radius":   1.0,
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _apply_noise(self, params):
        """Add noise to a layer."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            amount      = float(params.get("amount", 0.2))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:noise-hsv", {
                    "value-distance":      amount,
                    "saturation-distance": 0.0,
                    "hue-distance":        0.0,
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 9 — Export Pipelines
    # =========================================================================

    def _export_icon_sizes(self, params):
        """Export icon size sets for Android or iOS."""
        try:
            output_dir   = params.get("output_dir", "")
            platform_str = params.get("platform", "android").lower()
            src_index    = int(params.get("source_image_index", 0))
            fmt          = params.get("format", "png")

            ANDROID_SIZES = [
                (48,  "mdpi"), (72, "hdpi"), (96, "xhdpi"),
                (144, "xxhdpi"), (192, "xxxhdpi"), (512, "playstore"),
            ]
            IOS_SIZES = [
                (20, 1), (20, 2), (20, 3),
                (29, 1), (29, 2), (29, 3),
                (40, 2), (40, 3),
                (60, 2), (60, 3),
                (76, 1), (76, 2),
                (84, 2),   # 83.5x2 rounded
                (1024, 1),
            ]

            source_image = self._get_image(src_index)
            os.makedirs(output_dir, exist_ok=True)
            exported = []
            sizes = ANDROID_SIZES if platform_str == "android" else IOS_SIZES

            for entry in sizes:
                if platform_str == "android":
                    px, density = entry
                    out_name = f"icon_{density}_{px}.{fmt}"
                else:
                    px, scale = entry
                    actual_px = int(px * scale)
                    out_name = f"icon_{px}@{scale}x.{fmt}"
                    px = actual_px

                out_path = os.path.join(output_dir, out_name)
                dup = source_image.duplicate()
                try:
                    src_w = dup.get_width()
                    src_h = dup.get_height()
                    aspect = src_w / src_h
                    if aspect >= 1.0:
                        new_w = px
                        new_h = max(1, int(px / aspect))
                    else:
                        new_h = px
                        new_w = max(1, int(px * aspect))
                    dup.scale(new_w, new_h)
                    self._export_to_path(dup, out_path, fmt, 95, True)
                    exported.append({"size": px, "file_path": out_path})
                finally:
                    dup.delete()

            return {"status": "success", "results": {"exported": exported, "count": len(exported), "platform": platform_str}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _export_web_optimized(self, params):
        """Export as both JPEG and PNG, return comparison."""
        try:
            output_dir    = params.get("output_dir", "")
            jpeg_quality  = int(params.get("jpeg_quality", 85))
            image_index   = int(params.get("image_index", 0))
            max_width     = params.get("max_width", None)
            max_height    = params.get("max_height", None)
            png_compression = int(params.get("png_compression", 9))
            if not 0 <= png_compression <= 9:
                raise ValueError(f"png_compression must be between 0 and 9 (got {png_compression})")
            image = self._get_image(image_index)
            os.makedirs(output_dir, exist_ok=True)

            dup = image.duplicate()
            try:
                if max_width or max_height:
                    src_w = dup.get_width()
                    src_h = dup.get_height()
                    mw = int(max_width  or src_w)
                    mh = int(max_height or src_h)
                    aspect = src_w / src_h
                    if src_w / mw > src_h / mh:
                        new_w, new_h = mw, max(1, int(mw / aspect))
                    else:
                        new_h, new_w = mh, max(1, int(mh * aspect))
                    dup.scale(new_w, new_h)

                # Name the files after the source image; a duplicate never has a file, so all exports were "image".
                gio_file = image.get_file()
                if gio_file:
                    raw_name = os.path.splitext(gio_file.get_basename())[0]
                else:
                    # The new_canvas name, or Untitled-<id>; add the id so same-named images don't collide.
                    raw_name = self._image_name(image)
                    if not raw_name.endswith(f"-{image.get_id()}"):
                        raw_name = f"{raw_name}-{image.get_id()}"
                    raw_name = raw_name.replace(os.sep, "_")
                jpeg_path = os.path.join(output_dir, f"{raw_name}.jpg")
                png_path  = os.path.join(output_dir, f"{raw_name}.png")
                jpeg_size = self._export_to_path(dup, jpeg_path, "jpeg", jpeg_quality, True)
                png_size  = self._export_to_path(dup, png_path, "png", 95, True, compression=png_compression)
            finally:
                dup.delete()

            recommendation = "jpeg" if jpeg_size < png_size else "png"
            return {
                "status": "success",
                "results": {
                    "jpeg_path": jpeg_path, "jpeg_size": jpeg_size,
                    "png_path":  png_path,  "png_size":  png_size,
                    "recommendation": recommendation,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _batch_resize(self, params):
        """Resize all open images."""
        try:
            width           = params.get("width", None)
            height          = params.get("height", None)
            scale_factor    = params.get("scale_factor", None)
            maintain_aspect = bool(params.get("maintain_aspect", True))
            images  = Gimp.get_images()
            results = []
            for img in images:
                src_w = img.get_width()
                src_h = img.get_height()
                if scale_factor is not None:
                    new_w = max(1, int(src_w * float(scale_factor)))
                    new_h = max(1, int(src_h * float(scale_factor)))
                else:
                    tw = int(width  or src_w)
                    th = int(height or src_h)
                    if maintain_aspect:
                        if width and not height:
                            new_w = tw
                            new_h = max(1, int(src_h * tw / src_w))
                        elif height and not width:
                            new_h = th
                            new_w = max(1, int(src_w * th / src_h))
                        else:
                            aspect = src_w / src_h
                            if tw / th > aspect:
                                new_h = th
                                new_w = max(1, int(th * aspect))
                            else:
                                new_w = tw
                                new_h = max(1, int(tw / aspect))
                    else:
                        new_w, new_h = tw, th
                img.scale(new_w, new_h)
                results.append({"image_id": img.get_id(), "old_width": src_w, "old_height": src_h, "new_width": new_w, "new_height": new_h})
            Gimp.displays_flush()
            return {"status": "success", "results": {"results": results, "count": len(results)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _export_sprite_sheet(self, params):
        """Combine frames into a sprite sheet PNG that keeps transparency.

        Frames are copied with Layer.new_from_drawable (layers) or new_from_visible
        (images), so no selection, clipboard or GIMP color is touched.
        """
        try:
            output_path = str(params.get("output_path", "")).strip()
            columns     = params.get("columns", None)
            padding     = int(params.get("padding", 0))
            source      = self._choice(params.get("source", "layers"), {"layers": "layers", "images": "images"}, "source")
            image_index = int(params.get("image_index", 0))
            if not output_path:
                raise ValueError("output_path is required")
            if padding < 0:
                raise ValueError(f"padding must be 0 or more (got {padding})")
            if columns is not None and int(columns) < 1:
                raise ValueError(f"columns must be at least 1 (got {columns})")

            if source == "images":
                frames = Gimp.get_images()
            else:
                frames = self._get_image(image_index).get_layers()

            if not frames:
                return {"status": "error", "error": "No frames found"}

            # Use first frame dimensions as the cell size
            frame_w = frames[0].get_width()
            frame_h = frames[0].get_height()
            n = len(frames)
            cols = int(columns) if columns else max(1, math.ceil(math.sqrt(n)))
            rows = math.ceil(n / cols)

            sheet_w = cols * frame_w + (cols - 1) * padding
            sheet_h = rows * frame_h + (rows - 1) * padding

            # Alpha belongs to layers in GIMP 3: an RGB image with RGBA layers.
            sheet = Gimp.Image.new(sheet_w, sheet_h, Gimp.ImageBaseType.RGB)
            try:
                background = Gimp.Layer.new(sheet, "Background", sheet_w, sheet_h, Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)
                sheet.insert_layer(background, None, 0)
                background.fill(Gimp.FillType.TRANSPARENT)
                for i, frame in enumerate(frames):
                    if source == "images":
                        cell = Gimp.Layer.new_from_visible(frame, sheet, f"Frame {i + 1}")
                    else:
                        cell = Gimp.Layer.new_from_drawable(frame, sheet)
                    sheet.insert_layer(cell, None, 0)
                    if not cell.has_alpha():
                        cell.add_alpha()
                    cell.set_visible(True)
                    cell.set_offsets(0, 0)
                    # Crop or pad the frame to the cell. Called on Gimp.Layer because a copied text
                    # layer is a Gimp.TextLayer, whose own resize() sets the text box size instead.
                    self._check_ok(Gimp.Layer.resize(cell, frame_w, frame_h, 0, 0), "Fitting a frame to its cell")
                    cell.set_offsets((i % cols) * (frame_w + padding), (i // cols) * (frame_h + padding))
                # Not flattened: flattening drops the alpha channel.
                self._export_to_path(sheet, output_path, "png", 95, False)
            finally:
                sheet.delete()
            return {
                "status": "success",
                "results": {
                    "file_path":    output_path,
                    "columns":      cols,
                    "rows":         rows,
                    "frame_width":  frame_w,
                    "frame_height": frame_h,
                    "count":        n,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _export_social_media_kit(self, params):
        """Export for multiple social media platforms."""
        try:
            output_dir  = params.get("output_dir", "")
            platforms   = params.get("platforms", None)
            image_index = int(params.get("image_index", 0))

            PLATFORM_SIZES = {
                "instagram_square":   (1080, 1080),
                "instagram_story":    (1080, 1920),
                "twitter_header":     (1500, 500),
                "facebook_cover":     (820,  312),
                "youtube_thumbnail":  (1280, 720),
            }

            target_platforms = platforms if platforms else list(PLATFORM_SIZES.keys())
            source_image = self._get_image(image_index)
            os.makedirs(output_dir, exist_ok=True)
            exported = []

            for platform_name in target_platforms:
                if platform_name not in PLATFORM_SIZES:
                    continue
                target_w, target_h = PLATFORM_SIZES[platform_name]
                dup = source_image.duplicate()
                try:
                    src_w = dup.get_width()
                    src_h = dup.get_height()
                    # Scale to cover target (crop to exact size)
                    scale = max(target_w / src_w, target_h / src_h)
                    scaled_w = max(1, int(src_w * scale))
                    scaled_h = max(1, int(src_h * scale))
                    dup.scale(scaled_w, scaled_h)
                    # Crop to exact target
                    crop_x = (scaled_w - target_w) // 2
                    crop_y = (scaled_h - target_h) // 2
                    dup.crop(target_w, target_h, crop_x, crop_y)
                    out_path = os.path.join(output_dir, f"{platform_name}.png")
                    self._export_to_path(dup, out_path, "png", 95, True)
                    exported.append({"platform": platform_name, "file_path": out_path, "width": target_w, "height": target_h})
                finally:
                    dup.delete()

            return {"status": "success", "results": {"exported": exported, "count": len(exported)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 10 — Utility
    # =========================================================================

    @staticmethod
    def _image_name(image):
        """Stable name for list_images: file name, else the new_canvas name, else Untitled-<id>."""
        gio_file = image.get_file()
        if gio_file:
            return gio_file.get_basename()
        parasite = image.get_parasite(IMAGE_NAME_PARASITE)
        if parasite is not None:
            return bytes(parasite.get_data()).decode("utf-8", "replace")
        return f"Untitled-{image.get_id()}"

    def _list_images(self, params):
        """List all open images."""
        try:
            images = Gimp.get_images()
            image_list = []
            base_type_map = {
                Gimp.ImageBaseType.RGB:     "RGB",
                Gimp.ImageBaseType.GRAY:    "Grayscale",
                Gimp.ImageBaseType.INDEXED: "Indexed",
            }
            for i, img in enumerate(images):
                try:
                    gio_file = img.get_file()
                    file_path = gio_file.get_path() if gio_file else "Untitled"
                    image_list.append({
                        "index":      i,
                        "image_id":   img.get_id(),
                        "name":       self._image_name(img),
                        "width":      img.get_width(),
                        "height":     img.get_height(),
                        "color_mode": base_type_map.get(img.get_base_type(), "Unknown"),
                        "num_layers": len(img.get_layers()),
                        "file_path":  file_path,
                        "is_dirty":   img.is_dirty() if hasattr(img, "is_dirty") else None,
                    })
                except Exception as ex:
                    image_list.append({"index": i, "error": str(ex)})
            return {"status": "success", "results": {"images": image_list, "count": len(image_list)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _remember_display(self, image, display):
        """Record a window this plugin opened for image, so close_image can find it.

        GIMP 3 gives plug-ins no way to list an image's windows, so the ids are
        kept on the image itself in a parasite that is not saved into XCF files
        but outlives a plugin restart.
        """
        ids = [d.get_id() for d in self._recorded_displays(image)] + [display.get_id()]
        data = ",".join(str(i) for i in ids).encode("ascii")
        image.attach_parasite(Gimp.Parasite.new(IMAGE_DISPLAY_PARASITE, 0, data))

    @staticmethod
    def _recorded_displays(image):
        """Windows this plugin opened for image that are still open."""
        parasite = image.get_parasite(IMAGE_DISPLAY_PARASITE)
        if parasite is None:
            return []
        ids = bytes(parasite.get_data()).decode("ascii", "ignore").split(",")
        return [Gimp.Display.get_by_id(int(i)) for i in ids if i.isdigit() and Gimp.Display.id_is_valid(int(i))]

    @staticmethod
    def _delete_image_quietly(image):
        """Delete image if it has no windows; return False (without a GIMP error dialog) if it has one."""
        plug_in = Gimp.get_plug_in()
        handler = plug_in.get_pdb_error_handler()
        plug_in.set_pdb_error_handler(Gimp.PDBErrorHandler.PLUGIN)
        try:
            return bool(image.delete())
        finally:
            plug_in.set_pdb_error_handler(handler)

    def _set_active_image(self, params):
        """Raise a specific image's window to the front."""
        try:
            image_index = int(params.get("image_index", 0))
            image = self._get_image(image_index)
            displays = self._recorded_displays(image)
            if not displays:
                raise RuntimeError(
                    "This image has no window opened by the MCP server (for example it was opened from "
                    "GIMP's menus), and GIMP 3 gives plug-ins no way to find other windows. "
                    "Tools still work on it by image_index.")
            displays[-1].present()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "image_id": image.get_id()}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _convert_color_mode(self, params):
        """Convert image color mode."""
        try:
            image_index = int(params.get("image_index", 0))
            base_types  = {"RGB": Gimp.ImageBaseType.RGB, "RGBA": Gimp.ImageBaseType.RGB,
                           "GRAY": Gimp.ImageBaseType.GRAY, "GRAYA": Gimp.ImageBaseType.GRAY,
                           "INDEXED": Gimp.ImageBaseType.INDEXED}
            mode        = str(params.get("mode", "RGB")).strip().upper()
            target      = self._choice(mode, base_types, "mode")
            num_colors  = int(params.get("num_colors", 256))
            if mode == "INDEXED":
                self._check_range(num_colors, 1, 256, "num_colors")
            image = self._get_image(image_index)
            image.undo_group_start()
            try:
                # GIMP refuses to convert an image to the mode it already has.
                if image.get_base_type() != target:
                    if target == Gimp.ImageBaseType.RGB:
                        self._check_ok(image.convert_rgb(), "Converting to RGB")
                    elif target == Gimp.ImageBaseType.GRAY:
                        self._check_ok(image.convert_grayscale(), "Converting to grayscale")
                    else:
                        self._check_ok(image.convert_indexed(
                            Gimp.ConvertDitherType.NONE, Gimp.ConvertPaletteType.GENERATE,
                            num_colors, False, False, ""), "Converting to indexed")
                if mode in ("RGBA", "GRAYA"):
                    for layer in image.get_layers():
                        if not layer.has_alpha():
                            layer.add_alpha()
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "mode": mode}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _close_image(self, params):
        """Close an image, optionally saving it as XCF first."""
        try:
            from gi.repository import Gio
            image_index = int(params.get("image_index", 0))
            save_first  = bool(params.get("save_first", False))
            image    = self._get_image(image_index)
            image_id = image.get_id()
            saved_to = None
            if save_first:
                xcf_file = image.get_xcf_file()
                img_file = image.get_file()
                if xcf_file and xcf_file.get_path():
                    saved_to = xcf_file.get_path()  # opened from this XCF, so saving over it is the save
                else:
                    if img_file and img_file.get_path():
                        stem = os.path.splitext(img_file.get_path())[0]
                    else:
                        stem = os.path.join(tempfile.gettempdir(), f"gimp_backup_{image_id}")
                    # Never overwrite another file, such as a layered photo.xcf next to photo.png.
                    saved_to, n = stem + ".xcf", 1
                    while os.path.exists(saved_to):
                        n += 1
                        saved_to = f"{stem}-{n}.xcf"
                pdb  = Gimp.get_pdb()
                proc = pdb.lookup_procedure("gimp-xcf-save")
                cfg  = proc.create_config()
                cfg.set_property("image", image)
                cfg.set_property("file", Gio.File.new_for_path(saved_to))
                if proc.run(cfg).index(0) != Gimp.PDBStatusType.SUCCESS:
                    raise RuntimeError(f"Saving {saved_to} failed: {pdb.get_last_error()}; the image was left open")
            displays = self._recorded_displays(image)
            if displays:
                # Closing a window of a dirty image could open a "save changes?" dialog;
                # save_first is how callers keep their changes.
                image.clean_all()
                for display in displays:
                    display.delete()
            if image.is_valid() and not self._delete_image_quietly(image):
                saved_note = f" It was saved to {saved_to}." if saved_to else ""
                raise RuntimeError(
                    "This image has a window the MCP server did not open (for example one opened from "
                    "GIMP's menus), and GIMP 3 gives plug-ins no way to close other windows. "
                    "Close it in GIMP." + saved_note)
            return {"status": "success", "results": {"status": "success", "closed_image_id": image_id, "saved_to": saved_to}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _get_selection_bounds(self, params):
        """Get selection bounding rectangle."""
        try:
            image = self._get_image(int(params.get("image_index", 0)))
            _ok, non_empty, x1, y1, x2, y2 = Gimp.Selection.bounds(image)
            return {
                "status": "success",
                "results": {
                    "has_selection": bool(non_empty),
                    "x":      x1,
                    "y":      y1,
                    "width":  x2 - x1,
                    "height": y2 - y1,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _get_pixel_color(self, params):
        """Get the color at one pixel of the visible image, or of one layer."""
        try:
            image_index   = int(params.get("image_index", 0))
            layer_name    = params.get("layer_name", None)
            x             = int(params.get("x", 0))
            y             = int(params.get("y", 0))
            sample_merged = bool(params.get("sample_merged", True))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            (r, g, b), alpha = self._pick_color(image, drawable, x, y, sample_merged)
            return {
                "status": "success",
                "results": {
                    "color_hex": f"#{r:02x}{g:02x}{b:02x}",
                    "color_rgb": [r, g, b],
                    "alpha":     round(alpha * 255),
                    "sampled":   "all visible layers" if sample_merged else drawable.get_name(),
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _get_histogram(self, params):
        """Get histogram statistics for a layer channel."""
        try:
            CHANNEL_MAP = {
                "value": Gimp.HistogramChannel.VALUE,
                "red":   Gimp.HistogramChannel.RED,
                "green": Gimp.HistogramChannel.GREEN,
                "blue":  Gimp.HistogramChannel.BLUE,
                "alpha": Gimp.HistogramChannel.ALPHA,
            }
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            channel_str = params.get("channel", "value")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            channel  = self._choice(channel_str, CHANNEL_MAP, "channel")
            pdb  = Gimp.get_pdb()
            proc = pdb.lookup_procedure("gimp-drawable-histogram")
            cfg  = proc.create_config()
            cfg.set_property("drawable",    drawable)
            cfg.set_property("channel",     channel)
            cfg.set_property("start-range", 0.0)
            cfg.set_property("end-range",   1.0)
            result = proc.run(cfg)
            if result.index(0) != Gimp.PDBStatusType.SUCCESS:
                raise RuntimeError(f"Histogram failed: {pdb.get_last_error()}")
            # Value 0 is the procedure status; the statistics follow it.
            return {
                "status": "success",
                "results": {
                    "layer":      drawable.get_name(),
                    "mean":       result.index(1),
                    "std_dev":    result.index(2),
                    "median":     result.index(3),
                    "pixels":     result.index(4),
                    "count":      result.index(5),
                    "percentile": result.index(6),
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


Gimp.main(MCPPlugin.__gtype__, sys.argv)
