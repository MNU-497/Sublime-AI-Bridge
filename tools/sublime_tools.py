"""Tool implementations for the AI Bridge MCP server.

All Sublime API access is marshaled to the main thread via _on_main. The
search_text_in_project tool checks mcp_lite.server.is_cancelled() inside its
loop so the dispatcher's wall-clock timeout can interrupt runaway regex.

Tools are grouped by domain in this file; the registration order at the
bottom (TOOL_SPECS) controls how they appear in the MCP `tools/list`
response, and is intentionally grouped:
  1. Project structure
  2. File content
  3. Function/symbol
  4. Text search
  5. Editor state
"""
import fnmatch
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import sublime

from ..mcp_lite.server import is_cancelled


MAIN_THREAD_TIMEOUT = 15.0
LOAD_POLL_TIMEOUT = 5.0
FILENAME_RESULT_CAP = 1000
SEARCH_CANCEL_CHECK_EVERY = 16  # check cancel flag once per N files

# search_text_in_project skips files larger than this. Real source files in
# typical projects don't approach this; the cap exists to avoid pathological
# minified bundles, generated SQL dumps, lockfiles, etc. eating the budget.
MAX_SEARCHABLE_FILE_BYTES = 5 * 1024 * 1024

# Extensions assumed to be text. Files with these extensions skip the
# NUL-byte sniff entirely (saves one open() per file -- the dominant cost
# on a large tree). Anything else falls through to the sniff.
_TEXT_EXTENSIONS = frozenset({
    ".py", ".pyi", ".pyx",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".json", ".jsonc", ".json5",
    ".html", ".htm", ".xhtml", ".xml", ".svg",
    ".css", ".scss", ".sass", ".less",
    ".md", ".markdown", ".rst", ".txt", ".text",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".env",
    ".php", ".phtml", ".twig", ".blade.php",
    ".rb", ".erb",
    ".go", ".rs", ".java", ".kt", ".scala", ".groovy",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx",
    ".cs", ".vb", ".fs", ".fsx",
    ".m", ".mm",
    ".swift",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".sql",
    ".lua", ".pl", ".pm", ".tcl", ".r",
    ".vue", ".svelte", ".astro",
    ".gradle", ".sbt", ".cmake",
    ".dockerfile", ".tf", ".tfvars",
    ".csv", ".tsv", ".log",
})

SCOPE_SELECTORS = [
    "meta.function",
    "meta.method",
    "meta.class",
    "meta.struct",
    "meta.interface",
    "meta.namespace",
    "meta.block",
]


# ============================================================================
# Helpers
# ============================================================================

def _on_main(func, timeout=MAIN_THREAD_TIMEOUT):
    box = {}
    done = threading.Event()

    def wrapper():
        try:
            box["value"] = func()
        except BaseException as e:
            box["error"] = e
        finally:
            done.set()

    sublime.set_timeout(wrapper, 0)
    if not done.wait(timeout):
        raise TimeoutError("Sublime main-thread call timed out")
    if "error" in box:
        raise box["error"]
    return box["value"]


# Sublime's SymbolLocation is 1-indexed; rowcol/text_point are 0-indexed.
# Everything we expose to clients is 1-indexed for cross-tool consistency.

def _rowcol_1based(view, point):
    r, c = view.rowcol(point)
    return r + 1, c + 1


def _text_point_1based(view, row, col):
    return view.text_point(int(row) - 1, int(col) - 1)


def _wait_until_loaded(view, timeout=LOAD_POLL_TIMEOUT):
    """Block (off the main thread) until view.is_loading() returns False, or
    timeout elapses. Returns True if the view loaded in time, False on timeout.

    MUST be called from a background thread, not the main thread: it polls
    is_loading() via _on_main and sleeps between polls. If invoked from the
    main thread, the sleep would prevent ST from ever finishing the load.
    """
    if view is None:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _on_main(lambda v=view: v.is_loading()):
            return True
        time.sleep(0.05)
    return False


def _ensure_view(file_path):
    def init():
        w = sublime.active_window()
        if w is None:
            raise RuntimeError("no active Sublime window")
        view = w.find_open_file(file_path)
        was_open = view is not None
        if not was_open:
            view = w.open_file(file_path, sublime.TRANSIENT)
        return view, was_open
    view, was_open = _on_main(init)
    _wait_until_loaded(view)
    return view, was_open


def _scope_regions_for_body(view):
    """Snapshot every body-scope region in the view.

    One `find_by_selector` call per scope; callers that probe many points in
    the same view should compute this once and pass it to `_find_body_extent`.
    """
    out = []
    for selector in SCOPE_SELECTORS:
        out.extend(view.find_by_selector(selector))
    return out


def _find_body_extent(view, point, _scope_regions=None):
    if _scope_regions is None:
        _scope_regions = _scope_regions_for_body(view)
    candidates = [r for r in _scope_regions if r.begin() <= point <= r.end()]
    if candidates:
        return min(candidates, key=lambda r: r.size())

    size = view.size()
    i = point
    open_pos = -1
    while i < size:
        if view.substr(i) == "{" and not view.match_selector(i, "string, comment"):
            open_pos = i
            break
        i += 1
    if open_pos == -1:
        return None
    depth = 1
    i = open_pos + 1
    while i < size:
        if not view.match_selector(i, "string, comment"):
            c = view.substr(i)
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return sublime.Region(point, i + 1)
        i += 1
    return None


def _location_to_dict(loc):
    d = {
        "path": loc.path,
        "display_name": getattr(loc, "display_name", ""),
        "row": loc.row,
        "col": loc.col,
        "type": getattr(loc, "type", 0),
    }
    kind = getattr(loc, "kind", None)
    if kind:
        d["kind"] = {"id": kind[0], "letter": kind[1], "label": kind[2]}
    return d


def _symbol_region_to_dict(view, sr):
    row, col = _rowcol_1based(view, sr.region.begin())
    d = {
        "name": sr.name,
        "row": row,
        "col": col,
        "type": getattr(sr, "type", 0),
    }
    kind = getattr(sr, "kind", None)
    if kind:
        d["kind"] = {"id": kind[0], "letter": kind[1], "label": kind[2]}
    return d


