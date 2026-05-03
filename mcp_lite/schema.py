"""Build JSON-Schema for tool inputs from Python type hints.

Supports the subset used by the SublimeAIBridge tools: str, int, float, bool,
List[X], Dict[str, Any], Optional[X], Any. ST 4 ships Python 3.8, so callers
must use typing.Dict / typing.List / typing.Optional rather than PEP 585/604
syntax.
"""
import inspect
import typing


_PRIMITIVES = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
}


def _is_optional(tp):
    if typing.get_origin(tp) is typing.Union:
        return type(None) in typing.get_args(tp)
    return False


def _strip_optional(tp):
    args = [a for a in typing.get_args(tp) if a is not type(None)]
    return args[0] if len(args) == 1 else typing.Union[tuple(args)]


def type_to_schema(tp):
    if tp is typing.Any or tp is None:
        return {}
    if tp in _PRIMITIVES:
        return dict(_PRIMITIVES[tp])

    if _is_optional(tp):
        inner = type_to_schema(_strip_optional(tp))
        if "type" in inner:
            t = inner["type"]
            inner["type"] = [t, "null"] if isinstance(t, str) else list(t) + ["null"]
        return inner

    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    if origin in (list, typing.List):
        item = type_to_schema(args[0]) if args else {}
        return {"type": "array", "items": item}
    if origin in (dict, typing.Dict):
        return {"type": "object"}
    if origin is typing.Union:
        return {"anyOf": [type_to_schema(a) for a in args if a is not type(None)]}

    return {}


def build_input_schema(fn):
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    properties = {}
    required = []
    for name, param in sig.parameters.items():
        if name.startswith("_"):
            continue
        properties[name] = type_to_schema(hints.get(name, typing.Any))
        if param.default is inspect.Parameter.empty:
            required.append(name)
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def description_from_doc(fn):
    doc = inspect.getdoc(fn) or ""
    return doc.strip()


def build_prompt_arguments(fn, arg_descriptions=None):
    """MCP prompts use a flat [{name, description, required}] list, not JSON Schema."""
    arg_descriptions = arg_descriptions or {}
    sig = inspect.signature(fn)
    out = []
    for name, param in sig.parameters.items():
        if name.startswith("_"):
            continue
        entry = {"name": name}
        desc = arg_descriptions.get(name)
        if desc:
            entry["description"] = desc
        entry["required"] = param.default is inspect.Parameter.empty
        out.append(entry)
    return out
