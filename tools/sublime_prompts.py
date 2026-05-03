"""User-invoked prompts exposed by the SublimeAIBridge MCP server.

Prompts (unlike tools) are picked by the *user* from a slash-menu in the
client, and let the server pre-bake editor state -- here, the active view's
current selection -- into a ready-to-send message.
"""
import sublime

from .sublime_tools import _on_main, get_function_content


# Map a Sublime base scope (e.g. "source.python", "text.html.basic") to the
# fence language we want to emit. Only entries whose fenced rendering differs
# from the raw scope segment need to be listed; everything else falls through
# to the segment-extraction default.
_FENCE_OVERRIDES = {
    "source.c++": "cpp",
    "source.cs": "csharp",
    "source.objc": "objc",
    "source.objc++": "objcpp",
    "source.shell.bash": "bash",
    "source.js": "javascript",
    "source.ts": "typescript",
    "source.tsx": "tsx",
    "source.jsx": "jsx",
    "text.html.basic": "html",
    "text.html.markdown": "markdown",
    "text.xml": "xml",
}


def _fence_lang_from_scope(scope):
    if not scope:
        return ""
    base = scope.split()[0].strip()
    if base in _FENCE_OVERRIDES:
        return _FENCE_OVERRIDES[base]
    # "source.python" -> "python", "text.plain" -> "plain"
    parts = base.split(".")
    if len(parts) >= 2 and parts[0] in ("source", "text"):
        return parts[-1]
    return ""


def _grab_selection():
    """Return (display_path, fence_lang, selected_text). Raises if no selection."""
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

        # Concatenate multi-cursor selections in document order with a blank
        # line between them; single-selection (the common case) is unchanged.
        regions.sort(key=lambda r: r.begin())
        text = "\n\n".join(view.substr(r) for r in regions)

        path = view.file_name()
        if path:
            display = path
        else:
            display = view.name() or "<untitled {}>".format(view.id())

        scope = view.scope_name(regions[0].begin()) if regions else ""
        return display, _fence_lang_from_scope(scope), text

    return _on_main(go)


def _grab_cursor_location():
    """Return (file_path, row_1based, fence_lang). Raises if no saved file."""
    def go():
        w = sublime.active_window()
        if w is None:
            raise RuntimeError("no active Sublime window")
        view = w.active_view()
        if view is None:
            raise RuntimeError("no active view")
        path = view.file_name()
        if not path:
            raise RuntimeError(
                "active view has no saved file path (untitled buffer)")
        regions = list(view.sel())
        if not regions:
            raise RuntimeError("active view has no cursor")
        point = regions[0].begin()
        row, _ = view.rowcol(point)
        return path, row + 1, _fence_lang_from_scope(view.scope_name(point))
    return _on_main(go)


def _format_with_question(question, lang, body_text):
    fence_open = "```" + lang if lang else "```"
    lines = []
    if question:
        lines.append(question.strip())
        lines.append("")
    lines.append(fence_open)
    lines.append(body_text)
    lines.append("```")
    return "\n".join(lines)


def selection(question: str = "") -> dict:
    """Insert the current Sublime selection into the prompt.

    Pairs the user's question (if any) with the active view's selected text,
    fenced with the buffer's syntax. If multiple regions are selected, they
    are concatenated in document order.
    """
    display, lang, text = _grab_selection()
    body = _format_with_question(question, lang, text)
    return {
        "description": display,
        "messages": [
            {"role": "user", "content": {"type": "text", "text": body}},
        ],
    }


def function(question: str = "") -> dict:
    """Insert the function under the cursor into the prompt.

    Reads the active view's cursor row and pulls the enclosing function via
    get_function_content (innermost match wins for nested defs). Errors if
    the active view has no saved path, or if the cursor isn't inside a
    recognized function/method.
    """
    file_path, row, lang = _grab_cursor_location()
    result = get_function_content(file_path=file_path, row=row)
    matches = result.get("matches") or []
    if not matches:
        raise RuntimeError(
            "cursor is not inside a recognized function (line {} of {})".format(
                row, file_path))

    m = matches[0]
    func_name = m.get("name") or "<anonymous>"
    text = m.get("text") or ""
    start, end = m.get("start_row"), m.get("end_row")

    body = _format_with_question(question, lang, text)
    description = "{} · {} (lines {}-{})".format(file_path, func_name, start, end)
    return {
        "description": description,
        "messages": [
            {"role": "user", "content": {"type": "text", "text": body}},
        ],
    }


PROMPT_SPECS = [
    (selection, {"question": "Your question about the selected code"}, 5.0),
    (function, {"question": "Your question about the enclosing function"}, 20.0),
]


def register_all(mcp):
    for fn, arg_descriptions, timeout in PROMPT_SPECS:
        mcp.register_prompt(fn, arg_descriptions=arg_descriptions, timeout=timeout)
