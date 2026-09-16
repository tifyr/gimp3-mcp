"""Command line for gimp3-mcp: run the MCP server, or install the GIMP plugin."""

import argparse
import os
import re
import shutil
import stat
import sys
from pathlib import Path

PLUGIN = "gimp-mcp-plugin"


def plugin_source():
    """The plugin file shipped in the package, or the one at the root of a source checkout."""
    packaged = Path(__file__).parent / "plugin" / f"{PLUGIN}.py"
    return packaged if packaged.exists() else Path(__file__).resolve().parent.parent / f"{PLUGIN}.py"


def gimp_settings_bases():
    """Folders holding GIMP's per-version settings folders (3.0, 3.2, ...) on this platform."""
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "GIMP"]
    if sys.platform == "win32":
        return [Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "GIMP"]
    return [
        Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "GIMP",
        home / "snap" / "gimp" / "current" / ".config" / "GIMP",
        home / ".var" / "app" / "org.gimp.GIMP" / "config" / "GIMP",
    ]


def newest_gimp3_settings():
    """The settings folder of the newest GIMP 3.x found, or None."""
    found = [folder for base in gimp_settings_bases() if base.is_dir()
             for folder in base.iterdir() if folder.is_dir() and re.fullmatch(r"3\.\d+", folder.name)]
    return max(found, key=lambda folder: int(folder.name.split(".")[1]), default=None)


def install_plugin(settings):
    """Copy the plugin to settings/plug-ins/gimp-mcp-plugin/ and make it executable."""
    target = settings / "plug-ins" / PLUGIN / f"{PLUGIN}.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(plugin_source(), target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def run_server():
    try:
        from gimp3_mcp import server
    except ImportError:  # a source checkout keeps the server at the repo root
        import gimp_mcp_server as server
    server.main()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="gimp3-mcp",
        description="MCP server for GIMP 3. Without a command it runs the MCP server over stdio for your AI client.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the MCP server over stdio (the default)")
    install = commands.add_parser("install-plugin", help="copy the GIMP plugin into GIMP's plug-ins folder")
    install.add_argument("--gimp-dir", type=Path, metavar="DIR",
                         help="GIMP's settings folder for your version, such as ~/.config/GIMP/3.2 "
                              "(default: the newest GIMP 3.x folder found)")
    args = parser.parse_args(argv)

    if args.command != "install-plugin":
        run_server()
        return
    settings = args.gimp_dir.expanduser() if args.gimp_dir else newest_gimp3_settings()
    if settings is None:
        parser.exit(1, "No GIMP 3 settings folder found. Start GIMP 3 once so it creates one, or pass --gimp-dir.\n")
    if not settings.is_dir():
        parser.exit(1, f"{settings} is not a folder. Pass GIMP's settings folder for your version, "
                       "such as ~/.config/GIMP/3.2.\n")
    target = install_plugin(settings)
    print(f"Installed the GIMP plugin: {target}")
    print("Restart GIMP, then choose Tools > MCP > Start MCP Server.")


if __name__ == "__main__":
    main()
