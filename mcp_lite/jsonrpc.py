"""JSON-RPC 2.0 framing for MCP."""

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP-specific server error range is -32000 to -32099.
TIMEOUT_ERROR = -32000
TOOL_ERROR = -32001


def make_response(req_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


def make_error(code, message, data=None):
    e = {"code": int(code), "message": str(message)}
    if data is not None:
        e["data"] = data
    return e


def is_notification(msg):
    return isinstance(msg, dict) and "id" not in msg
