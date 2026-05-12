# AI Bridge

An MCP (Model Context Protocol) server, hosted *inside* Sublime Text as a
plugin, that lets an LLM read and edit code through Sublime's own APIs —
symbol index, syntax-aware definition extraction, project search, and atomic
in-buffer edits.

The LLM gets the same "go to definition" and "find references" power that
Sublime gives you, plus the ability to read and rewrite whole functions while
leaving ST's undo stack intact.

There is **no separate server process** to start. Drop the package into
Sublime, and the MCP endpoint at `http://127.0.0.1:8765/mcp` comes up with it.

https://github.com/user-attachments/assets/75c83736-1e69-49b7-bb53-dee496b22bf0

## What it does

Tools are grouped by domain. Every coordinate is 1-indexed, matching
Sublime's status bar.

### Project structure

| Tool | What it does |
|---|---|
| `list_folders_in_project()` | Root folders + project file of the active ST window. |
| `find_files_in_project(pattern, regex?, glob?)` | Find files by basename across the project. |

### File content

| Tool | What it does |
|---|---|
| `get_file_content(project_file, start_line_number, stop_line_number?)` | Read a line-range slice of a file from disk. 1-indexed, inclusive. Omit `stop_line_number` to read through end-of-file. |
| `set_file_content(project_file, content, start_line_number, stop_line_number?, save?)` | Replace a contiguous range of lines. Omit `stop_line_number` to replace through end-of-file. Single ST undo entry. |

### Function / symbol

> "Function" is shorthand. These tools work on any symbol kind Sublime
> indexes — functions, methods, classes, namespaces, structs, etc. The
> result `kind` field tells you the actual type.

| Tool | What it does |
|---|---|
| `find_function_in_project(symbol)` | Find DECLARATION sites for `symbol`. |
| `find_function_in_open_files(symbol)` | Same as above, restricted to currently open buffers. |
| `find_function_usages_in_project(symbol)` | Find CALL SITES / reference sites for `symbol`. |
| `list_functions_in_file(file_path)` | List all symbols in a single file. |
| `get_function_content(file_path, name, row?)` | Return the full source text of a definition (signature through closing brace). |
| `set_function_content(file_path, name, new_text, save?, row?)` | Replace an entire definition with `new_text`. Single undo entry. |

### Text search

| Tool | What it does |
|---|---|
| `search_text_in_project(pattern, regex?, case_sensitive?, glob?, max_results?)` | Grep file contents across the project (on-disk). |
| `search_text_in_open_files(pattern, regex?, case_sensitive?, max_results?)` | Grep across the in-memory buffer text of currently open views — sees unsaved edits. |

### Editor state

| Tool | What it does |
|---|---|
| `get_current_selections()` | Return the active view's cursor/selection state with the file path and 1-indexed line ranges. |
| `run_sublime_command(command, args?, file_path?)` | Run any Sublime Text command (formatters, sort, custom plugins). Escape hatch. |

## How it works

Sublime Text's Python API only runs *inside* Sublime Text. Earlier versions
of this project worked around that with an external Python process talking
to a thin TCP bridge in the plugin. That's gone. Now the MCP server itself
lives inside the plugin, served from a stdlib `http.server` thread:

```
LLM client ──MCP / Streamable-HTTP──▶  127.0.0.1:8765/mcp
                                            │
                                            ▼
                                    AI Bridge plugin
                                    (mcp_lite + tool dispatch)
                                            │
                                            ▼
                                    sublime / sublime_plugin APIs
```

There is no `mcp` Python package dependency. The protocol layer
(`mcp_lite/`) is a hand-written ~400-line stdlib-only implementation of the
MCP wire format — JSON-RPC 2.0 over Streamable-HTTP — sized for exactly the
tools this plugin exposes. This sidesteps `pydantic_core`, `cryptography`,
and other binary-wheel deps that don't play well with Sublime Text's
embedded Python.

The "active project" for any given call is whatever
`sublime.active_window()` returns — that's the ST window you most recently
focused, even when ST isn't the foreground app.

## Requirements

- **Sublime Text 4** (build 4081 or newer). Plugin runs under ST's bundled
  Python 3.8.
