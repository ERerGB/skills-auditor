"""One bounded JSON request over an existing owner-controlled SSH connection."""
import json
import sys

from ..lifecycle.common import LifecycleError
from .common import MAX_BYTES, decode_json
from .node import Node


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise LifecycleError("workspace_transport", "Request exceeds the 64 MiB transport bound.")
        request = decode_json(raw)
        state_dir = request.get("state_dir")
        if not isinstance(state_dir, str) or not state_dir.startswith("/"):
            raise LifecycleError("workspace_state", "Select the target's absolute owner-local state directory.")
        result = Node(state_dir).dispatch(request)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (LifecycleError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "code": getattr(error, "code", "workspace_protocol"),
                          "message": str(error)[:1000], "details": getattr(error, "details", {})}))
        return getattr(error, "exit_code", 3)


if __name__ == "__main__":
    raise SystemExit(main())
