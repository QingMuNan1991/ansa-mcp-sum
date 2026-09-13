# -*- coding: utf-8 -*-
"""诊断 ansa-mcp-sum 的 MCP 通道健康状态，逐环定位断点。

用法:
    python check_mcp_sum_health.py          # 快速检查（不启动 server）
    python check_mcp_sum_health.py --deep   # 额外做一次 stdio 握手（约 12 秒）

检查链条（从客户端到 ANSA）：
    配置条目 -> 可执行文件 -> 源码路径 -> 模块可导入 -> server 进程 -> 桥状态 -> ANSA 插件
"""
import json
import os
import re
import subprocess
import sys
import time

MCP_JSON = r"C:\Users\Admin\.workbuddy\mcp.json"
SUM_HOME = r"C:\Users\Admin\.ansa-mcp-sum"
SERVER_KEY = "ansa-mcp-sum"

OK, BAD, WARN = "  [OK]  ", "  [FAIL]", "  [WARN]"
results = []


def say(level, text, note=""):
    line = "%s %s" % (level, text)
    if note:
        line += "  ->  " + note
    print(line)
    results.append((level.strip(" []"), text))


print("=" * 66)
print("ansa-mcp-sum 通道自检    %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 66)

# ---- 1) 配置条目 ----
cfg = {}
python_exe = src_path = None
try:
    with open(MCP_JSON, "r", encoding="utf-8") as f:
        cfg = json.load(f).get("mcpServers", {})
except Exception as e:
    say(BAD, "读取 mcp.json", repr(e)[:80])

entry = cfg.get(SERVER_KEY)
if entry is None:
    say(BAD, "配置条目 %s" % SERVER_KEY, "mcp.json 里没有这一条")
else:
    say(OK, "配置条目 %s" % SERVER_KEY,
        "disabled=%s, type=%s" % (entry.get("disabled", False), entry.get("type")))
    python_exe = entry.get("command")
    env = entry.get("env", {}) or {}
    src_path = env.get("PYTHONPATH")

# ---- 2) 可执行文件 ----
if python_exe:
    if os.path.isfile(python_exe):
        say(OK, "解释器存在", python_exe)
    else:
        say(BAD, "解释器不存在", python_exe)

# ---- 3) 源码路径 ----
if src_path:
    if os.path.isdir(src_path):
        say(OK, "PYTHONPATH 存在", src_path)
    else:
        say(BAD, "PYTHONPATH 不存在", src_path)
pkg = os.path.join(src_path or "", "ansa_mcp_sum", "server.py")
if src_path and os.path.isfile(pkg):
    say(OK, "server.py 存在", pkg)
elif src_path:
    say(BAD, "server.py 缺失", pkg)

# ---- 4) 模块可导入 ----
if python_exe and src_path:
    env = dict(os.environ)
    env["PYTHONPATH"] = src_path
    env["ANSA_MCP_SUM_HOME"] = SUM_HOME
    try:
        r = subprocess.run([python_exe, "-c",
                            "import ansa_mcp_sum.server as s;"
                            "print(len([n for n in dir(s) if n.startswith('tool_')]))"],
                           capture_output=True, text=True, env=env, timeout=60)
        if r.returncode == 0 and r.stdout.strip().isdigit():
            say(OK, "模块可导入", "注册了 %s 个 tool_ 函数" % r.stdout.strip())
        else:
            say(BAD, "模块导入失败", (r.stderr or r.stdout)[-160:].replace("\n", " "))
    except Exception as e:
        say(BAD, "模块导入异常", repr(e)[:100])

# ---- 5) server 进程（并核对它跑的是不是当前源码） ----
# 这一步存在的理由：上面的"模块可导入"是**另起一个解释器**数出来的，回答的是
# "当前源码会注册几个工具"，不是"客户端正连着的那个进程注册了几个"。两者可以不一致 ——
# 实测就出现过：源文件 13:05 改完，而 server 进程 12:57 就起来了，于是 73 个工具的源码
# 只能被看到 70 个，新加的工具/资源"怎么调都不存在"。比进程启动时间与 server.py 的
# mtime 是唯一能一眼定性的手段。
ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and "
      "$_.CommandLine -like '*ansa_mcp_sum.server*' } "
      "| ForEach-Object { \"$($_.ProcessId) :: $($_.CreationDate.ToString('yyyy-MM-dd HH:mm:ss'))\" }")
try:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=60)
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if lines:
        say(OK, "server 进程在跑", "%d 个" % len(lines))
        for ln in lines:
            print("           %s" % ln[:130])

        server_py = os.path.join(src_path or "", "ansa_mcp_sum", "server.py")
        try:
            changed = os.path.getmtime(server_py)
            changed_txt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(changed))
            stale = []
            for ln in lines:
                parts = [p.strip() for p in ln.split("::")]
                started_txt = parts[1] if len(parts) > 1 else ""
                try:
                    started = time.mktime(time.strptime(started_txt, "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    continue
                if started < changed:
                    stale.append("%s (启动 %s)" % (parts[0], started_txt))
            if stale:
                say(WARN, "server 进程早于 server.py 的修改时间",
                    "源码改于 %s；这些进程跑的是改前的代码：%s" % (changed_txt, ", ".join(stale)))
                print("           后果：新加的工具 / resource 客户端看不到，只表现为\"调不到\"。")
                print("           修法：在连接器管理页把 %s 关掉再打开（不要直接 kill —— 它是客户端的子进程）。"
                      % SERVER_KEY)
            else:
                say(OK, "server 进程与当前源码对齐",
                    "均启动于 server.py 的最后修改（%s）之后" % changed_txt)
        except Exception as e:
            say(WARN, "无法比对进程与源码时间", repr(e)[:90])
    else:
        say(BAD, "server 进程不存在", "客户端没有把它拉起来 —— 这就是 Not connected 的直接原因")
except Exception as e:
    say(WARN, "无法枚举进程", repr(e)[:90])

# ---- 6) 桥状态 ----
# status.json 的年龄是判断"ANSA 侧到底有没有在轮询"最便宜的证据：轮询活着
# 的时候，每次 tick 都会走到 plugin.write_idle_status()，而它的节流是 5 秒。
# 所以 running 状态下 30 秒不写盘不是节流，是定时器没在跳。
# 反过来，Run Once 走的是 direct call，桥死着它照样跑 —— 不能拿它当证据。
st_path = os.path.join(SUM_HOME, "status.json")
if os.path.isfile(st_path):
    try:
        with open(st_path, "r", encoding="utf-8") as f:
            st = json.load(f)
        age = time.time() - float(st.get("timestamp", 0))
        state = st.get("status")
        if state == "stopped":
            say(OK, "桥状态文件",
                "status=stopped（Stop 按过，轮询是主动停的），心跳距今 %.0f 秒" % age)
        else:
            say(OK if age < 30 else WARN, "桥状态文件",
                "status=%s, pid=%s, 心跳距今 %.0f 秒, 已处理 %s 条"
                % (state, st.get("pid"), age, st.get("processed_count")))
            if age >= 30:
                print("           轮询活着时最多 5 秒写一次盘，所以这不是写盘节流 —— 定时器没在跳。")
                print("           三个已知成因：")
                print("             1) _busy 卡住：上一条命令没返回，之后每个 tick 直接 return")
                print("             2) _stop_requested 被 Stop 拉高：tick 静默 return，不写盘")
                print("             3) 定时器 active 但不再投递：GUI 主线程被模态框占住")
                print("           修法：把 scripts/bridge_doctor.py 投进队列，点 MCP-Sum > Run Once。")
                print("                 它绕开定时器执行，会打印真实状态、清软闸门，并把当前")
                print("                 autoload 脚本热重载进内存（不必重启 ANSA）。")
                print("                 若本来就只是 Stop 拉高了闸门，直接点 Auto Poll 也行。")
        msg = st.get("message") or ""
        if "in flight" in msg or "re-entered" in msg:
            say(WARN, "状态里带着「命令仍在执行」的痕迹", msg[:110])
    except Exception as e:
        say(WARN, "解析 status.json 失败", repr(e)[:90])
else:
    say(BAD, "status.json 不存在", st_path)

pending = os.path.join(SUM_HOME, "commands")
if os.path.isdir(pending):
    now = time.time()
    rows = []
    for f in os.listdir(pending):
        if f.startswith("cmd_"):
            p = os.path.join(pending, f)
            try:
                rows.append((now - os.path.getmtime(p), f))
            except Exception:
                rows.append((0.0, f))
    if not rows:
        say(OK, "待处理命令队列", "0 条")
    else:
        rows.sort(reverse=True)
        oldest = rows[0][0]
        say(OK if oldest < 60 else BAD, "待处理命令队列",
            "%d 条，最老一条已等 %.0f 秒" % (len(rows), oldest))
        if oldest >= 60:
            print("           命令躺了超过 1 分钟没人取 = 轮询确认停摆（这两件事必须一起看）。")
            print("           注意超过 timeout+60s 的命令会被 cleanup_stale_commands 丢掉并记进")
            print("           logs/dropped.log —— 所以「命令凭空消失」的第一现场在那里。")

# ---- 6b) 文件通道 ping：直接证明桥是否活着 ----
# 心跳老化可能只是插件的写盘节流（空闲时降频），ping 才是硬证据。
SUM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cli = os.path.join(SUM_ROOT, "scripts", "mcp_cli.py")
if os.path.isfile(cli) and python_exe:
    try:
        r = subprocess.run([python_exe, cli, "ping", "--timeout", "20"],
                           capture_output=True, text=True, timeout=60, cwd=SUM_ROOT)
        out = r.stdout or ""
        if '"success": true' in out or '"pong"' in out:
            say(OK, "文件通道 ping", "桥在正常轮询并应答")
        else:
            say(BAD, "文件通道 ping 无响应", "桥可能停了，或 ANSA 没开着")
    except Exception as e:
        say(WARN, "文件通道 ping 异常", repr(e)[:90])

# ---- 7) 深度握手 ----
if "--deep" in sys.argv:
    print("-" * 66)
    print("走线会话测试（启动临时 server 进程，约 20 秒）...")
    probe = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "probe_mcp_sum_server.py")
    if os.path.isfile(probe):
        try:
            r = subprocess.run([python_exe or sys.executable, probe],
                               capture_output=True, text=True, timeout=180)
            out = r.stdout or ""
            # The probe reports its own verdict as "探针结果：N/M 通过" and exits
            # non-zero on failure. Grepping stdout for a raw "protocolVersion"
            # string stopped working the moment the probe grew past
            # `initialize`: the check has to read the probe's contract, not the
            # JSON-RPC wire format it happens to use today.
            summary = re.search(r"探针结果：(\d+)/(\d+) 通过", out)
            if summary:
                passed, total = int(summary.group(1)), int(summary.group(2))
                say(OK if passed == total else BAD, "会话探针（走 JSON-RPC 真实链路）",
                    "%d/%d 项：initialize + tools/list + resources/list + resources/read + tools/call"
                    % (passed, total))
                for line in out.splitlines():
                    if "[FAIL]" in line:
                        print("           %s" % line.strip()[:130])
            else:
                say(BAD, "会话探针无结果",
                    " / ".join((out or r.stderr or "无输出").strip().splitlines()[-2:])[:170])
        except Exception as e:
            say(BAD, "会话探针异常", repr(e)[:100])
    else:
        say(WARN, "找不到 probe_mcp_sum_server.py", probe)

# ---- 汇总 ----
print("=" * 66)
fails = [t for lv, t in results if lv == "FAIL"]
if fails:
    print("结论：链路在第 %d 环断开 -> %s" % (len(fails), "；".join(fails)))
    print()
    print("恢复动作（按顺序试）：")
    print("  1. 连接器管理页右上角「自定义连接器」入口，找到 ansa-mcp-sum，点「信任」/ 重新连接")
    print("  2. 仍不行 -> 完全退出并重启 WorkBuddy（重启会重新 spawn 所有 enabled 的 MCP server）")
    print("  3. 期间可先用文件通道工作：python scripts/mcp_cli.py ping")
else:
    print("结论：链路完好。客户端侧若仍报 Not connected，只需重连或重启客户端。")
print("=" * 66)