- An MCP-capable client (Claude Code, Claude Desktop, LM Studio, etc.)
- **No `pip` installs.** No external Python. Stdlib only.

## Installation

In Sublime Text: `Preferences → Browse Packages...`. This opens
`%APPDATA%\Sublime Text\Packages\` on Windows.[^1]

Create a folder called `AI Bridge` inside it and copy the entire
contents of this repo into it. Final layout:

```
Packages/
└── AI Bridge/
    ├── .python-version
    ├── AIBridge.py
    ├── AI Bridge.sublime-settings
    ├── Main.sublime-menu
    ├── mcp_lite/
    │   ├── __init__.py
    │   ├── jsonrpc.py
    │   ├── schema.py
    │   ├── server.py
    │   └── transport_http.py
    └── tools/
        ├── __init__.py
        └── sublime_tools.py
```

> **Watch out for `.python-version`.** It's a hidden file on macOS/Linux
> and easy to drop during a copy. Without it, ST loads the plugin under
> Python 3.3 and you'll see `ImportError: No module named 'typing'` in
> the console.

The plugin auto-loads. Open ST's console (`` Ctrl+` ``); on a successful
load you'll see:

```
[AI Bridge] MCP HTTP transport listening on 127.0.0.1:8765/mcp
```

If port 8765 is busy, the plugin walks 8766–8774 looking for a free one.
The actual bound port is also written to `<Cache>/AIBridge.port`,
and the command palette entry **AI Bridge: Show Port** displays it.

[^1]: On macOS: `~/Library/Application Support/Sublime Text/Packages/`. On Linux: `~/.config/sublime-text/Packages/`.

## Settings

`AI Bridge.sublime-settings`:

```json
{
    "host": "127.0.0.1",
    "port": 8765
}
```

`host` is fixed to localhost regardless — the server rejects requests with
non-localhost `Origin` headers. `port` is just the *preferred* port; the
fallback walk above kicks in if it's busy.

## Connecting MCP clients

Sublime Text must be running with the plugin loaded before any client will
see tools.

### Claude Code (CLI or desktop)

```
claude mcp add --transport http sublime-ai-bridge http://127.0.0.1:8765/mcp
```

Or edit `%USERPROFILE%\.claude.json` directly:

