"""MCP resources exposed by the AI Bridge server.

Resources are URI-addressable read-only content. Clients that support
@-mention of MCP resources (Claude Desktop, Cursor, etc.) let users inline
these into a chat message; the client fetches the content and substitutes
it where the @mention sat.

URI scheme: `aibridge://...` -- prefixed with the package's canonical short
name to avoid collision with anything Sublime HQ may ship under their own
namespace. Client coverage varies; clients that don't surface resources at
all are unaffected by this module.

Currently exposed:

  - aibridge://selection             (static)
        Current selection in the active view, formatted with a small header.

Output formatting matches `sublime_prompts.selection` so the LLM sees the
same context whether the user picks a slash-prompt or @-mentions the
resource.
"""
import sublime

from .sublime_tools import _on_main
from .sublime_prompts import _fence_lang_from_scope


def _format_block(header_rows, lang, body_text):
    """Header lines followed by a fenced code block. Mirrors the layout used
    by sublime_prompts._format_message but without a leading question."""
    width = max(len(label) for label, _ in header_rows) + 1
    lines = []
    for label, value in header_rows:
        prefix = label + ":"
        pad = " " * (width + 1 - len(prefix))
        lines.append("{}{}{}".format(prefix, pad, value))
    lines.append("")
    lines.append("```" + lang if lang else "```")
    lines.append(body_text)
    lines.append("```")
    return "\n".join(lines)


def selection_resource() -> str:
    """Active view's selection, with file path and line range as a header.

    Multi-cursor selections are concatenated in document order with blank
    lines between regions, matching the `selection` prompt.
    """
    def go():
        w = sublime.active_window()
        if w is None:
            raise RuntimeError("no active Sublime window")
        view = w.active_view()
        if view is None:
            raise RuntimeError("no active view")
        regions = [r for r in view.sel() if not r.empty()]
        if not regions:
            raise RuntimeError("no text is selected in the active view")

        regions.sort(key=lambda r: r.begin())
        text = "\n\n".join(view.substr(r) for r in regions)

        path = view.file_name() or view.name() or "<untitled {}>".format(view.id())

        start_row, _ = view.rowcol(regions[0].begin())
        end_pt = regions[-1].end()
        end_row, end_col = view.rowcol(end_pt)
        if end_col == 0 and end_row > 0:
            end_row -= 1
        total_lines = view.rowcol(view.size())[0] + 1

        scope = view.scope_name(regions[0].begin())
        return _format_block(
            [
                ("File", path),
                ("Lines", "{}-{} (of {})".format(
                    start_row + 1, end_row + 1, total_lines)),
            ],
            _fence_lang_from_scope(scope),
            text,
        )

    return _on_main(go)


# (uri, fn, display_name, mime_type, timeout)
# `display_name` is what most clients show in their @-mention picker; keep
# it short and friendly. The URI travels invisibly on the wire.
RESOURCE_SPECS = [
    ("aibridge://selection", selection_resource,
     "selection", "text/markdown", 5.0),
]


def register_all(mcp):
    for uri, fn, display_name, mime_type, timeout in RESOURCE_SPECS:
        mcp.register_resource(
            uri, fn, name=display_name,
            mime_type=mime_type, timeout=timeout)
