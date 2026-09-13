# -*- coding: utf-8 -*-
"""探测 ansa-mcp-sum 的 MCP server 能否独立启动，并完成一次真实的会话。

用途：MCP 客户端报 Not connected 时，区分
  (a) server 进程死了 / 起不来   -> 问题在 server 侧
  (b) server 健康但客户端没握手 -> 只需客户端重连

v1.0.2 第二批起，这个探针不再只做 initialize：它把「新加的能力层能不能被客户端
真正拿到」也测掉 —— tools/list 覆盖 78 个工具，resources/list 覆盖 5 个 URI，
resources/read 逐个把 pitfalls / workflows / memory 读回来并解析。注册了但读不出
的资源等于没加，而这一点在本地单元测试里是看不到的（本地是直接调函数，不走线）。

用法：
    python probe_mcp_sum_server.py          # 用下面写死的 PY / SRC / HOME
    python probe_mcp_sum_server.py --home <path>   # 换个 HOME（不动真实状态目录）
"""
import json
import os
import subprocess
import sys
import threading
import time

PY = r"C:\Users\Admin\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
SRC = r"D:\ansa-mcp-sum\src"
HOME = r"C:\Users\Admin\.ansa-mcp-sum"

if "--home" in sys.argv:
    HOME = sys.argv[sys.argv.index("--home") + 1]

# Also set it for *this* process: the cleanup step at the end imports
# ansa_mcp_sum.memory, whose CONFIG is bound from the environment at import
# time. Without this the probe would clean up the wrong HOME.
os.environ["ANSA_MCP_SUM_HOME"] = HOME

env = dict(os.environ)
env["PYTHONPATH"] = SRC
env["ANSA_MCP_SUM_HOME"] = HOME
env["ANSA_MCP_HEARTBEAT_STALE_SECONDS"] = "2147483647"
env["ANSA_MCP_TIMEOUT_SECONDS"] = "600"

EXPECTED_TOOLS = ("ping", "recall", "remember", "remember_note")
EXPECTED_RESOURCES = ("ansa://status", "ansa://capabilities", "ansa://pitfalls",
                      "ansa://workflows", "ansa://memory")

proc = subprocess.Popen(
    [PY, "-m", "ansa_mcp_sum.server"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
)

out_lines = []
replies = {}


def reader():
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            out_lines.append(line)
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict) and "id" in payload:
                replies[payload["id"]] = payload
    except Exception:
        pass


threading.Thread(target=reader, daemon=True).start()


def send(request_id, method, params=None):
    try:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                     "method": method, "params": params or {}}) + "\n")
        proc.stdin.flush()
    except Exception as exc:
        print("WRITE FAILED:", exc)


def wait_for(request_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if request_id in replies:
            return replies[request_id]
        time.sleep(0.1)
    return None


checks = []


def check(name, ok, detail=""):
    checks.append((bool(ok), name, detail))
    print("  [%s] %s%s" % ("OK" if ok else "FAIL", name, "  ->  " + detail if detail else ""))


# ---- 1) initialize ----
send(1, "initialize", {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "probe", "version": "2.0"},
})
init = wait_for(1, timeout=20)
check("stdio 握手 initialize",
      bool(init) and "protocolVersion" in json.dumps(init),
      (init or {}).get("result", {}).get("serverInfo", {}).get("name", "no reply"))

try:
    proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                 "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()
except Exception:
    pass

# ---- 2) tools/list ----
send(2, "tools/list")
tools_reply = wait_for(2, timeout=20)
tools = ((tools_reply or {}).get("result") or {}).get("tools") or []
check("tools/list 返回 78 个工具", len(tools) == 78, "%d 个" % len(tools))
names = {t.get("name") for t in tools}
missing = [n for n in EXPECTED_TOOLS if n not in names]
check("记忆工具已注册", not missing, ", ".join(missing) or "ping/recall/remember/remember_note")
annotated = [t for t in tools if t.get("annotations")]
check("工具带读写注解", len(annotated) == len(tools),
      "%d/%d" % (len(annotated), len(tools)))
described = [t for t in tools if (t.get("description") or "").strip()]
check("工具都有描述（空描述会让模型瞎猜）", len(described) == len(tools),
      "%d/%d" % (len(described), len(tools)))
guided = [t for t in tools if "Known pitfalls" in (t.get("description") or "")]
check("踩坑提示随工具描述下发", len(guided) >= 5, "%d 个工具" % len(guided))

# ---- 3) resources/list ----
send(3, "resources/list")
res_reply = wait_for(3, timeout=20)
resources = ((res_reply or {}).get("result") or {}).get("resources") or []
uris = {r.get("uri") for r in resources}
check("resources/list 暴露 5 个 URI", len(uris) == 5, ", ".join(sorted(str(u) for u in uris)))
absent = [u for u in EXPECTED_RESOURCES if u not in uris]
check("pitfalls / workflows / memory 都可发现", not absent, ", ".join(absent))

