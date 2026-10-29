"""The part of redact_pdf that matches regular expressions, in a separate process.

A pattern can take exponential time on the wrong text (``(a+)+$`` on a long run of
"a"), and Python's matcher can't be interrupted from another thread. So patterns run
in a child process with a CPU limit, which the server kills at its deadline.

Run as ``python -I -m paperboy.engine.pattern_worker``: the request is JSON on stdin,
and the answer one JSON object on stdout.
"""
from __future__ import annotations

import json
import re
import sys

from .table_worker import _limit


def run(request: dict) -> dict:
    """Every non-empty match of each pattern in each text, as [text, pattern, start,
    end], up to max_matches; or the first pattern that isn't valid."""
    flags = re.IGNORECASE if request["ignore_case"] else 0
    compiled = []
    for n, pattern in enumerate(request["patterns"]):
        try:
            compiled.append(re.compile(pattern, flags))
        except (re.error, OverflowError, RecursionError) as exc:
            return {"invalid": n, "error": str(exc)[:200]}
    found, room = [], request["max_matches"]
    for t, text in enumerate(request["texts"]):
        for p, regex in enumerate(compiled):
            for match in regex.finditer(text):
                if match.end() == match.start():
                    continue
                if len(found) == room:
                    return {"matches": found, "truncated": True}
                found.append([t, p, match.start(), match.end()])
    return {"matches": found, "truncated": False}


def main() -> None:
    request = json.loads(sys.stdin.read())
    _limit(request["cpu_seconds"], request["memory_bytes"])
    sys.stdout.write(json.dumps(run(request)))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
