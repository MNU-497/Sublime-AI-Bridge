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
    deadline = time.time() + LOAD_POLL_TIMEOUT
    while time.time() < deadline:
        if not _on_main(lambda v=view: v.is_loading()):
            break
        time.sleep(0.05)
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
    w = sublime.active_window()
    if w is None:
        return [], [], []
    folders = list(w.folders())
    project_data = w.project_data() or {}
    prefs = sublime.load_settings("Preferences.sublime-settings")

    def _list(obj, key):
        v = obj.get(key)
        return list(v) if v else []

    file_excludes = (
        _list(project_data, "file_exclude_patterns")
        + (prefs.get("file_exclude_patterns") or [])
        + (prefs.get("binary_file_patterns") or [])
    )
    folder_excludes = (
        _list(project_data, "folder_exclude_patterns")
        + (prefs.get("folder_exclude_patterns") or [])
    )
    return folders, file_excludes, folder_excludes


def _matches_any(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _walk(root, file_excludes, folder_excludes):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _matches_any(d, folder_excludes)]
        for fn in filenames:
            if _matches_any(fn, file_excludes):
                continue
            yield os.path.join(dirpath, fn)


def _looks_binary(path):
    try:
        with open(path, "rb") as fb:
            return b"\x00" in fb.read(4096)
    except OSError:
        return True


def _count_lines(buf):
    if not buf:
        return 0
    if buf.endswith("\n"):
        return buf.count("\n")
    return buf.count("\n") + 1


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
                          glob: Optional[str] = None) -> List[str]:
    """Search for files by basename across the active project's folders.

    pattern: fnmatch-style glob by default (e.g. "*.py"); set regex=true to treat as a Python regex. The optional `glob` arg is an additional fnmatch filter applied on top. Honors project + global file/folder exclude patterns. Capped at 1000 results.
    """
    folders, file_excludes, folder_excludes = _on_main(_get_search_config)
    if regex:
        rx = re.compile(pattern)
        def match(name): return rx.search(name) is not None
    else:
        def match(name): return fnmatch.fnmatchcase(name, pattern)
    glob_match = (lambda n: fnmatch.fnmatchcase(n, glob)) if glob else (lambda n: True)

    results = []
    checked = 0
    for folder in folders:
        for path in _walk(folder, file_excludes, folder_excludes):
            checked += 1
            if checked % SEARCH_CANCEL_CHECK_EVERY == 0 and is_cancelled():
                return results
            base = os.path.basename(path)
            if match(base) and glob_match(base):
                results.append(path)
                if len(results) >= FILENAME_RESULT_CAP:
                    return results
    return results


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
                         save: bool = False, row: Optional[int] = None) -> Dict[str, Any]:
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
    matching project + global exclude patterns and files with NUL bytes in
    the first 4KB. Returns up to `max_results` entries: {path, line, text}.

    For matching against unsaved buffer state, use `search_text_in_open_files`.
    """
    folders, file_excludes, folder_excludes = _on_main(_get_search_config)
    flags = 0 if case_sensitive else re.IGNORECASE
    rx = re.compile(pattern if regex else re.escape(pattern), flags)
    glob_match = (lambda n: fnmatch.fnmatchcase(n, glob)) if glob else (lambda n: True)

    results: List[Dict[str, Any]] = []
    checked = 0
    for folder in folders:
        for path in _walk(folder, file_excludes, folder_excludes):
            checked += 1
            if checked % SEARCH_CANCEL_CHECK_EVERY == 0 and is_cancelled():
                return results
            if not glob_match(os.path.basename(path)):
                continue
            if _looks_binary(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if rx.search(line):
                            results.append({
                                "path": path,
                                "line": lineno,
                                "text": line.rstrip("\n")[:500],
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

    return _on_main(go)


def set_current_selection_content(content: str) -> Dict[str, Any]:
    """Replace the FIRST selection in the active Sublime view with `content`.

    Symmetric counterpart to `get_current_selections` -- pair them when an AI
    needs to read the user's highlighted text, transform it, and write the
    result back without bothering with file paths or line numbers.

    Multi-cursor: only the first (top-most) selection is touched; any other
    cursors are left in place. If the first region is a zero-width cursor,
    `content` is INSERTED at that point. The edit is one Sublime undo entry
    and is left in the buffer (not saved to disk); call `save_open_file` or
    set `save=true` on a different tool to persist it.

    Untitled buffers are supported -- `project_file` will be a display name
    like `<untitled 5>`.

    Returns: {ok, project_file, replaced_start_line, replaced_stop_line,
    inserted_length, dirty}. The reported line range is the PRE-edit range of
    the selection that was replaced (with the same column-0 normalization as
    `get_current_selections`).
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

        return {
            "ok": True,
            "project_file": name,
            "replaced_start_line": start_row + 1,
            "replaced_stop_line": end_row + 1,
            "inserted_length": len(content),
            "dirty": view.is_dirty(),
        }

    return _on_main(go)