def _get_search_config():
    """Return a list of (folder_root, file_excludes, folder_excludes) tuples,
    one per project folder.

    Excludes are merged from three layers, matching what Sublime's own
    Goto/Find-in-Files honors:

    1. Per-folder entry in project_data["folders"][i] (`folder_exclude_patterns`,
       `file_exclude_patterns`). This is where users typically scope an exclude
       to a single root -- the layer the previous implementation missed.
    2. Top-level project_data keys (apply to every folder).
    3. Preferences.sublime-settings (global), plus `binary_file_patterns` for
       files.

    Folder roots are resolved to absolute paths so per-folder matching against
    project_data["folders"][i]["path"] (which may be relative to the .sublime-
    project file) lines up with what `window.folders()` reports.
    """
    w = sublime.active_window()
    if w is None:
        return []
    folders = list(w.folders())
    project_data = w.project_data() or {}
    prefs = sublime.load_settings("Preferences.sublime-settings")

    def _list(obj, key):
        v = obj.get(key)
        return list(v) if v else []

    top_file_excludes = _list(project_data, "file_exclude_patterns")
    top_folder_excludes = _list(project_data, "folder_exclude_patterns")
    pref_file_excludes = (
        (prefs.get("file_exclude_patterns") or [])
        + (prefs.get("binary_file_patterns") or [])
        # `index_exclude_patterns` is the indexer's allowlist for "don't
        # bother reading this file's contents" -- exactly what we want
        # for grep, and a well-tuned project usually has it set to skip
        # generated/minified noise.
        + (prefs.get("index_exclude_patterns") or [])
    )
    pref_folder_excludes = prefs.get("folder_exclude_patterns") or []

    # Build a path -> folder-entry map so we can attach per-folder excludes
    # to the corresponding resolved root from window.folders(). project_data
    # paths can be relative; resolve against the .sublime-project's directory.
    project_file = w.project_file_name()
    project_dir = os.path.dirname(project_file) if project_file else None
    entries_by_root = {}
    for entry in (project_data.get("folders") or []):
        p = entry.get("path")
        if not p:
            continue
        if not os.path.isabs(p) and project_dir:
            p = os.path.join(project_dir, p)
        try:
            key = os.path.normcase(os.path.normpath(p))
        except Exception:
            continue
        entries_by_root[key] = entry

    out = []
    for folder in folders:
        try:
            key = os.path.normcase(os.path.normpath(folder))
        except Exception:
            key = folder
        entry = entries_by_root.get(key, {})
        file_excludes = (
            _list(entry, "file_exclude_patterns")
            + top_file_excludes
            + pref_file_excludes
        )
        folder_excludes = (
            _list(entry, "folder_exclude_patterns")
            + top_folder_excludes
            + pref_folder_excludes
        )
        out.append((folder, file_excludes, folder_excludes))
    return out


def _matches_any(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


# Path-glob -> regex. Treats `/` as the path separator and `**` as
# "zero or more path segments". `*` matches within a single segment, `?`
# matches one non-/ character. Anchored at both ends so the whole
# project-relative path must match.
#
# Supported:    foo/*.py        application/**        src/**/*.test.ts
# Not supported: brace expansion ({a,b}), character classes ([...]).
def _path_glob_to_regex(glob_pattern):
    out = ["^"]
    i = 0
    while i < len(glob_pattern):
        c = glob_pattern[i]
        if c == "*":
            if i + 1 < len(glob_pattern) and glob_pattern[i + 1] == "*":
                # `**/` -> any number of leading directories (incl. zero).
                if i + 2 < len(glob_pattern) and glob_pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        if c in r".+^$()|{}\\":
            out.append("\\" + c)
            i += 1
            continue
        out.append(c)
        i += 1
    out.append("$")
    return re.compile("".join(out))


def _dir_excluded(dirpath, dirname, root, folder_excludes):
    """Test a directory against folder_exclude_patterns.

    Sublime's documented behavior matches basenames, but users also write
    path-style patterns scoped to the project tree. We test three forms so
    all common shapes work:

      1. bare basename ("_common")                 -- Sublime's default
      2. relpath from the folder root ("app/_common")
      3. relpath with the folder's basename prepended
         ("admin-webapp/app/_common") -- matches how users naturally write
         the path, including the folder name itself
    """
    if _matches_any(dirname, folder_excludes):
        return True
    full = os.path.join(dirpath, dirname)
    try:
        rel = os.path.relpath(full, root).replace(os.sep, "/")
    except ValueError:
        return False
    if _matches_any(rel, folder_excludes):
        return True
    rel_with_root = os.path.basename(root.rstrip(os.sep) or root) + "/" + rel
    return _matches_any(rel_with_root, folder_excludes)


def _walk(root, file_excludes, folder_excludes):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not _dir_excluded(dirpath, d, root, folder_excludes)
        ]
        for fn in filenames:
            if _matches_any(fn, file_excludes):
                continue
            yield os.path.join(dirpath, fn)