```json
{
  "mcpServers": {
    "sublime-ai-bridge": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

Restart Claude Code (close and relaunch — including any background tray
process). Open a new conversation; the `mcp__sublime-ai-bridge__*` tools should
appear.

### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json`. Some Claude Desktop
builds support native HTTP MCP servers; others only support stdio. The
stdio bridge form below works on every version (requires
[Node.js](https://nodejs.org/) installed):

```json
{
  "mcpServers": {
    "sublime-ai-bridge": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8765/mcp"]
    }
  }
}
```

`mcp-remote` is a tiny shim that turns the HTTP MCP server into a stdio
one. Fully quit Claude Desktop (including system tray) and relaunch.

If your build supports native HTTP, this also works:

```json
{
  "mcpServers": {
    "sublime-ai-bridge": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

### LM Studio

Edit `%USERPROFILE%\.lmstudio\mcp.json` (or use **Program → Install → Edit
mcp.json** in the app):

```json
{
  "mcpServers": {
    "sublime-ai-bridge": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

Toggle the server on in the Program panel.

## Smoke test

In a new conversation with any connected client, ask:

> *Use sublime-ai-bridge to list folders in the project and find the function
> definition of [some function name in your project].*

If you get the folder list and a definition location back, the chain is
alive.

## Coordinate convention

Every tool reads and writes row/column values as **1-indexed**, matching
what Sublime's status bar displays ("Line 12, Column 4"). This is normalized
at the boundary — Sublime's `view.rowcol()` is internally 0-indexed, but
you never see that.

Practical implication: a row from `find_function_in_project`'s output can
be passed directly into `get_function_content(..., row=N)` or
`set_function_content(..., row=N)` to disambiguate when the same name
appears multiple times in one file.

## Edit semantics

`set_function_content`, `set_file_content`, and `apply_edits_to_file`
follow these rules:

- **One call = one undo entry.** Whether you replace one character or
  fifty lines, `Ctrl+Z` in Sublime reverts the whole call.
- **`save=False` (default)** leaves the buffer dirty so you can review and
  `Ctrl+S` (or `Ctrl+Z`) yourself.
- **If the file isn't already open**, the plugin loads it as a transient
  view, applies the edit, **forces a save regardless of the `save=` flag**,
  and closes the view. Otherwise the edit would be lost when the transient
  view closes.
- **Read/write asymmetry.** `get_file_content` reads from disk;
  `set_file_content` and the function-edit tools go through ST's view layer
  so they integrate with undo and respect any unsaved buffer state. If you
  need to grep the unsaved buffer text, use `search_text_in_open_files`.
- **First match wins** when a name is ambiguous in a single file. Pass
  `row=` to target a specific occurrence.

`get_function_content` uses Sublime's syntax scopes (`meta.function`,
`meta.method`, `meta.class`, etc.) to find the body, falling back to
bracket matching that skips strings and comments. Works reliably for PHP,
JS/TS, Python, Ruby, Go, Rust, C/C++, Java. Less common syntax packages
may not define those scopes — in which case the bracket fallback handles
any `{}`-delimited language.

## Known limits

- **Symbol lookups only see what Sublime has indexed.** Files outside
  project folders, or files whose syntax has no symbol indexer, won't
  appear in `find_function_in_project` /
  `find_function_usages_in_project`. This is a deliberate tradeoff — the
  bridge does not force re-indexing.
- **Content search uses Python's `re` module**, not Sublime's Boost
  regex. Most patterns are identical, but advanced features (some
  lookbehinds, named groups) follow Python semantics.
- **`search_text_in_project` is timeout-bounded** at 30s by default.
  Adversarial regex (`(a+)+b` style) and large unfiltered project trees
  both hit this wall. The catastrophic case is hard-killed via cooperative
  cancellation, ST stays responsive, and the next call works. If routine
  searches time out, your project's `folder_exclude_patterns` likely need
  to skip `vendor`, `node_modules`, framework code, etc.
- **No auth.** The server listens on `127.0.0.1` only and rejects
  non-localhost `Origin` headers. Anything else that can run code on your
  machine can call it. Don't expose port 8765 to a network.
- **Edits in dirty buffers** are written to the buffer, not disk. If you
  have unsaved changes in ST and ask the LLM to edit the same file, the
  LLM's edit layers on top of your unsaved work in a single undo entry —
  `Ctrl+Z` reverts both at once.

## Troubleshooting

**ImportError: No module named 'typing' on plugin load.**
Sublime Text 4 defaults plugins to Python 3.3 unless a `.python-version`
file with the literal string `3.8` exists at the package root. Make sure
that file is present in `Packages/AI Bridge/`.

**MCP client doesn't show any `sublime-ai-bridge` tools.**
1. Verify the plugin is loaded: open ST's console (`` Ctrl+` ``). On
   startup you should see
   `[AI Bridge] MCP HTTP transport listening on 127.0.0.1:8765/mcp`.
   Any traceback there is your culprit.
2. Verify the server is bound: `curl -i http://127.0.0.1:8765/mcp` should
   give an HTTP response (a `200` SSE stream for GET, since the server
   serves an idle event stream on that endpoint).
3. Some clients only load MCP servers at startup — fully restart the
   client (including system tray) after editing its config.

**Port already in use on another tool's side.**
The plugin walks 8765 → 8774 to find a free port. Use the command palette
**AI Bridge: Show Port** to see what got bound, and update your MCP
client's URL to match. The bound port is also written to
`<Cache>/AIBridge.port`.

**`definition '...' not found` from `set_function_content`.**
The symbol exists in the index but `view.symbol_regions()` for that file
doesn't list it under the exact name you passed, or scope detection
couldn't locate the body. Run `list_functions_in_file(file_path)` to see
exactly what names are recognized. As a fallback, use `apply_edits_to_file`
with explicit row/col regions.

**Edit applied but on-disk file unchanged.**
You called `set_function_content` (or sibling) with `save=false` (the
default) on a file that was already open in Sublime. Either save in ST
manually, call `save_open_file`, or pass `save=true` to the edit call.

## License

MIT — see [LICENSE](LICENSE).
