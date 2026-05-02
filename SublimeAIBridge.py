"""SublimeAIBridge: in-process MCP server hosted inside Sublime Text.

Replaces the previous external-process design. Drop the package into Packages/
(Preferences > Browse Packages...). On load, it starts a Streamable-HTTP MCP
server on 127.0.0.1:8765 by default; configure via SublimeAIBridge.sublime-settings.
"""
import os
import sublime
import sublime_plugin
import threading

from .mcp_lite.server import MCPServer
from .mcp_lite.transport_http import HTTPTransport
from .tools.sublime_tools import register_all


SERVER_NAME = "sublime-ai"
SERVER_VERSION = "0.2.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_FALLBACK_RANGE = 10  # try DEFAULT_PORT .. DEFAULT_PORT+9 before giving up
PORT_STATE_FILENAME = "SublimeAIBridge.port"

_lock = threading.Lock()
_transport = None  # type: HTTPTransport | None
_mcp = None  # type: MCPServer | None


# ---------------------------------------------------------------- text command
# Defined at the top level so ST registers it as a TextCommand. The tools
# package invokes it by name, so this class never imports from tools/.

class SublimeAiBridgeApplyEditsCommand(sublime_plugin.TextCommand):
    """Atomic batch edit. `edits` is a list of {start, end, text} dicts using
    absolute character offsets. Edits are applied in reverse order so earlier
    replacements don't shift the offsets of later ones. Whole batch forms a
    single ST undo entry."""
    def run(self, edit, edits):
        for e in sorted(edits, key=lambda x: -int(x["start"])):
            region = sublime.Region(int(e["start"]), int(e["end"]))
            self.view.replace(edit, region, e["text"])


# ---------------------------------------------------------------- logging

def _log(fmt, *args):
    try:
        msg = fmt % args if args else fmt
    except Exception:
        msg = "{} {!r}".format(fmt, args)
    print("[SublimeAIBridge] " + msg)


# ---------------------------------------------------------------- port

def _settings():
    return sublime.load_settings("SublimeAIBridge.sublime-settings")


def _state_path():
    return os.path.join(sublime.cache_path(), PORT_STATE_FILENAME)


def _write_port_state(port):
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            f.write("{}\n".format(port))
    except OSError as e:
        _log("could not write port state file: %s", e)


def _bind_with_fallback(mcp, host, preferred_port):
    last_err = None
    for offset in range(PORT_FALLBACK_RANGE):
        port = preferred_port + offset
        transport = HTTPTransport(mcp, host=host, port=port, logger=_log)
        try:
            transport.start()
            return transport
        except OSError as e:
            last_err = e
            transport.stop()
    raise RuntimeError("could not bind any port in {}..{} ({})".format(
        preferred_port, preferred_port + PORT_FALLBACK_RANGE - 1, last_err))


# ---------------------------------------------------------------- lifecycle

def plugin_loaded():
    global _transport, _mcp
    with _lock:
        if _transport is not None:
            return  # idempotent against double-load (ST reloads)

        settings = _settings()
        host = settings.get("host") or DEFAULT_HOST
        port = int(settings.get("port") or DEFAULT_PORT)

        mcp = MCPServer(SERVER_NAME, version=SERVER_VERSION, logger=_log)
        register_all(mcp)

        try:
            transport = _bind_with_fallback(mcp, host, port)
        except Exception as e:
            _log("failed to start MCP server: %s", e)
            sublime.status_message("SublimeAIBridge: failed to start ({})".format(e))
            mcp.shutdown()
            return

        _mcp = mcp
        _transport = transport
        _write_port_state(transport.bound_port)
        sublime.status_message("SublimeAIBridge: MCP at {}:{}/mcp".format(
            host, transport.bound_port))


def plugin_unloaded():
    global _transport, _mcp
    with _lock:
        t = _transport
        m = _mcp
        _transport = None
        _mcp = None
    if t is not None:
        try:
            t.stop(timeout=2.0)
        except Exception as e:
            _log("transport stop raised: %s", e)
    if m is not None:
        try:
            m.shutdown()
        except Exception as e:
            _log("mcp shutdown raised: %s", e)


# ---------------------------------------------------------------- ux commands

class SublimeAiBridgeShowPortCommand(sublime_plugin.WindowCommand):
    """Command palette: report the bound MCP port and URL."""
    def is_enabled(self):
        return _transport is not None

    def run(self):
        if _transport is None:
            sublime.message_dialog("SublimeAIBridge is not running.")
            return
        url = "http://{}:{}/mcp".format(_transport.host, _transport.bound_port)
        sublime.message_dialog("SublimeAIBridge MCP endpoint:\n\n{}".format(url))


class SublimeAiBridgeRestartCommand(sublime_plugin.WindowCommand):
    """Command palette: restart the embedded MCP server."""
    def run(self):
        plugin_unloaded()
        plugin_loaded()
        if _transport is not None:
            sublime.status_message("SublimeAIBridge: restarted on port {}".format(
                _transport.bound_port))
        else:
            sublime.status_message("SublimeAIBridge: restart failed; see console")
