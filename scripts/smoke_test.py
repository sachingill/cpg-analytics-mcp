"""Smoke-test the MCP server over stdio with raw JSON-RPC. Stdlib only.

Usage:
    python scripts/smoke_test.py uv run cpg-analytics-mcp
    python scripts/smoke_test.py docker run -i --rm cpg-analytics-mcp

Exits non-zero if any check fails, so CI can use it directly.
"""

from __future__ import annotations

import json
import subprocess
import sys


def main(cmd: list[str]) -> int:
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    next_id = 0

    def request(method: str, params: dict | None = None) -> dict:
        nonlocal next_id
        next_id += 1
        msg = {"jsonrpc": "2.0", "id": next_id, "method": method, "params": params or {}}
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        while True:  # skip any server-initiated notifications
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError(f"server closed stdout while waiting for '{method}'")
            reply = json.loads(line)
            if reply.get("id") == next_id:
                return reply

    failures = 0

    def check(name: str, ok: bool) -> None:
        nonlocal failures
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}")

    try:
        init = request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "smoke-test", "version": "0"}})
        check("initialize returns serverInfo", init["result"]["serverInfo"]["name"] == "cpg-analytics")
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        tools = {t["name"] for t in request("tools/list")["result"]["tools"]}
        check("6 tools listed", len(tools) == 6)

        roi = request("tools/call", {"name": "get_metric",
                                     "arguments": {"metrics": ["promo_roi"], "group_by": ["channel"]}})
        check("get_metric promo_roi returns 5 channels",
              not roi["result"].get("isError") and roi["result"]["structuredContent"]["row_count"] == 5)

        blocked = request("tools/call", {"name": "run_sql",
                                         "arguments": {"sql": "SELECT cogs_per_unit FROM dim_product"}})
        check("restricted column is refused", blocked["result"].get("isError") is True)
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)

    print(f"{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1:]))