def run_sublime_command(command: str, args: Optional[Dict[str, Any]] = None,
                        file_path: Optional[str] = None) -> Dict[str, Any]:
    """Run an arbitrary Sublime Text command. Escape hatch.

    If `file_path` is given, runs as a TextCommand on that view (loading it
    transiently if needed). Otherwise runs as a WindowCommand on the active
    window.

    Examples -- window-level (no file_path):
      run_sublime_command("new_window")
      run_sublime_command("save_all")
      run_sublime_command("show_overlay", {"overlay": "command_palette", "text": "Format"})
      run_sublime_command("show_overlay", {"overlay": "goto", "show_files": true, "text": "@some_function"})
      run_sublime_command("toggle_side_bar")

    Examples -- view-level (file_path required):
      run_sublime_command("close", file_path="/path/to/file")
      run_sublime_command("detect_indentation", file_path="/path/to/file")
      run_sublime_command("duplicate_line", file_path="/path/to/file")
      run_sublime_command("goto_line", {"line": 1688}, "/path/to/file")
      run_sublime_command("reindent", {"single_line": false}, "/path/to/file")
      run_sublime_command("revert", file_path="/path/to/file")
      run_sublime_command("save", file_path="/path/to/file")
      run_sublime_command("set_file_type", {"syntax": "Packages/PHP/PHP.sublime-syntax"}, "/path/to/file")
      run_sublime_command("sort_lines", {"case_sensitive": false}, "/path/to/file")
      run_sublime_command("toggle_comment", {"block": false}, "/path/to/file")
      run_sublime_command("upper_case", file_path="/path/to/file")

    Rule: If the user says "help me find [criteria]", generate a regex pattern and immediately run run_sublime_command(command: show_panel, args: {"panel": "find_in_files", "pattern": "[GENERATED_REGEX]", "regex": true, "case_sensitive": false, "whole_word": false}). Ensure all required arguments are present.

    Examples of [criteria] to regex:
      "X"                          \\bX\\b
      "X or Y"                     \\b(?:X|Y)\\b
      "X and Y" (same line)        \\bX\\b.*\\bY\\b|\\bY\\b.*\\bX\\b
      "X near Y"                   \\bX\\b.{0,80}\\bY\\b|\\bY\\b.{0,80}\\bX\\b
      "X within N chars of Y"      \\bX\\b.{0,N}\\bY\\b|\\bY\\b.{0,N}\\bX\\b
      "X followed by Y"            \\bX\\b.{0,80}?\\bY\\b
      "X but not Y" (same line)    ^(?!.*\\bY\\b).*\\bX\\b
      "lines starting with X"      ^\\s*X
      "lines ending with X"        X\\s*$
      "function X" (PHP/JS)        \\bfunction\\s+X\\b
      "function X" (Python)        \\bdef\\s+X\\b
      "method X" (PHP)             \\b(?:public|private|protected|static|\\s)+function\\s+X\\b
      "TODO comments"              \\b(?:TODO|FIXME|XXX|HACK)\\b
      "trailing whitespace"        [ \\t]+$
      "any number"                 \\b\\d+\\b
      "URL"                        https?://\\S+
      "email"                      \\b[\\w.+-]+@[\\w.-]+\\.\\w+\\b
      "string literal X"           ["']X["']        (single OR double quoted)

    Notes:
      - Selection-based commands (sort_lines, toggle_comment, upper_case,
        duplicate_line, swap_line_up/down, join_lines) operate on whatever
        the user currently has selected. Pair with get_current_selections
        first to know what they'll affect.
      - Plugin-installed commands work too (Package Control, formatters,
        LSP). Discover names via Tools > Developer > Command Logger,
        which prints every command ST runs.
      - Some commands open interactive panels (show_panel for find/replace,
        prompt_open_file) -- useful for "show the user something" but
        won't return programmatic data to the caller.

    Returns {ok, scope, dirty?}.
    """
    args = args or {}
    if file_path:
        view, _was_open = _ensure_view(file_path)
        def go_view():
            view.run_command(command, args)
            return {"ok": True, "scope": "view", "dirty": view.is_dirty()}
        return _on_main(go_view)

    def go_win():
        w = sublime.active_window()
        if w is None:
            raise RuntimeError("no active Sublime window")
        w.run_command(command, args)
        return {"ok": True, "scope": "window"}
    return _on_main(go_win)


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
