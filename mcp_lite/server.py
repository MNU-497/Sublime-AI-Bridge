"""Minimal MCP server: protocol surface + tool dispatch with timeout/cancel."""
import concurrent.futures
import json
import re
import threading
import traceback
import urllib.parse

from .jsonrpc import (
    INTERNAL_ERROR, INVALID_PARAMS, METHOD_NOT_FOUND, PARSE_ERROR,
    TIMEOUT_ERROR, TOOL_ERROR, make_error, make_response,
)
from .schema import (
    build_input_schema, build_prompt_arguments, description_from_doc,
)

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TOOL_TIMEOUT = 5.0
DEFAULT_PROMPT_TIMEOUT = 5.0
DEFAULT_RESOURCE_TIMEOUT = 5.0


def _template_to_regex(template):
    """Convert an RFC 6570 level-1 template ("scheme://path/{var}") to a regex.

    Captured params are matched as `[^/]+`, which is right for a single path
    segment. Anything fancier (RFC 6570 level 2+, multi-segment captures) is
    out of scope -- this matcher exists only for the @-mention shapes the
    server actually exposes.
    """
    parts = re.split(r"\{(\w+)\}", template)
    pattern = ""
    for i, part in enumerate(parts):
        if i % 2 == 0:
            pattern += re.escape(part)
        else:
            pattern += "(?P<" + part + ">[^/]+)"
    return re.compile("^" + pattern + "$")

_cancel_local = threading.local()


def is_cancelled():
    """Tools that loop over file I/O should poll this and exit early when set."""
    ev = getattr(_cancel_local, "event", None)
    return ev is not None and ev.is_set()


class _Tool:
    __slots__ = ("name", "fn", "description", "input_schema", "timeout")

    def __init__(self, fn, name=None, description=None, timeout=DEFAULT_TOOL_TIMEOUT):
        self.fn = fn
        self.name = name or fn.__name__
        self.description = description or description_from_doc(fn)
        self.input_schema = build_input_schema(fn)
        self.timeout = float(timeout)


class _Prompt:
    __slots__ = ("name", "fn", "description", "arguments", "timeout")

    def __init__(self, fn, name=None, description=None,
                 arg_descriptions=None, timeout=DEFAULT_PROMPT_TIMEOUT):
        self.fn = fn
        self.name = name or fn.__name__
        self.description = description or description_from_doc(fn)
        self.arguments = build_prompt_arguments(fn, arg_descriptions)
        self.timeout = float(timeout)


class _Resource:
    __slots__ = ("uri", "name", "fn", "description", "mime_type", "timeout")

    def __init__(self, uri, fn, name=None, description=None,
                 mime_type="text/plain", timeout=DEFAULT_RESOURCE_TIMEOUT):
        self.uri = uri
        self.fn = fn
        self.name = name or fn.__name__
        self.description = description or description_from_doc(fn)
        self.mime_type = mime_type
        self.timeout = float(timeout)


class _ResourceTemplate:
    __slots__ = ("uri_template", "name", "fn", "description", "mime_type",
                 "timeout", "_regex")

    def __init__(self, uri_template, fn, name=None, description=None,
                 mime_type="text/plain", timeout=DEFAULT_RESOURCE_TIMEOUT):
        self.uri_template = uri_template
        self.fn = fn
        self.name = name or fn.__name__
        self.description = description or description_from_doc(fn)
        self.mime_type = mime_type
        self.timeout = float(timeout)
        self._regex = _template_to_regex(uri_template)

    def match(self, uri):
        m = self._regex.match(uri)
        if not m:
            return None
        # URL-decode each captured group so callers see "load new users" for
        # an @mention typed as "load%20new%20users".
        return {k: urllib.parse.unquote(v) for k, v in m.groupdict().items()}