def _load_searchable_bytes(path):
    """Read a file's raw bytes, skipping anything not worth grepping.

    Returns bytes, or None if the file should be skipped (too large,
    unreadable, or appears binary).

    Returning bytes (not str) is deliberate: the search hot path runs a
    compiled regex over the whole buffer first to decide whether the file
    matches at all, and only decodes / splits into lines for the (usually
    small) set of files that actually contain a hit. Decoding every
    surviving file up front was the previous bottleneck on large trees.

    Files with known text extensions skip the NUL-byte sniff entirely;
    unknown extensions are sniffed against the bytes already loaded.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_size > MAX_SEARCHABLE_FILE_BYTES:
        return None
    try:
        with open(path, "rb") as fb:
            data = fb.read()
    except OSError:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext not in _TEXT_EXTENSIONS:
        if b"\x00" in data[:4096]:
            return None
    return data


def _count_lines(buf):
    if not buf:
        return 0
    if buf.endswith("\n"):
        return buf.count("\n")
    return buf.count("\n") + 1


def _read_selections(view):
    """Build the selection-state payload for `view`.

    MUST be called on the main thread (it touches view.sel(), view.substr,
    view.rowcol, view.file_name, etc.). Callers in this module wrap the
    invocation in `_on_main` so all reads happen atomically on a single
    main-thread tick.

    Output is the source of truth for both the top-level
    `get_current_selections` MCP tool and the in-chain pseudo-command of
    the same name dispatched from `run_sublime_command`. If you change
    this shape, both paths change at once -- which is the point.

    Sublime always has at least one region (a bare cursor counts as a
    zero-width selection with empty `text`), so `selections` is always
    non-empty in practice. Multi-cursor: each cursor is its own entry, in
    top-to-bottom order.

    Line-end normalization: if a selection ends at column 0 of a line
    (typical when selecting full lines via shift-down or triple-click),
    `stop_line_number` reports the PREVIOUS line -- the last line with
    any character actually highlighted. So selecting visible content of
    lines 3-5 always returns stop_line_number=5.

    Untitled buffers: `project_file` falls back to a display name like
    `<untitled 5>` and CANNOT be passed to file-oriented tools.
    """
    regions = list(view.sel())
    if not regions:
        raise RuntimeError("active view has no selection regions")

    name = view.file_name() or view.name() or "<untitled {}>".format(view.id())
    project_file_total_lines = _count_lines(view.substr(sublime.Region(0, view.size())))

    def region_to_dict(r):
        start_row, _ = view.rowcol(r.begin())
        end_row, end_col = view.rowcol(r.end())
        # If a non-empty selection ends at column 0, the visible last
        # highlighted line is the previous one.
        if not r.empty() and end_col == 0 and end_row > 0:
            end_row -= 1
        return {
            "start_line_number": start_row + 1,
            "stop_line_number": end_row + 1,
            "text": view.substr(r),
        }

    return {
        "project_file": name,
        "project_file_total_lines": project_file_total_lines,
        "selections": [region_to_dict(r) for r in regions],
    }


def _symbol_locations(symbol, source, type_):
    def go():
        w = sublime.active_window()
        if w is None:
            return []
        locs = w.symbol_locations(symbol, source, type_)
        return [_location_to_dict(l) for l in locs]
    return _on_main(go)


# ============================================================================
# 1. Project structure
# ============================================================================

def list_folders_in_project() -> Dict[str, Any]:
    """List the root folders of the most-recently-active Sublime Text window's project.

    Returns: {"folders": [absolute paths], "project_file": path or null}.
    """
    def go():
        w = sublime.active_window()
        if w is None:
            return {"folders": [], "project_file": None}
        return {"folders": list(w.folders()), "project_file": w.project_file_name()}
    return _on_main(go)


def find_files_in_project(pattern: str, regex: bool = False,
                          glob: Optional[str] = None,
                          max_results: int = FILENAME_RESULT_CAP
                          ) -> Dict[str, Any]:
    """Search for files across the active project's folders.

    `pattern` is matched against each file's BASENAME -- fnmatch-style glob
    by default (e.g. "*.py", "test_*.py"); set regex=true to treat it as a
    Python regex (re.search semantics). The optional `glob` arg is a PATH
    glob restricting which directories to search, matched against each
    file's project-relative path (e.g. "application/**", "src/**/*.test.ts").
    Use "**" for recursive directory matching; "*" matches within a single
    segment.

    Honors project + global file/folder exclude patterns. Capped at
    `max_results` (default 1000); the returned `truncated` flag is true if
    the cap was hit.
    """
    folder_configs = _on_main(_get_search_config)
    if regex:
        rx = re.compile(pattern)
        def match_basename(name): return rx.search(name) is not None
    else:
        def match_basename(name): return fnmatch.fnmatchcase(name, pattern)

    if glob:
        path_rx = _path_glob_to_regex(glob)
        def match_path(rel): return path_rx.match(rel) is not None
    else:
        def match_path(rel): return True

    results = []
    truncated = False
    checked = 0
    for folder, file_excludes, folder_excludes in folder_configs:
        for path in _walk(folder, file_excludes, folder_excludes):
            checked += 1
            if checked % SEARCH_CANCEL_CHECK_EVERY == 0 and is_cancelled():
                return {"files": results, "count": len(results),
                        "truncated": truncated}
            base = os.path.basename(path)
            if not match_basename(base):
                continue
            # Project-relative path, normalized to forward slashes for
            # cross-platform glob matching.
            rel = os.path.relpath(path, folder).replace(os.sep, "/")
            if not match_path(rel):
                continue
            results.append(path)
            if len(results) >= max_results:
                truncated = True
                return {"files": results, "count": len(results),
                        "truncated": truncated}
    return {"files": results, "count": len(results), "truncated": truncated}


# ============================================================================
# 2. File content
# ============================================================================

def get_file_content(project_file: str, start_line_number: int,
                     stop_line_number: Optional[int] = None) -> Dict[str, Any]:
    """Return a slice of file contents by line number.

    Reads `project_file` from disk and returns lines from `start_line_number` through `stop_line_number` INCLUSIVE. Both bounds are 1-indexed (matching every other row value in this server). Asking for start_line_number=1, stop_line_number=5 returns exactly 5 lines.

    If `stop_line_number` is omitted or null, content from `start_line_number` through the end of the file .

    Returns: {path, start_line, end_line, total_lines, text}. `text` is a `\\n`-delimited string where the final line has no trailing newline.
    """
    if start_line_number < 1:
        raise ValueError("start_line_number must be >= 1 (1-indexed)")
    if stop_line_number is not None and int(stop_line_number) < int(start_line_number):
        raise ValueError("stop_line_number must be >= start_line_number")

    sliced: List[str] = []
    total = 0
    stop = int(stop_line_number) if stop_line_number is not None else None
    start = int(start_line_number)

    with open(project_file, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            total = lineno
            if lineno < start:
                continue
            if stop is not None and lineno > stop:
                # keep counting to report accurate total_lines
                continue
            sliced.append(line.rstrip("\n").rstrip("\r"))

    if start > total:
        end_line = start - 1
    else:
        end_line = start + len(sliced) - 1

    return {
        "path": project_file,
        "start_line": start,
        "end_line": end_line,
        "total_lines": total,
        "text": "\n".join(sliced),
    }


def set_file_content(project_file: str, content: str,
                     start_line_number: int,
                     stop_line_number: int,
                     save: bool = True) -> Dict[str, Any]:
    """This function replaces a contiguous range of lines within a specified file.

    *   **Indexing:** Both `start_line_number` and `stop_line_number` are **inclusive** and are 1-indexed.
    *   **Concurrency Safety:** When making multiple edits to the same file, always apply changes starting from the **highest line numbers first**. This prevents earlier edits from invalidating line numbers for subsequent operations.

    **Arguments:**
    *   `project_file` (STRING): The absolute path to the file being edited.
    *   `content` (STRING): The exact text you want to substitute for the selected lines. **Do not add automatic newlines.** If you need a newline at the end of your replacement block, include it in `content`. Passing an empty string (`""`) deletes all lines in the range.
    *   `start_line_number` (INTEGER): The first line number to replace/delete (inclusive). Must be $\\ge 1$.
    *   `stop_line_number` (INTEGER): The last line number to replace/delete (inclusive).
    *   `save` (BOOLEAN, default: `True`): If set to `False`, the edit is performed in an in-memory buffer and is **not written back to disk** until explicitly saved.

    **Returns:**
    A dictionary containing status information: `{ok, saved, dirty, replaced_start_line, replaced_stop_line, previous_total_lines, new_total_lines}`.
    """
    start = int(start_line_number)
    stop = int(stop_line_number) if stop_line_number is not None else None

    if start < 1:
        raise ValueError("start_line_number must be >= 1 (1-indexed)")
    if stop is not None and stop < start:
        raise ValueError("stop_line_number must be >= start_line_number")

    view, was_open = _ensure_view(project_file)

    def apply():
        buf = view.substr(sublime.Region(0, view.size()))
        total_lines = _count_lines(buf)

        # Allow start = total_lines + 1 as "append at EOF". Empty file
        # (total_lines = 0) accepts start = 1 only.
        max_start = total_lines + 1 if total_lines > 0 else 1
        if start > max_start:
            raise ValueError(
                "start_line_number {} exceeds file length ({} lines; max start is {})".format(
                    start, total_lines, max_start))

        # Find character offset where line N begins. Lines are 1-indexed.
        # For an empty file, offset is always 0.
        def line_start_offset(line_num):
            if total_lines == 0 or line_num <= 1:
                return 0
            seen_newlines = 0
            for i, c in enumerate(buf):
                if c == "\n":
                    seen_newlines += 1
                    if seen_newlines == line_num - 1:
                        return i + 1
            return len(buf)

        # End-of-line offset: position just before the line's terminator,
        # or end-of-buffer if the line has no terminator.
        def line_end_offset(line_num):
            if total_lines == 0:
                return 0
            line_start = line_start_offset(line_num)
            nl = buf.find("\n", line_start)
            return nl if nl != -1 else len(buf)

        start_pt = line_start_offset(start)
        if stop is None:
            end_pt = len(buf)
            actual_stop = total_lines
        else:
            actual_stop = min(stop, total_lines) if total_lines > 0 else 0
            if actual_stop == 0:
                end_pt = 0
            else:
                end_pt = line_end_offset(actual_stop)

        # Deletion mode: empty content string means fully remove the lines
        # including their trailing newlines, leaving no blank lines behind.
        if content == "":
            end_line_start = line_start_offset(actual_stop)
            nl = buf.find("\n", end_line_start)
            end_pt = nl + 1 if nl != -1 else len(buf)

        view.run_command("ai_bridge_apply_edits", {
            "edits": [{"start": start_pt, "end": end_pt, "text": content}]
        })

        effective_save = bool(save) or not was_open
        if effective_save:
            view.run_command("save")
        dirty = view.is_dirty()
        if not was_open and effective_save:
            view.close()

        new_buf = view.substr(sublime.Region(0, view.size()))
        return {
            "ok": True,
            "saved": effective_save,
            "dirty": dirty,
            "replaced_start_line": start,
            "replaced_stop_line": actual_stop if stop is not None or total_lines > 0 else (start - 1),
            "previous_total_lines": total_lines,
            "new_total_lines": _count_lines(new_buf),
        }

    return _on_main(apply)


# ============================================================================
# 3. Function / symbol
#
# "Function" in these tool names is shorthand for any indexed symbol --
# functions, methods, classes, namespaces, structs, etc. Sublime's symbol
# index is the source of truth and includes all of those. The naming is
# kept user-friendly even though the underlying coverage is broader.
# ============================================================================

def find_function_in_project(symbol: str) -> List[Dict[str, Any]]:
    """Find DECLARATION sites for functions, methods, classes or namespaces across the project.

    NAMING NOTE (applies to find_function_content / get_function_content /
    set_function_content / list_functions_in_file tool): "function" is shorthand for any
    indexed symbol -- functions, methods, classes, namespaces, etc.

    Returns the row/path you should pass to `get_function_content` /
    `set_function_content`. For invocation sites instead of definitions, use
    `find_function_usages_in_project`.

    Returns: [{path, display_name, row, col, type, kind}].
    """
    return _symbol_locations(
        symbol, sublime.SYMBOL_SOURCE_ANY, sublime.SYMBOL_TYPE_DEFINITION,
    )


def find_function_usages_in_project(symbol: str) -> List[Dict[str, Any]]:
    """Find CALL SITES / reference locations for `symbol` -- where it is
    INVOKED, not where it is defined. Use for "who calls X?".

    CRITICAL: Do NOT pass these rows to `get_function_content` or
    `set_function_content` -- they need a DEFINITION row from
    `find_function_in_project`, otherwise you'll extract the wrong function
    (the one that contains the call).

    Returns: same shape as `find_function_in_project`.
    """
    return _symbol_locations(
        symbol, sublime.SYMBOL_SOURCE_ANY, sublime.SYMBOL_TYPE_REFERENCE,
    )


def list_functions_in_file(file_path: str) -> Dict[str, Any]:
    """List all top-level symbols declared in `file_path`. The `kind` field on
    each entry distinguishes functions / classes / methods / etc.

    Returns: {path, symbols: [{name, row, col, type, kind}]}.
    """
    view, was_open = _ensure_view(file_path)

    def extract():
        regions = view.symbol_regions()
        result = [_symbol_region_to_dict(view, sr) for sr in regions]
        if not was_open:
            view.close()
        return {"path": file_path, "symbols": result}

    return _on_main(extract)


def get_function_content(file_path: str, name: Optional[str] = None,
                         row: Optional[int] = None) -> Dict[str, Any]:
    """Return the full source-code/contents of a function/definition in `file_path`, signature through closing brace. The returned `text` is a drop-in replacement target for `set_function_content`.

    Provide AT LEAST ONE of `name` or `row`:
      - `name` only: locate the symbol by name in this file (most common usage; pair with `find_function_in_project` to discover the file).
      - `row` only: locate the function whose body BRACKETS the given line. Useful when you have a hit from a text search (e.g. `{path, line}` from `search_text_in_project`) and want the enclosing function's content without a separate symbol lookup.
      - both: filter by name, then require `row` to fall within the body. Backward-compatible with passing the symbol's declaration row.

    The `row` parameter is now interpreted as ANY line inside the function body (not strictly the declaration line). Passing the declaration line still matches because it lies within the body extent.

    When multiple candidates qualify under `row`, the INNERMOST (smallest body extent) wins -- so a line inside a nested method returns the method, not its enclosing class.

    Returns: {matches: [...], candidates_in_file?: [...], found_elsewhere?: [...]}.

    Self-correction when matches is empty (only when `name` is provided):
      - candidates_in_file: same name found on a different row
      - found_elsewhere: name found in alternative file(s)

    Python Support Enhancement:
      - For files ending in `.py`, this function attempts a line-based extraction fallback if symbol indexing fails, prioritizing the definition matching `name` or finding the first `def` statement near `row`.
    """
    if name is None and row is None:
        raise ValueError("Provide at least one of `name` or `row`")

    # Normalize once for path comparisons below.
    try:
        norm_self = os.path.normcase(os.path.abspath(file_path))
    except OSError:
        norm_self = file_path

    is_python = file_path.lower().endswith('.py')

    # ---- Precheck via the project-wide symbol index --------------------
    if name is not None and not is_python:
        def precheck():
            w = sublime.active_window()
            if w is None:
                return None
            try:
                return w.symbol_locations(
                    name,
                    sublime.SYMBOL_SOURCE_ANY,
                    sublime.SYMBOL_TYPE_DEFINITION,
                )
            except Exception:
                return None

        locs = _on_main(precheck)
        if locs is not None:
            in_file_locs = []
            elsewhere_locs = []
            for loc in locs:
                try:
                    norm_loc = os.path.normcase(os.path.abspath(loc.path))
                except OSError:
                    norm_loc = loc.path
                if norm_loc == norm_self:
                    in_file_locs.append(loc)
                else:
                    elsewhere_locs.append(loc)
            if not in_file_locs:
                # Symbol isn't indexed in `file_path`. Don't open the view
                result = {"matches": []}
                if elsewhere_locs:
                    result["found_elsewhere"] = [
                        _location_to_dict(l) for l in elsewhere_locs
                    ]
                return result

    # ---- Symbol IS (or might be) in this file: open and extract body ---

    view, was_open = _ensure_view(file_path)

    def go():
        # Resolve `row` to a character point at the line's start. Used
        # for "extent contains row" matching. None means no row filter.
        target_point = None
        if row is not None:
            try:
                target_point = _text_point_1based(view, int(row), 1)
            except Exception:
                target_point = None

        # Collect all qualifying (extent_size, entry) tuples, then trim.
        # Python: skip the brace/scope-based extent path. Sublime's Python
        # syntax matches `meta.function.python` on just the signature line,
        # which would otherwise yield a one-line "body" extent and short-
        # circuit the indentation-based extraction below.
        candidates = []
        sym_iter = () if is_python else view.symbol_regions()
        # Snapshot body-scope regions once for the whole loop; without this,
        # _find_body_extent re-runs 7 find_by_selector calls per symbol.
        scope_regions = None if is_python else _scope_regions_for_body(view)
        for sr in sym_iter:
            if name is not None and sr.name != name:
                continue
            # Cheap pre-filter when `row` is given: a symbol declared AFTER
            # the target point cannot have a body that brackets it (bodies
            # extend forward from the declaration). Skipping these saves
            # an expensive `_find_body_extent` per symbol on large files.
            if target_point is not None and sr.region.begin() > target_point:
                continue
            sym_row, sym_col = _rowcol_1based(view, sr.region.begin())
            extent = _find_body_extent(view, sr.region.begin(),
                                       _scope_regions=scope_regions)
            if extent is None:
                continue
            # Row filter: extent must bracket the requested line.
            if target_point is not None:
                if not (extent.begin() <= target_point <= extent.end()):
                    continue
            sr_, sc_ = _rowcol_1based(view, extent.begin())
            er, ec = _rowcol_1based(view, extent.end())
            entry = {
                "name": sr.name,
                "start_row": sr_,
                "start_col": sc_,
                "end_row": er,
                "end_col": ec,
                "name_row": sym_row,
                "name_col": sym_col,
                "text": view.substr(extent),
            }
            kind = getattr(sr, "kind", None)
            if kind:
                entry["kind"] = {"id": kind[0], "letter": kind[1], "label": kind[2]}
            candidates.append((extent.size(), entry))

        # When `row` is given, multiple symbol extents can contain the
        # same point (a method nested inside a class, etc.). Return the
        # innermost -- smallest extent. Without `row`, return all
        # overloads (preserves existing behavior for name-only callers).
        if target_point is not None and candidates:
            min_size = min(c[0] for c in candidates)
            matches = [c[1] for c in candidates if c[0] == min_size]
        else:
            matches = [c[1] for c in candidates]

        result = {"matches": matches}

        # candidates_in_file / found_elsewhere are name-driven fallbacks.
        # They don't apply on the row-only path (no name to search for).
        if not matches and name is not None:
            in_file = []
            for sr in view.symbol_regions():
                if sr.name == name:
                    r, c = _rowcol_1based(view, sr.region.begin())
                    in_file.append({"name": sr.name, "row": r, "col": c})
            if in_file:
                result["candidates_in_file"] = in_file
            else:
                # Precheck said the symbol is indexed in this file, so we
                # don't normally land here. If we do (stale index vs
                # buffer), fall back to a fresh project-wide lookup so we
                # can still emit found_elsewhere rather than an empty
                # result.
                w = sublime.active_window()
                if w is not None:
                    elsewhere = []
                    try:
                        fresh = w.symbol_locations(
                            name,
                            sublime.SYMBOL_SOURCE_ANY,
                            sublime.SYMBOL_TYPE_DEFINITION,
                        )
                        for loc in fresh:
                            try:
                                norm_loc = os.path.normcase(os.path.abspath(loc.path))
                            except OSError:
                                norm_loc = loc.path
                            if norm_loc == norm_self:
                                continue
                            elsewhere.append(_location_to_dict(loc))
                    except Exception:
                        pass
                    if elsewhere:
                        result["found_elsewhere"] = elsewhere

        # --- Python Specific Fallback Logic ---
        # Python has no braces, so _find_body_extent comes up empty. Use
        # indentation: find the `def`, then take following lines that are
        # more indented (blanks/comments don't terminate).
        if is_python and not matches:
            DEF_RE = re.compile(r'^(\s*)(?:async\s+)?def\s+(\w+)\s*\(')
            line_regions = view.lines(sublime.Region(0, view.size()))
            texts = [view.substr(r) for r in line_regions]
            hits = [(i, m) for i, t in enumerate(texts)
                    for m in [DEF_RE.match(t)] if m and (name is None or m.group(2) == name)]

            if row is not None:
                hits = [h for h in hits if h[0] < int(row)][-1:]  # nearest preceding def

            py_matches = []
            for di, m in hits:
                def_indent = len(m.group(1))
                end = di
                for j in range(di + 1, len(texts)):
                    s = texts[j].lstrip()
                    if not s or s.startswith('#'):
                        continue
                    if len(texts[j]) - len(s) <= def_indent:
                        break
                    end = j
                sr_, sc_ = _rowcol_1based(view, line_regions[di].begin())
                er, ec = _rowcol_1based(view, line_regions[end].end())
                py_matches.append({
                    "name": m.group(2),
                    "start_row": sr_, "start_col": sc_,
                    "end_row": er, "end_col": ec,
                    "name_row": sr_, "name_col": sc_ + m.start(2),
                    "text": "\n".join(texts[di:end + 1]),
                })

            if py_matches and row is not None and len(py_matches) > 1:
                py_matches = [min(py_matches, key=lambda x: x["end_row"] - x["start_row"])]
            if py_matches:
                result["matches"] = py_matches

        if not was_open:
            view.close()
        return result

    return _on_main(go)


def set_function_content(file_path: str, name: str, new_text: str,
                         save: bool = True, row: Optional[int] = None) -> Dict[str, Any]:
    """Replace definition `name` in `file_path` with `new_text`.

    `new_text` is the COMPLETE replacement (signature + body + closing
    brace), not just the body. Single ST undo entry.

    If `name` has multiple definitions in the file, first match wins -- pass
    `row` (a DEFINITION row from `find_function_in_project`, not a reference
    row) to target a specific one.

    Save semantics: `save=true` writes to disk. If the file wasn't already
    open in Sublime, save is forced regardless to avoid losing the edit when
    the transient view closes.

    Python files use indentation-based body detection: the body extends from
    the `def` line through the last line indented deeper than the `def`
    itself (blank lines and comments don't terminate it).
    """

    view, was_open = _ensure_view(file_path)
    is_python = file_path.lower().endswith('.py')

    def find_target():
        if is_python:
            DEF_RE = re.compile(r'^(\s*)(?:async\s+)?def\s+(\w+)\s*\(')
            line_regions = view.lines(sublime.Region(0, view.size()))
            texts = [view.substr(r) for r in line_regions]
            for di, t in enumerate(texts):
                m = DEF_RE.match(t)
                if not m or m.group(2) != name:
                    continue
                if row is not None and (di + 1) != int(row):
                    continue
                def_indent = len(m.group(1))
                end = di
                for j in range(di + 1, len(texts)):
                    s = texts[j].lstrip()
                    if not s or s.startswith('#'):
                        continue
                    if len(texts[j]) - len(s) <= def_indent:
                        break
                    end = j
                return sublime.Region(line_regions[di].begin(), line_regions[end].end())
            return None

        for sr in view.symbol_regions():
            if sr.name != name:
                continue
            sym_row, _ = _rowcol_1based(view, sr.region.begin())
            if row is not None and sym_row != int(row):
                continue
            extent = _find_body_extent(view, sr.region.begin())
            if extent is not None:
                return extent
        return None

    extent = _on_main(find_target)
    if extent is None:
        if not was_open:
            _on_main(lambda: view.close())
        raise ValueError(
            "definition '{}' not found (or no body extent) in {}".format(name, file_path)
        )

    start_pt, end_pt = extent.begin(), extent.end()
    pre_start = list(_rowcol_1based(view, start_pt))
    pre_end = list(_rowcol_1based(view, end_pt))

    def apply():
        view.run_command("ai_bridge_apply_edits", {
            "edits": [{"start": start_pt, "end": end_pt, "text": new_text}]
        })
        effective_save = bool(save) or not was_open
        if effective_save:
            view.run_command("save")
        dirty = view.is_dirty()
        if not was_open and effective_save:
            view.close()
        return {
            "ok": True,
            "saved": effective_save,
            "dirty": dirty,
            "replaced_start": pre_start,
            "replaced_end": pre_end,
        }

    return _on_main(apply)


# ============================================================================
# 4. Text search
# ============================================================================

def search_text_in_project(pattern: str, regex: bool = False, case_sensitive: bool = False,
                           glob: Optional[str] = None,
                           max_results: int = 200) -> List[Dict[str, Any]]:
    """Grep file contents across the active project's folders (on disk).

    pattern: literal substring by default; set regex=true for Python `re` regex.
    Optional `glob` filters by basename (fnmatch) (e.g. "*.php"). Skips files
    matching project + global exclude patterns (including
    `index_exclude_patterns`), files larger than MAX_SEARCHABLE_FILE_BYTES,
    and files that appear binary (NUL bytes in the first 4KB, only sniffed
    for unknown extensions). Returns up to `max_results` entries:
    {path, line, text}.

    For matching against unsaved buffer state, use `search_text_in_open_files`.
    """
    folder_configs = _on_main(_get_search_config)
    flags = 0 if case_sensitive else re.IGNORECASE
    # Compile against bytes so the whole-file pre-filter below is a single
    # C-level scan, no decode. Same regex (recompiled for str) is used to
    # walk lines once we know the file actually contains a hit.
    pattern_b = pattern.encode("utf-8") if regex else re.escape(pattern.encode("utf-8"))
    rx_bytes = re.compile(pattern_b, flags)
    rx_str = re.compile(pattern if regex else re.escape(pattern), flags)
    glob_match = (lambda n: fnmatch.fnmatchcase(n, glob)) if glob else (lambda n: True)

    results: List[Dict[str, Any]] = []
    checked = 0
    for folder, file_excludes, folder_excludes in folder_configs:
        for path in _walk(folder, file_excludes, folder_excludes):
            checked += 1
            if checked % SEARCH_CANCEL_CHECK_EVERY == 0 and is_cancelled():
                return results
            if not glob_match(os.path.basename(path)):
                continue
            data = _load_searchable_bytes(path)
            if data is None:
                continue
            # Whole-file pre-filter: if the pattern doesn't appear anywhere
            # in the bytes, skip the decode + line walk entirely. This is
            # the dominant speedup on large trees -- most files don't match,
            # and a single bytes-level regex scan is far cheaper than
            # splitlines() + per-line regex.
            if rx_bytes.search(data) is None:
                continue
            try:
                text = data.decode("utf-8", errors="replace")
                for lineno, line in enumerate(text.splitlines(), 1):
                    if rx_str.search(line):
                        results.append({
                            "path": path,
                            "line": lineno,
                            "text": line[:500],
                        })
                        if len(results) >= max_results:
                            return results
                    # Mid-file cancel check: catastrophic regex on a single
                    # large file is the worst case; the per-file check above
                    # alone wouldn't catch it.
                    if lineno % 1024 == 0 and is_cancelled():
                        return results
            except OSError:
                continue
    return results


def search_text_in_open_files(pattern: str, regex: bool = True, case_sensitive: bool = False,
                              max_results: int = 200) -> List[Dict[str, Any]]:
    """Grep across the contents of files currently open in the active Sublime window.

    Searches the in-memory BUFFER text including any UNSAVED edits, so this
    sees changes the user has made but not yet written to disk. Untitled
    buffers are included; their `path` field follows Sublime's symbol-API
    convention (e.g. `<untitled 5>`) and CANNOT be passed to file-oriented
    tools like `get_function_content` or `set_function_content`.

    Use this when you specifically want results from what the user has open
    and may have edited locally, NOT what's on disk. For project-wide grep
    against on-disk files, use `search_text_in_project`.

    pattern: Python `re` regex by default; set regex=false for literal substring.
    Returns up to `max_results` entries: {path, line, text}. Result shape is
    identical to `search_text_in_project`.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    rx = re.compile(pattern if regex else re.escape(pattern), flags)

    def snapshot():
        w = sublime.active_window()
        if w is None:
            return []
        out = []
        for view in w.views():
            name = view.file_name() or view.name() or "<untitled {}>".format(view.id())
            content = view.substr(sublime.Region(0, view.size()))
            out.append((name, content))
        return out

    snapshots = _on_main(snapshot)

    results: List[Dict[str, Any]] = []
    for name, content in snapshots:
        if is_cancelled():
            return results
        for lineno, line in enumerate(content.splitlines(), 1):
            if rx.search(line):
                results.append({
                    "path": name,
                    "line": lineno,
                    "text": line[:500],
                })
                if len(results) >= max_results:
                    return results
            if lineno % 1024 == 0 and is_cancelled():
                return results
    return results


# ============================================================================
# 5. Editor state
# ============================================================================

def get_current_selections() -> Dict[str, Any]:
    """Return the current text selections in the active Sublime view.

    Sublime always has at least one region (a bare cursor counts as a
    zero-width selection with empty `text`), so `selections` is always
    non-empty. Multi-cursor: each cursor is its own entry, in top-to-bottom
    order.

    Line-end normalization: if a selection ends at column 0 of a line
    (typical when selecting full lines via shift-down or triple-click),
    `stop_line_number` reports the PREVIOUS line -- the last line with any
    character actually highlighted. So selecting visible content of lines
    3-5 always returns stop_line_number=5.

    Untitled buffers: `project_file` is a display name like `<untitled 5>`
    and CANNOT be passed to file-oriented tools.

    Returns: {project_file, project_file_total_lines, selections: [{start_line_number,
    stop_line_number, text}, ...]}.

    Also available as a chain pseudo-command inside `run_sublime_command`
    under the same name. Both paths share the `_read_selections` helper,
    so the output shape and edge-case behavior are identical.
    """
    def go():
        w = sublime.active_window()
        if w is None:
            raise RuntimeError("no active Sublime window")
        view = w.active_view()
        if view is None:
            raise RuntimeError("no active view")
        return _read_selections(view)

    return _on_main(go)


def set_current_selection_content(content: str) -> Dict[str, Any]:
    """Replace the FIRST selection in the active Sublime view with `content`.

    Symmetric counterpart to `get_current_selections` -- pair them when an AI
    needs to read the user's highlighted text, transform it, and write the
    result back without bothering with file paths or line numbers.

    Multi-cursor: only the first (top-most) selection is touched; any other
    cursors are left in place. If the first region is a zero-width cursor,
    `content` is INSERTED at that point. The edit is one Sublime undo entry.
    If after the edit the buffer is dirty AND has a real on-disk file path,
    the file is saved automatically; otherwise the edit is left unsaved.

    Untitled buffers are supported -- `project_file` will be a display name
    like `<untitled 5>`.

    Returns: {ok, project_file, replaced_start_line, replaced_stop_line,
    inserted_length, dirty, saved}. The reported line range is the PRE-edit
    range of the selection that was replaced (with the same column-0
    normalization as `get_current_selections`). `saved` is True if the file
    was written to disk during this call.
    """
    def go():
        w = sublime.active_window()
        if w is None:
            raise RuntimeError("no active Sublime window")
        view = w.active_view()
        if view is None:
            raise RuntimeError("no active view")
        regions = list(view.sel())
        if not regions:
            raise RuntimeError("active view has no selection regions")

        r = regions[0]
        start_pt, end_pt = r.begin(), r.end()
        start_row, _ = view.rowcol(start_pt)
        end_row, end_col = view.rowcol(end_pt)
        # Match get_current_selections' line-end normalization so the
        # reported range matches what a caller would have just read.
        if not r.empty() and end_col == 0 and end_row > 0:
            end_row -= 1

        name = view.file_name() or view.name() or "<untitled {}>".format(view.id())

        view.run_command("ai_bridge_apply_edits", {
            "edits": [{"start": start_pt, "end": end_pt, "text": content}]
        })

        # Persist to disk only if the buffer is dirty AND backed by a real
        # on-disk file. Untitled buffers (file_name() is None) and views
        # whose path no longer exists are left dirty for the user to handle.
        saved = False
        if view.is_dirty():
            fpath = view.file_name()
            if fpath and os.path.isfile(fpath):
                view.run_command("save")
                saved = True

        return {
            "ok": True,
            "project_file": name,
            "replaced_start_line": start_row + 1,
            "replaced_stop_line": end_row + 1,
            "inserted_length": len(content),
            "dirty": view.is_dirty(),
            "saved": saved,
        }

    return _on_main(go)


def run_sublime_command(commands: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run one or more Sublime Text commands, each with its own scope.

    Always takes an array of commands. Single commands are just an array
    of length one. Each command specifies its own scope ("view" or "window")
    so a single call can mix scopes — e.g. open_file (window) followed by
    expand_selection (view) — without the chain-scope conflicts that occur
    when using Sublime's built-in `chain` command across mixed scopes.

    Reference: https://docs.sublimetext.io/reference/commands.html

    Each command in the list is a dict with:
      - command (str):     The Sublime command name (required)
      - args (dict):       Arguments for the command (optional, defaults to {})
      - scope (str):       "view" or "window" (optional, defaults to "window")
      - file_path (str):   Target file for view-scoped commands (optional;
                           if omitted on a view command, uses active view)

    Examples -- single window command:
      run_sublime_command([
          {"command": "new_window"}
      ])

    Examples -- single view command on a specific file:
      run_sublime_command([
          {"command": "goto_line", "args": {"line": 1688},
           "scope": "view", "file_path": "/path/to/file"}
      ])

    Examples -- mixed-scope chain (the case that motivated this):
      run_sublime_command([
          {"command": "open_file", "args": {"file": "/path/to/file"},
           "scope": "window"},
          {"command": "goto_line", "args": {"line": 113},
           "scope": "window"},
          {"command": "expand_selection", "args": {"to": "brackets"},
           "scope": "view"},
          {"command": "show_at_center", "scope": "view"}
      ])

    Examples -- view command on the currently active view (no file_path):
      run_sublime_command([
          {"command": "expand_selection", "args": {"to": "indentation"},
           "scope": "view"}
      ])

    Examples -- view command on the currently active view (no file_path):
      run_sublime_command([
          {"command": "open_file",
           "args": {"file": "/path/to/file"},
           "scope": "window"},
          {"command": "goto_line",
           "args": {"line": 113},
           "scope": "window"},
          {"command": "expand_selection_to_paragraph",
           "scope": "view"}
      ])

    Examples -- inline selection capture (read -> mutate -> read in one call).
    Sublime's command system has no return-value channel (run_command always
    returns None), so we expose `get_current_selections` as a chain pseudo-
    command that the dispatcher handles itself. Each capture step's payload
    appears under `result` in that step's entry of the returned `results`
    array, with the same shape as the top-level get_current_selections tool:
      run_sublime_command([
          {"command": "open_file", "args": {"file": "/path/to/file"},
           "scope": "window"},
          {"command": "goto_line", "args": {"line": 113}, "scope": "window"},
          {"command": "expand_selection_to_paragraph", "scope": "view"},
          {"command": "get_current_selections", "scope": "view"},
          {"command": "expand_selection", "args": {"to": "scope"},
           "scope": "view"},
          {"command": "get_current_selections", "scope": "view"}
      ])

    Pseudo-commands (handled inside the dispatcher, never forwarded to ST):
      - get_current_selections: read the active view's (or `file_path`'s)
        selection state into this step's `result` field. Identical output
        shape and edge-case behavior to the top-level MCP tool of the same
        name -- both call _read_selections under the hood.

    Notes:
      - Selection-based commands (sort_lines, toggle_comment, upper_case,
        duplicate_line, swap_line_up/down, join_lines) operate on whatever
        the user currently has selected. Pair with get_current_selections
        (top-level tool, or inline as a chain pseudo-command) to know what
        they'll affect.
      - Plugin-installed commands work too (Package Control, formatters,
        LSP). Discover names via Tools > Developer > Command Logger.
      - Some commands open interactive panels (show_panel for find/replace,
        prompt_open_file) — useful for "show the user something" but
        won't return programmatic data to the caller.
      - Commands run sequentially on the main thread. If one fails, the
        rest still attempt to run; per-command success is in `results`.

    Returns:
        {
          "ok": bool,             # True if all commands succeeded
          "results": [            # one entry per input command, in order
            {
              "command": str,
              "scope": "view" | "window",
              "ok": bool,
              "dirty": bool,      # only present for view-scoped commands
              "file": str | None, # only present for view-scoped commands
              "result": dict,     # only present for pseudo-commands that
                                  # produce a payload (e.g. get_current_selections)
              "error": str        # only present if ok is False
            },
            ...
          ]
        }
    """
    if not commands or not isinstance(commands, list):
        raise ValueError("commands must be a non-empty list")

    # Dispatch each command via its own _on_main hop, with off-main waits
    # between hops. We CANNOT run the whole chain inside one _on_main call:
    # that would hold the main thread, and any open_file in the chain would
    # never get to finish loading — subsequent commands would race against
    # an unloaded view. See _wait_until_loaded for the same threading rule.
    if _on_main(lambda: sublime.active_window()) is None:
        raise RuntimeError("no active Sublime window")

    results = []
    all_ok = True

    for cmd in commands:
        cmd_name = cmd.get("command")
        cmd_args = cmd.get("args") or {}
        cmd_scope = cmd.get("scope", "window")
        cmd_file = cmd.get("file_path")

        if not cmd_name:
            results.append({
                "command": None,
                "scope": cmd_scope,
                "ok": False,
                "error": "missing 'command' key",
            })
            all_ok = False
            continue

        # Pseudo-commands: handled by the dispatcher itself, never forwarded
        # to Sublime. Documented under "Pseudo-commands:" in the docstring
        # above. Add new ones here, keeping the result envelope consistent
        # with regular chain steps so callers don't have to special-case.
        if cmd_name == "get_current_selections":
            try:
                if cmd_file:
                    view, _ = _ensure_view(cmd_file)
                else:
                    view = _on_main(lambda: (
                        sublime.active_window().active_view()
                        if sublime.active_window() else None))
                    if view is None:
                        raise RuntimeError("no active view")
                payload = _on_main(lambda v=view: _read_selections(v))
                results.append({
                    "command": cmd_name,
                    "scope": cmd_scope,
                    "ok": True,
                    "result": payload,
                })
            except Exception as e:
                results.append({
                    "command": cmd_name,
                    "scope": cmd_scope,
                    "ok": False,
                    "error": str(e),
                })
                all_ok = False
            continue

        try:
            if cmd_scope == "view":
                # Resolve the target view. _ensure_view already waits for
                # loading; for the no-file_path path we just grab whatever's
                # currently active (which is whatever a preceding open_file
                # left us on, since we waited for it below).
                if cmd_file:
                    view, _ = _ensure_view(cmd_file)
                else:
                    view = _on_main(lambda: (
                        sublime.active_window().active_view()
                        if sublime.active_window() else None))
                    if view is None:
                        raise RuntimeError("no active view")

                _on_main(lambda v=view, n=cmd_name, a=cmd_args:
                         v.run_command(n, a))
                dirty, fname = _on_main(lambda v=view:
                                        (v.is_dirty(), v.file_name()))
                results.append({
                    "command": cmd_name,
                    "scope": "view",
                    "ok": True,
                    "dirty": dirty,
                    "file": fname,
                })
            else:
                def _run_window(n=cmd_name, a=cmd_args):
                    w = sublime.active_window()
                    if w is None:
                        raise RuntimeError("no active Sublime window")
                    w.run_command(n, a)
                _on_main(_run_window)
                # If the command opened or switched to a view that's still
                # loading from disk (open_file is the obvious case), wait for
                # the load to finish before running the next command — the
                # whole point of letting the caller chain commands is that
                # later steps in the chain can target the just-opened file.
                av = _on_main(lambda: (
                    sublime.active_window().active_view()
                    if sublime.active_window() else None))
                _wait_until_loaded(av)
                results.append({
                    "command": cmd_name,
                    "scope": "window",
                    "ok": True,
                })
        except Exception as e:
            results.append({
                "command": cmd_name,
                "scope": cmd_scope,
                "ok": False,
                "error": str(e),
            })
            all_ok = False

    return {"ok": all_ok, "results": results}


# ============================================================================
# Registration
# ============================================================================

# (function, timeout_seconds). Order here is the order tools appear in the
# MCP `tools/list` response, which most clients render in registration order.
# Grouped to match the section ordering above.
TOOL_SPECS = [
    # 1. Project structure
    (list_folders_in_project, 5.0),
    (find_files_in_project, 15.0),

    # 2. File content
    (get_file_content, 5.0),
    (set_file_content, 15.0),

    # 3. Function / symbol
    (find_function_in_project, 5.0),
    (find_function_usages_in_project, 5.0),
    (list_functions_in_file, 10.0),
    (get_function_content, 10.0),
    (set_function_content, 15.0),

    # 4. Text search
    (search_text_in_project, 30.0),
    (search_text_in_open_files, 5.0),

    # 5. Editor state
    (get_current_selections, 5.0),
    (set_current_selection_content, 5.0),
    (run_sublime_command, 10.0),
]


def register_all(mcp):
    for fn, timeout in TOOL_SPECS:
        mcp.register(fn, timeout=timeout)