# ---- 4) resources/read —— 注册了但读不出来等于没加 ----
for index, uri in enumerate(EXPECTED_RESOURCES):
    request_id = 100 + index
    send(request_id, "resources/read", {"uri": uri})
    reply = wait_for(request_id, timeout=20)
    contents = ((reply or {}).get("result") or {}).get("contents") or []
    if not contents:
        check("resources/read %s" % uri, False, "空回复")
        continue
    text = contents[0].get("text") or ""
    if uri in ("ansa://pitfalls", "ansa://workflows", "ansa://memory",
               "ansa://capabilities"):
        try:
            parsed = json.loads(text)
            check("resources/read %s 可解析为 JSON" % uri,
                  isinstance(parsed, dict), "%d bytes" % len(text))
        except Exception as exc:
            check("resources/read %s 可解析为 JSON" % uri, False,
                  "%s: %s" % (type(exc).__name__, exc))
    else:
        check("resources/read %s" % uri, bool(text.strip()), "%d bytes" % len(text))

def tool_payload(reply):
    """Extract the JSON a tool returned from a tools/call reply.

    The inner payload travels as a *string* inside result.content[0].text, so
    searching the outer JSON forces you to reason about escaping (the inner
    quotes arrive as \\"). Parsing it is both simpler and stricter than the
    substring match this probe used first, which failed on a correct reply.
    """
    try:
        text = (reply or {}).get("result", {}).get("content", [{}])[0].get("text", "")
        return json.loads(text)
    except Exception:
        return None


# ---- 5) 记忆工具真的能读回来（走线，不走本地函数） ----
send(200, "tools/call", {"name": "remember",
                         "arguments": {"key": "probe_marker", "value": 1,
                                       "note": "written by probe_mcp_sum_server"}})
remember_payload = tool_payload(wait_for(200, timeout=20))
check("tools/call remember 成功", (remember_payload or {}).get("ok") is True,
      json.dumps(remember_payload or {})[:110])
check("remember 回显写入的记录",
      (((remember_payload or {}).get("data") or {}).get("record") or {}).get("value") == 1,
      json.dumps((remember_payload or {}).get("data") or {})[:110])

send(201, "tools/call", {"name": "recall", "arguments": {"key": "probe_marker"}})
recall_payload = tool_payload(wait_for(201, timeout=20))
check("tools/call recall 读回刚写的值",
      (((recall_payload or {}).get("data") or {}).get("record") or {}).get("value") == 1,
      json.dumps(recall_payload or {})[:110])

send(202, "tools/call", {"name": "recall", "arguments": {"key": "probe_never_set"}})
missing_payload = tool_payload(wait_for(202, timeout=20))
check("recall 对未知键返回 NotFound 并列出已知键",
      (missing_payload or {}).get("ok") is False
      and "probe_marker" in json.dumps(missing_payload or {}),
      json.dumps(missing_payload or {})[:110])

# ---- 6) 收尾：擦掉探针自己写的标记 ----
# Default HOME is the live one, and a health check should not leave litter in
# the operator's memory. The write→read path has already been proven above, so
# the marker has served its purpose.
cleaned = False
try:
    sys.path.insert(0, SRC)
    from ansa_mcp_sum import memory as memory_module
    cleaned = memory_module.delete_pref("probe_marker")
except Exception as exc:
    print("  [WARN] 探针标记清理失败：%r" % (exc,))
check("探针清理掉自己写入的标记（不给真实 HOME 留垃圾）", cleaned or HOME != r"C:\Users\Admin\.ansa-mcp-sum",
      "HOME=%s" % HOME)

alive = proc.poll() is None
try:
    proc.terminate()
except Exception:
    pass
time.sleep(1)

failed = [name for ok, name, _ in checks if not ok]
print("-" * 66)
print("ALIVE_BEFORE_TERMINATE:", alive)
print("RETURN_CODE:", proc.returncode)
print("探针结果：%d/%d 通过" % (len(checks) - len(failed), len(checks)))
if failed:
    print("失败项：")
    for name in failed:
        print("  - %s" % name)
if os.environ.get("PROBE_VERBOSE"):
    print("--- SERVER STDOUT (%d lines) ---" % len(out_lines))
    for line in out_lines[:12]:
        print(line[:900])
    try:
        err = proc.stderr.read()
    except Exception:
        err = ""
    if err:
        print("--- SERVER STDERR ---")
        print(err[:1200])

raise SystemExit(1 if failed else 0)