class MCPServer:
    def __init__(self, name, version="0.1.0", logger=None):
        self.name = name
        self.version = version
        self._tools = {}
        self._prompts = {}
        self._resources = {}            # uri -> _Resource
        self._resource_templates = []   # list[_ResourceTemplate], scanned in order
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="mcp-tool",
        )
        self._log = logger or (lambda *a, **kw: None)

    # ---- registration ------------------------------------------------------

    def register(self, fn, name=None, description=None, timeout=DEFAULT_TOOL_TIMEOUT):
        tool = _Tool(fn, name=name, description=description, timeout=timeout)
        self._tools[tool.name] = tool
        return tool

    def tool(self, *, name=None, description=None, timeout=DEFAULT_TOOL_TIMEOUT):
        def deco(fn):
            self.register(fn, name=name, description=description, timeout=timeout)
            return fn
        return deco

    def register_prompt(self, fn, name=None, description=None,
                        arg_descriptions=None, timeout=DEFAULT_PROMPT_TIMEOUT):
        prompt = _Prompt(fn, name=name, description=description,
                         arg_descriptions=arg_descriptions, timeout=timeout)
        self._prompts[prompt.name] = prompt
        return prompt

    def prompt(self, *, name=None, description=None, arg_descriptions=None,
               timeout=DEFAULT_PROMPT_TIMEOUT):
        def deco(fn):
            self.register_prompt(fn, name=name, description=description,
                                 arg_descriptions=arg_descriptions, timeout=timeout)
            return fn
        return deco

    def register_resource(self, uri, fn, name=None, description=None,
                          mime_type="text/plain",
                          timeout=DEFAULT_RESOURCE_TIMEOUT):
        res = _Resource(uri, fn, name=name, description=description,
                        mime_type=mime_type, timeout=timeout)
        self._resources[uri] = res
        return res

    def register_resource_template(self, uri_template, fn, name=None,
                                   description=None, mime_type="text/plain",
                                   timeout=DEFAULT_RESOURCE_TIMEOUT):
        tmpl = _ResourceTemplate(uri_template, fn, name=name,
                                 description=description, mime_type=mime_type,
                                 timeout=timeout)
        self._resource_templates.append(tmpl)
        return tmpl

    def shutdown(self):
        self._executor.shutdown(wait=False)

    # ---- dispatch ----------------------------------------------------------

    def handle(self, msg):
        """Returns a response dict, or None for notifications (no reply)."""
        if not isinstance(msg, dict):
            return make_response(None, error=make_error(INVALID_REQUEST, "not an object"))

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            return self._on_initialize(req_id, params)
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return make_response(req_id, {})
        if method == "tools/list":
            return self._on_tools_list(req_id)
        if method == "tools/call":
            return self._on_tools_call(req_id, params)
        if method == "prompts/list":
            return self._on_prompts_list(req_id)
        if method == "prompts/get":
            return self._on_prompts_get(req_id, params)
        if method == "resources/list":
            return self._on_resources_list(req_id)
        if method == "resources/templates/list":
            return self._on_resource_templates_list(req_id)
        if method == "resources/read":
            return self._on_resources_read(req_id, params)
        if req_id is None:
            return None
        return make_response(req_id, error=make_error(
            METHOD_NOT_FOUND, "method not found: {}".format(method)))

    def _on_initialize(self, req_id, params):
        return make_response(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
                "prompts": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
            },
            "serverInfo": {"name": self.name, "version": self.version},
        })

    def _on_tools_list(self, req_id):
        tools = [{
            "name": t.name,
            "description": t.description,
            "inputSchema": t.input_schema,
        } for t in self._tools.values()]
        return make_response(req_id, {"tools": tools})

    def _on_tools_call(self, req_id, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or name not in self._tools:
            return make_response(req_id, error=make_error(
                INVALID_PARAMS, "unknown tool: {}".format(name)))
        if not isinstance(args, dict):
            return make_response(req_id, error=make_error(
                INVALID_PARAMS, "arguments must be an object"))

        tool = self._tools[name]
        cancel = threading.Event()

        def runner():
            _cancel_local.event = cancel
            try:
                return tool.fn(**args)
            finally:
                _cancel_local.event = None

        fut = self._executor.submit(runner)
        try:
            result = fut.result(timeout=tool.timeout)
        except concurrent.futures.TimeoutError:
            cancel.set()
            self._log("tool %s exceeded %.1fs timeout", name, tool.timeout)
            return _tool_error(req_id, "timeout: {} exceeded {}s".format(name, tool.timeout),
                               code=TIMEOUT_ERROR, is_error=True)
        except TypeError as e:
            return make_response(req_id, error=make_error(INVALID_PARAMS, str(e)))
        except Exception as e:
            self._log("tool %s raised: %s\n%s", name, e, traceback.format_exc())
            return _tool_error(req_id, "{}: {}".format(type(e).__name__, e),
                               code=TOOL_ERROR, is_error=True)

        return make_response(req_id, {
            "content": [{"type": "text", "text": _to_text(result)}],
            "isError": False,
        })

    # ---- prompts -----------------------------------------------------------

    def _on_prompts_list(self, req_id):
        prompts = [{
            "name": p.name,
            "description": p.description,
            "arguments": p.arguments,
        } for p in self._prompts.values()]
        return make_response(req_id, {"prompts": prompts})

    def _on_prompts_get(self, req_id, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or name not in self._prompts:
            return make_response(req_id, error=make_error(
                INVALID_PARAMS, "unknown prompt: {}".format(name)))
        if not isinstance(args, dict):
            return make_response(req_id, error=make_error(
                INVALID_PARAMS, "arguments must be an object"))

        prompt = self._prompts[name]
        cancel = threading.Event()

        def runner():
            _cancel_local.event = cancel
            try:
                return prompt.fn(**args)
            finally:
                _cancel_local.event = None

        fut = self._executor.submit(runner)
        try:
            result = fut.result(timeout=prompt.timeout)
        except concurrent.futures.TimeoutError:
            cancel.set()
            self._log("prompt %s exceeded %.1fs timeout", name, prompt.timeout)
            return make_response(req_id, error=make_error(
                TIMEOUT_ERROR, "timeout: {} exceeded {}s".format(name, prompt.timeout)))
        except TypeError as e:
            return make_response(req_id, error=make_error(INVALID_PARAMS, str(e)))
        except Exception as e:
            self._log("prompt %s raised: %s\n%s", name, e, traceback.format_exc())
            return make_response(req_id, error=make_error(
                INTERNAL_ERROR, "{}: {}".format(type(e).__name__, e)))

        return make_response(req_id, _normalize_prompt_result(result))

    # ---- resources ---------------------------------------------------------

    def _on_resources_list(self, req_id):
        resources = [{
            "uri": r.uri,
            "name": r.name,
            "description": r.description,
            "mimeType": r.mime_type,
        } for r in self._resources.values()]
        return make_response(req_id, {"resources": resources})

    def _on_resource_templates_list(self, req_id):
        templates = [{
            "uriTemplate": t.uri_template,
            "name": t.name,
            "description": t.description,
            "mimeType": t.mime_type,
        } for t in self._resource_templates]
        return make_response(req_id, {"resourceTemplates": templates})

    def _on_resources_read(self, req_id, params):
        uri = params.get("uri")
        if not isinstance(uri, str):
            return make_response(req_id, error=make_error(
                INVALID_PARAMS, "params.uri must be a string"))

        # Static resources first; then walk templates in registration order.
        resource = self._resources.get(uri)
        kwargs = {}
        if resource is None:
            for tmpl in self._resource_templates:
                captured = tmpl.match(uri)
                if captured is not None:
                    resource = tmpl
                    kwargs = captured
                    break

        if resource is None:
            return make_response(req_id, error=make_error(
                INVALID_PARAMS, "no resource matches uri: {}".format(uri)))

        cancel = threading.Event()

        def runner():
            _cancel_local.event = cancel
            try:
                return resource.fn(**kwargs)
            finally:
                _cancel_local.event = None

        fut = self._executor.submit(runner)
        try:
            result = fut.result(timeout=resource.timeout)
        except concurrent.futures.TimeoutError:
            cancel.set()
            self._log("resource %s exceeded %.1fs timeout", uri, resource.timeout)
            return make_response(req_id, error=make_error(
                TIMEOUT_ERROR, "timeout: {} exceeded {}s".format(
                    uri, resource.timeout)))
        except TypeError as e:
            return make_response(req_id, error=make_error(INVALID_PARAMS, str(e)))
        except Exception as e:
            self._log("resource %s raised: %s\n%s", uri, e, traceback.format_exc())
            return make_response(req_id, error=make_error(
                INTERNAL_ERROR, "{}: {}".format(type(e).__name__, e)))

        return make_response(req_id, _normalize_resource_result(
            result, uri, resource.mime_type))


def _normalize_resource_result(result, uri, default_mime):
    """Accept str | dict | list[dict], coerce to {contents: [...]}.

    str          -> single text content with the resource's default mime
    dict         -> single content; uri/mimeType filled in from the resource
    list[dict]   -> contents list; each entry gets uri/mimeType defaulted
    """
    def _fill(entry):
        if "uri" not in entry:
            entry["uri"] = uri
        if "mimeType" not in entry:
            entry["mimeType"] = default_mime
        return entry

    if isinstance(result, str):
        return {"contents": [{"uri": uri, "mimeType": default_mime, "text": result}]}
    if isinstance(result, dict):
        return {"contents": [_fill(dict(result))]}
    if isinstance(result, list):
        return {"contents": [_fill(dict(e)) for e in result]}
    raise TypeError("resource fn must return str, dict, or list[dict]")


def _normalize_prompt_result(result):
    """Accept str | list[message] | dict, coerce to {description?, messages}."""
    if isinstance(result, str):
        return {"messages": [_user_text(result)]}
    if isinstance(result, list):
        return {"messages": result}
    if isinstance(result, dict):
        if "messages" not in result:
            raise ValueError("prompt dict must include 'messages'")
        return result
    raise TypeError("prompt must return str, list, or dict")


def _user_text(text):
    return {"role": "user", "content": {"type": "text", "text": text}}


def _to_text(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _tool_error(req_id, message, code=TOOL_ERROR, is_error=True):
    # MCP convention: tool-level errors are still successful JSON-RPC responses
    # whose result has isError=true. Protocol-level errors use the error field.
    return make_response(req_id, {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    })
