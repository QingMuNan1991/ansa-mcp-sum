# ansa-mcp-sum 使用指南（USAGE）

> 一份面向实际使用的操作手册。读完即可：装好 → 起 MCP server → 在 ANSA GUI 里加载桥 → 用任意 MCP 客户端调用工具驱动 ANSA。
> 项目背景、架构与**测试验证**见 `README.md`。所有命令均在 **Windows + ANSA 25.1.4** 环境验证。

---

## 1. 它解决什么问题

| 项目 | 传输层 | 能否看到 GUI | 工具数 |
|------|--------|--------------|--------|
| `ansa-tcp-bridge` | TCP/IAP（`-b` 无界面） | ❌ 看不到 | 49 |
| `ansa-mcp` | 文件 IPC 命令队列（GUI） | ✅ 看得到 | ~20 |
| **`ansa-mcp-sum`** | **文件 IPC 命令队列（GUI）** | ✅ 看得到 | **78（65 派发 + 13 服务端辅助）** |

一句话：**用 ansa-mcp 的文件 IPC 传输层，把 tcp-bridge 的 49 个工具全部跑在 ANSA GUI 模式下**——既能用这些具名工具（当前合计 **78** 个），又能实时看到模型被操作。

---

## 2. 工作原理（文件 IPC）

```
MCP 客户端 ──stdio──▶ ansa-mcp-sum-server (server.py)
                          │  send_command(<type>, **kwargs)
                          ▼
        ANSA_MCP_SUM_HOME/commands/cmd_*.json   (落盘命令)
                          │
                          ▼  由 ANSA 主线程的重复定时器消费
        ANSA GUI 内 plugin.py 的 HANDLERS[<type>] → tools_impl.<fn>(command)
                          │  tools_impl 内调用 base.* / mesh.* / connections.*
                          ▼
        ANSA_MCP_SUM_HOME/results/<command_id>.json   (结果写回，文件名就是命令 id)
                          │
                          ▼
        server.py 读回结果 → 返回给 MCP 客户端
```

> **更正**：早期文档写作 `results/res_*.json`，实际文件名是
> `results/<command_id>.json`（12 位 hex，例如 `results/11843759560c.json`），
> 与 `commands/cmd_<command_id>.json` 一一对应。按旧写法去找文件是找不到的。


- 命令目录默认：`%USERPROFILE%\.ansa-mcp-sum\`（可用环境变量 `ANSA_MCP_SUM_HOME` 改）。
- GUI 模式下不靠心跳超时（按需桥），`timeout_seconds` 默认 600，给人工点击留足等待。

---

## 3. 安装

推荐装进受管 venv（里面 `mcp` 已锁 1.29.1，与本项目兼容）：

```powershell
# 用受管解释器
C:\Users\Admin\.workbuddy\binaries\python\envs\default\Scripts\pip install -e D:\ansa-mcp-sum
```

装完会多出一个入口：`ansa-mcp-sum-server`（位于该 venv 的 `Scripts\` 下）。

> 若用系统 Python，请确认 `mcp>=1.29` 已装，再 `pip install -e D:\ansa-mcp-sum`。

---

## 4. 启动 MCP server

在 **ANSA 之外的终端**启动（stdio 传输，给任意 MCP 客户端用）：

```powershell
ansa-mcp-sum-server
# 或者等价写法：
C:\Users\Admin\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m ansa_mcp_sum.server
```

启动即开始监听 `ANSA_MCP_SUM_HOME/commands` 目录，等待 ANSA 侧消费命令。

### 环境变量（可选）

| 变量 | 默认 | 说明 |
|------|------|------|
| `ANSA_MCP_SUM_HOME` | `~/.ansa-mcp-sum` | 命令/结果目录根 |
| `ANSA_MCP_TIMEOUT_SECONDS` | `600` | 单条命令等待 ANSA 回结果的超时 |
| `ANSA_MCP_HEARTBEAT_STALE_SECONDS` | `2147483647` | 按需桥不靠心跳，基本永不过期 |
| `ANSA_MCP_MAX_SCRIPT_CHARS` | `65536` | `run_python_script_in_ansa` 脚本大小上限 |

---

## 4.5 会话开始先调这三个工具（v1.0.2 第二批新增）

它们**不走 ANSA 桥**，所以 ANSA 还没开、桥挂了，照样能用 —— 而这恰恰是最需要它们的时候。

| 工具 | 什么时候用 |
|---|---|
| `recall()` | **会话第一件事。** 空参返回：本项目的偏好（deck、标准 `.ansa_mpar` 路径、目标边长、质量基线）+ 笔记 + 最近命令历史摘要 |
| `recall("deck")` | 取单条。键不存在时返回 `NotFound`，**并列出已经存在的键** |
| `remember(key, value, note)` | 存一条以后不想再重新推导的事实。覆盖时保留 `previous_value` |
| `remember_note(text, tag)` | 装不进键的观察（"这个件壁厚 1.86–3.88，所以中面取 3.0"） |

等价资源：`ansa://memory`（客户端支持 resource 时更省事）。同类还有两个：

| URI | 内容 |
|---|---|
| `ansa://pitfalls` | 本机 ANSA 的 **11 条**实测踩坑（症状/原因/正确写法/判据）。**写脚本动几何或读卡片值之前先看** |
| `ansa://workflows` | 5 段实测流水线（诊断 → 体检 → 载标准 → 中面+网格 → 验证），每步带真实 API 与验收标准 |

> 最要命的几条踩坑也写进了对应工具的 description（`execute_script`、`get_entity`、
> `get_bounding_box`、`get_node_coordinates`、`check_geometry`、`count_entities`、`export_*` 等
> **12 个**），所以即使客户端不取 resource，模型也能看到。

## 4.6 状态目录（`ANSA_MCP_SUM_HOME`，默认 `~/.ansa-mcp-sum`）

```
commands/          待执行命令（写进去 = 派发）。超龄的会被丢弃并记入 audit
results/<id>.json  命令结果。**超时之后也来这里查** —— 超时不取消 ANSA 侧操作
runs/<时间>-<id>/  每次派发的 input.json / result.json
logs/audit.jsonl   ★ 一行一条命令：真实耗时、模型路径前后变化、成败、失败类型。4MB 轮转
logs/dropped.log   被丢弃的过期命令（旧格式，保留兼容）
memory/prefs.json  ★ 跨会话偏好（键值）
memory/notes.md    ★ 跨会话笔记（追加式 Markdown）
artifacts/         脚本自用的中间产物（不要再往 HOME 根目录丢）
capabilities/      probe 产出的本机能力清单
status.json        桥状态与心跳（会被每次启动覆盖，**不是**持久状态）
stop.flag          写它 = 让轮询循环停下
```

**"我发的那条命令到底跑了没有"** —— 按顺序看三处，不用猜：

```bash
tail -5  "$HOME/logs/audit.jsonl"        # ① 跑过没有、多久、成没成、模型变没变
ls       "$HOME/results/<command_id>.json"  # ② 有结果文件 = 跑完了（超时后也来看这里）
ls       "$HOME/commands/"                # ③ 还躺在这里 = 从没被取走；超龄会记为 dropped_stale
```

> 超时**不取消** ANSA 侧的操作。重试一条改模型的命令之前，先看 ① 和 ②。
> 审计日志写不进去时不会让命令失败，但会把次数记在 `status.json` 的
> `audit_write_errors` 里 —— 忽略这个字段就等于信任一份可能不完整的日志。

## 4.7 写脚本前先查 API，跑脚本前先预检（v1.0.4 起）

ANSA 脚本错误里最贵的一类是**用了本机根本不存在的函数**：它要么静默返回 0、要么在操作跑到一半
时才抛 `TypeError`。本项目把另外那个 `ansa-api` MCP 的检索能力**内嵌**了进来（v1.0.6 起索引
随包分发），并加了一层执行前预检。

**查文档**（读包内 10.4 MB 索引：5892 个函数 / ANSA v25.1.4，**不需要 ANSA 在线**）：

| 工具 | 用途 |
|---|---|
| `ansa_api_doc_search(query, module?, category?, top_n?)` | 模糊找函数。传脚本名（`Mesh`、`GetEntity`）而不是菜单名，排序最准 |
| `ansa_api_doc_lookup(function_name, module?)` | 精确查一个函数：签名、参数、返回值、示例 |
| `ansa_api_doc_modules()` | 列所有模块与函数数 |
| `ansa_api_doc_categories()` | 列所有类别与函数数 |

**跑之前预检**：

```python
validate_script(script="...")                  # 只检查，不执行
execute_script(script="...")                   # validate=True 是默认值，会自动先预检
execute_script(script="...", validate=False)   # 只有确定调用合法、但探测不到时才关掉
```

预检会把脚本里每个 `ansa.<module>.<func>`（以及裸 `<module>.<func>`）对照**正在运行的** ANSA
build，有缺失就返回 `data.missing`（缺哪些调用）与 `data.checked`，**在真正执行前**就让你改掉。
注意它需要桥在线（校验的是活着的 build），桥没起来时会被插件就绪门禁挡下。

## 5. 配置 MCP 客户端

把下面这段加进你的 `mcp.json`（WorkBuddy：`C:\Users\Admin\.workbuddy\mcp.json`；Claude Desktop 类似）。**配一边，另一边不生效**——本项目是独立 server。

```json
{
  "mcpServers": {
    "ansa-mcp-sum": {
      "command": "C:\\Users\\Admin\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\ansa-mcp-sum-server.exe",
      "args": [],
      "env": {
        "ANSA_MCP_SUM_HOME": "C:\\Users\\Admin\\.ansa-mcp-sum"
      }
    }
  }
}
```

> 若 `pip install -e .` 装到了别的解释器，把 `command` 改成该解释器 `Scripts\ansa-mcp-sum-server.exe` 的绝对路径即可。修改后需在客户端里 **Trust / 重新启用** 该 server。

---

## 6. 在 ANSA GUI 里加载桥（关键一步）

MCP server 只负责发命令；真正执行命令的是 ANSA 主线程里的 `plugin.py`。需要让 ANSA 启动时加载 `scripts/ansa_mcp_sum_autoload.py`。

编辑 `ANSA_TRANSL.py`（GUI 模式才加载，带 `-b` 批处理时跳过）：

```python
import ansa
try:
    args = ansa.session.ProgramArguments()
    _gui = '-b' not in [str(a) for a in args]
except Exception:
    _gui = True

if _gui:
    try:
        import ansa_mcp_sum_autoload   # 见下方“路径说明”
    except Exception as exc:
        print("[ANSA_TRANSL] ansa-mcp-sum autoload failed: %s" % exc)
```

**路径说明**：`ansa_mcp_sum_autoload.py` 顶部已写死两件事，若项目位置不同请改：

```python
os.environ.setdefault("ANSA_MCP_SUM_HOME", r"C:\Users\Admin\.ansa-mcp-sum")
PROJECT_SRC = r"D:\ansa-mcp-sum\src"     # 加进 sys.path 后才能 import ansa_mcp_sum
```

两种加载方式（任选其一）：

- **方式 A（推荐）**：把 `D:\ansa-mcp-sum\scripts` 加进 `PYTHONPATH`，再 `import ansa_mcp_sum_autoload`。
- **方式 B**：直接在 `ANSA_TRANSL.py` 里 `ansa.ImportCode(r"D:\ansa-mcp-sum\scripts\ansa_mcp_sum_autoload.py")`。

### 加载后 ANSA 里的表现

- ANSA 工具栏多出一个 **`MCP-Sum`** 按钮组，含四个按钮：
  - **Run Once**：主线程手动处理当前队列里所有命令（点一下处理一批）。
    它直接调处理函数、绕过轮询，所以**桥死着它照样能跑** —— 别拿它当轮询正常的证据。
  - **Auto Poll**：确保有一个活着的 200ms 轮询定时器。它调的 `_init_bridge()` 与
    启动时的自动轮询是同一个函数；定时器"对象 active 但已经不再投递回调"时它会重建，
    并打印为什么这样做。
  - **Probe Caps**：一键采集本机能力清单（等价于 `ansa.ImportCode(scripts/probe_ansa_capabilities.py)`），结果写到 `<HOME>/capabilities/capabilities.json`。
  - **Stop**：停止轮询并写 `stop.flag`（同时把 `status.json` 改成 `stopped`，服务端门禁立刻生效）。
- 默认 `AUTO_START_POLL = True`，ANSA 一启动就自动轮询，无需手动点按钮。
  想改成"按需"（不常驻轮询、只靠 Run Once）：
  ```
  set ANSA_MCP_SUM_AUTO_POLL=0     # PowerShell: $env:ANSA_MCP_SUM_AUTO_POLL="0"
  ```
  但**必须和 ANSA 在同一个环境里生效**（改完要重启 ANSA）。
- 模型加载有 30s 宽限期：刚开 ANSA 还没模型时空命令会延后，避免误执行。
- 那个 `ANSA_MCP_SUM_Bridge` 小窗口**不是开关**，它只是定时器的宿主（`guitk.BCTimerCreate` 需要父窗口）。
  它出现 + 点 `Auto Poll` 跑的是同一个函数 `_init_bridge()`。
  **v1.0.7 起它的标题栏关闭按钮被隐藏**：以前点那个 X 会销毁窗口及其子定时器，留下悬空的 C++ 句柄，
  在下次自动轮询时触发**不可捕获的 native 崩溃**（`EXCEPTION 0xC0000005`，ANSA 直接消失）。
  所以它现在**关不掉** —— 这是故意的，不是 bug。真停了用 `scripts/bridge_doctor.py`（经 `Run Once` 跑）
  出诊断报告并重建。

### 控制台输出策略（为什么日志很安静）

定时器每 200ms 触发一次，**不能**每次触发都打印。现在的策略是：

| 事件 | 输出 |
|---|---|
| 启动 | 一行 `bridge loaded ... mode=AUTO` + 一行按钮/目录信息 |
| 执行了一条命令 | 一行 `executed <命令名> (queue had N, ticks=M)` |
| 什么都没发生 | 每 **15 分钟** 一行 `idle heartbeat ticks=...` |
| 轮询抛异常 | 第 1 次打一行，之后每 50 次一次（避免刷屏） |

`status.json` 同步降频：空闲时 **5 秒**才写一次（原本每 200ms 一次 = 每小时 18000 次写盘）。
状态一旦变化（收到命令 / 开始执行 / 出错 / 停止）仍然立刻落盘。

### 关于日志里的 "没有 mcp-sum 的 script"

ANSA 启动日志只对**它自己按 transl 规则找到的那个文件**打印
`Reading script from: ...ANSA_TRANSL.py as 'ANSA_TRANSL1'`；
后面这行 `NOTICE: script [...] not found` 是 ANSA 在依次探测 5 个 transl 候选路径，属正常。

`ANSA_TRANSL.py` 里的 `ansa.ImportCode(...)` 是**直接执行**目标文件，
**不会**再打印一行 "Reading script from"，所以日志里看不到 mcp-sum 的加载记录 ——
**这是正常的**，"证明它加载了"的证据就是 `[ansa-mcp-sum] bridge loaded ...` 那行。
反过来，如果只看到 `NOTICE: ... not found` 而没有任何 `[ansa-mcp-sum]` 输出，那就真的没加载，
此时检查 `ANSA_TRANSL.py` 里的 `ansa.ImportCode` 路径是否存在。

---

## 7. 端到端示例

1. 打开 ANSA GUI（已按第 6 节加载桥，看到 `MCP-Sum` 工具栏）。
2. 终端起 `ansa-mcp-sum-server`（第 4 节）。
3. MCP 客户端（如 WorkBuddy / Claude Desktop）连上 `ansa-mcp-sum`，调用工具：

```
# 打开一个模型
open_model(filepath="J:/models/demo.k")

# 统计 FACE 数量
# 参数名是 entity_type（不是 type）；返回里会带 deck 名与节点类型
count_entities(deck=0, entity_type="FACE")
    → {"deck":0,"deck_name":"NASTRAN","node_type":"GRID","type":"FACE","count":91}

# 统计节点：语义化写 "nodes" 即可，deck 会自动决定 GRID / NODE
count_entities(deck=0, entity_type="nodes")   → {"type":"GRID","count":27734}

# 质量检查会返回实际执行的 Check 名称与报告
check_intersections()                       → {"check":"base.checks.<...>.Xxx", ...}

# 网格质量（QCHECK 指标）
calc_mesh_quality(deck=0, entity_type="SHELL")
    → {"qcheck":{"skewness":..,"warping":..,"aspect":..,"total":..}}

# 曲面网格（长度 5mm）
# length 是必填；entity_ids 只在 allow_full_model=true 时才被接受（见 §9）
set_shell_mesh_params(deck=0, length=5.0)   → {"mode":"absolute","applied":true}
mesh_shells(length=5.0)                     → 在 GUI 里能看到网格生成

# 新建螺栓连接点（自动识别螺栓后落连接用）
create_connection_point(
    position=[-822.0, 531.0, 259.0],
    connection_type="Bolt_Type",
    connectivity=[{"type":"ANSAPART","id":999999},{"type":"ANSAPART","id":888888}],
)

# 保存（绝对路径）
save_model_as(filepath="J:/models/demo_meshed.k")
```

> **更正两处旧示例**：`count_entities(type=...)` 的参数名是 `entity_type`；
> `mesh_shells(length=5.0)` 想只对指定面片网格化时必须显式 `allow_full_model=true`
> 或在 GUI 里先只显示目标面片，否则会被拒绝（而不是静默把整个模型网格化）。


每一步在 ANSA 窗口里都能**直接看到结果**——这是与 tcp-bridge（`-b` 无界面）最大的区别。

---

## 8. 工具清单（命令类型 = 工具名，共 78 个 = 65 派发 + 13 服务端辅助）

> 全部走文件 IPC，在 ANSA GUI 内执行。括号内为 `tools_impl` 中的实现函数。

### A. 会话与文件 I/O（10）
`ping_ansa` · `check_ansa_connection` · `open_model` · `new_model` · `save_model` · `save_model_as` · `export_nastran` · `export_lsdyna` · `export_step` · `run_python_script_in_ansa`

### B. 实体操作（18）
`count_entities` · `list_entities` · `get_entity` · `set_entity_fields` · `create_entity` · `delete_entities` · `search_entities_by_name` · `get_bounding_box` · `get_node_coordinates` · `change_element_type` · `create_part` · `create_set` · `add_to_set` · `get_model_summary` · `list_model_includes` · `calc_element_mass` · `calc_shell_area` · `calc_solid_volume`

### C. 检查与质量（9）
`check_intersections` · `check_penetrations` · `check_free_nodes` · `run_quality_check` · `count_failed_elements` · `check_geometry` · `check_sharp_edges` · `check_rigid_dependencies` · `calc_mesh_quality`

### D. 网格（5）
`mesh_shells` · `mesh_volume` · `set_shell_mesh_params` · `delete_mesh` · `run_batch_mesh`

### E. 连接（4）
`apply_connectors` · `check_connections` · `list_connectors` · `create_connection_point`

> `list_connectors` 查的实体类型是 `CONNECTOR_ENTITY`（旧代码写 `"CONNECTION"`，
> 永远匹配不到任何实体，所以这个工具一直返回空）。
> `create_connection_point` 是本次新增：底层是文档确认的
> `connections.CreateConnectionPoint(type, position, id, connectivity)`，
> 支持 `Bolt_Type` / `SpotweldPoint_Type` / `GumDrop_Type` / `Rivet_Type` / `Screw_Type`。

### F. 显示控制（5）
`show_only` · `show_also` · `hide` · `near` · `neighb`

### G. 脚本执行与预检（2）
`execute_script` · `validate_script`（v1.0.4；见 §4.7）

### H. 原 ansa-mcp 保留（12）
`get_capabilities` · `get_model_info` · `geometry_inventory` · `import_file` · `list_faces` · `face_properties` · `delete_faces` · `restore_model` · `save_model_as_ws` · `check_mesh_quality` · `surface_mesh` · `export_solver_deck`

> A–H 合计 **65 个派发工具**，收敛到 **64 种命令类型**（`import_file` 与 `restore_model` 共用其一）。

### 服务端-only helper（13，不向 ANSA 派发命令，属正常）

- **跨会话记忆（3）**：`recall` · `remember` · `remember_note`（见 §4.5）
- **内嵌 ANSA API 文档（4）**：`ansa_api_doc_search` · `ansa_api_doc_lookup` · `ansa_api_doc_modules` · `ansa_api_doc_categories`（见 §4.7）
- **其它（6）**：`ping` · `select_faces_by_query` · `preview_selection` · `autosave_model` · `batch_benchmark_test` · `read_last_log`

> **更正旧版两处错误**：`check_ansa_connection` 与 `restore_model` 现在**会向 ANSA 派发命令**
> （分别复用 `ping` 与 `import_file` 命令类型），所以它们已归入 A / H，不再是服务端-only。

---

## 9. `save_model_as` vs `save_model_as_ws`

两个工具都能存模型，区别在路径处理：

| 工具 | 路径语义 | 用途 |
|------|----------|------|
| `save_model_as(filepath)` | **绝对路径**，直接存到你给的位置 | 迁移自 tcp-bridge，最通用 |
| `save_model_as_ws(output_path)` | 相对路径会落到 `ANSA_MCP_SUM_HOME` 下（workspace 校验） | 原 ansa-mcp 风格，防写飞 |

> **更正**：`save_model_as` 的参数名是 `filepath`（旧文档写 `path`）。
> 实现层现在同时接受 `path` / `filepath` / `output_path`，所以两种写法都能用，
> 但推荐按上表用。
>
> `autosave_model` 现在走 `save_model_as_ws`：之前它调 `save_model_as(path, silent=True)`，
> 而那个函数只收一个参数，**每次调用都必然 TypeError**。

---

## 10. 事实层：`ansa_api.py`

新增模块 `src/ansa_mcp_sum/ansa_api.py`，是"ANSA 到底有什么 API"的唯一事实来源。
其中每个名字都对照过 ANSA 25.1.4 的 Python API 索引（5892 个函数）核对。

三件事：

1. **`VERIFIED_API`** —— 已核对存在的函数及其**真实签名**（86 条）。
   例如 `base.DeleteEntity(entities, force=False, compress=True) -> int`（0 = 成功），
   `base.CalcShellArea(entity) -> float`（收实体，不是 id）。
2. **`REJECTED_API`** —— 已确认**不存在**的名字及其替代写法（33 条）。
   例如 `mesh.MeshShell` → 用 `mesh.CreateBestMesh()`；
   `base.GetFailedEntitiesCount` → 用 `base.CalculateOffElements()`。
   **不要再把它们加回来。**
3. **`DECK_NODE_TYPE`** —— deck 到节点类型字符串的映射。
   NASTRAN 用 `GRID`，LSDYNA/ABAQUS 用 `NODE`。用错了**不报错、只返回 0**。

调用规约：`api.resolve("base.DeleteEntity")` 在函数不存在时抛 `ApiMissing`，
由 `plugin._call_tool` 转成 `error_type: "ApiMissing"` 的错误响应。
**禁止再用 `getattr(mod, "Fn", None)` 兜底** —— 那会把"函数不存在"变成
操作执行到一半时的 `TypeError: 'NoneType' object is not callable`。

### 导入方式也是事实的一部分（`IMPORT_STYLE`）

ANSA 把子模块注册成 `ansa` 模块的**属性**，`ansa` 本身不是 package：

```python
import ansa.constants        # ✗ ModuleNotFoundError: 'ansa' is not a package
from ansa import constants   # ✓ 正确（ANSA 官方示例都这么写）
```

另外 `ImportCode` **只存在于顶层 `ansa`**（`ansa.ImportCode`），
没有 `ansa.session.ImportCode` —— 这正是早期探测脚本误判的一处。
顶层 `ansa` 在 25.1.4 只有 8 个 callable：
`CompileScript / ImportCode / PybImport / ReadingFile / ScriptCurrentDir / ScriptHomeDir / ScriptUserDir / mergeToImporter`。

### 返回值约定不统一（最容易写反的地方）

| 函数族 | 成功值 |
|---|---|
| `base.Open` / `base.Save` / `base.SaveAs` / `base.SetANSAdefaultsValues` / `base.Clear` | **0** |
| `base.OutputNastran` / `OutputLSDyna` / `OutputAnsys` / `OutputAbaqus` | **1** |
| `mesh.Mesh` / `mesh.CreateBestMesh` 等 | **1** |
| `base.DeleteEntity` | **0**（且收实体，不是 `(deck,type,id)`） |

写导出工具时最容易踩：按 `ret == 0` 判断成功，会让**每一次成功导出都带上一条警告**。

### 校验脚本

```bash
PYTHONPATH=src python scripts/regression_check.py       # 208 项离线断言，全绿
PYTHONPATH=src python scripts/audit_command_keys.py     # 服务端参数 ↔ 插件消费 契约
PYTHONPATH=src python scripts/audit_ansa_api_usage.py   # 源码里的 ANSA 调用 ↔ 事实层（扫描 8 文件）
PYTHONPATH=src python scripts/audit_ansa_api_usage.py --all   # 连历史探测脚本一起查
```

三个脚本都**不需要开 ANSA**，退出码非 0 表示有问题，可直接接进 CI。
第三个脚本按 AST 抽取所有 `base.* / mesh.* / connections.* / guitk.*` 调用，
凡是命中 `REJECTED_API`（已核实不存在）的立即失败。

### 能力清单（`capabilities.json`）

在 ANSA 里点 **MCP-Sum → Probe Caps**（或 `ansa.ImportCode(<probe 路径>)`）生成，
服务端用 `load_capabilities()` 读取 ✓ 并按 `probe_version` 判断是否过期：

- `missing_functions` —— 工具需要、但本机**没有**的 API；
- `unexpectedly_present` —— 我们已列为"不存在"、但本机**居然有**的 API（说明事实层过期）；
- `open_questions` —— 文档也无法定论的名字（如按实体隐藏/显示的 API）；
- `import_style` —— 每个模块**实测用哪种导入方式才成功**；
- `requirements_met` —— 一个布尔值，服务端据此判断能否信任静态工具表。

> 旧版（probe 1.0）用单一 `importlib` 策略，把 `ansa.constants` 和
> `ansa.base.checks.*` 误判成"不可用"。**1.0 的清单不可信**，服务端会返回
> `stale: true / action: reprobe`，请重新生成。



一般优先用 `save_model_as(path=...)` 指定绝对路径。

---

## 11. 排错

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| **ANSA 突然整个消失（无报错、无崩溃日志）** | v1.0.7 之前：点过桥窗口的 **X**，窗口连同子定时器被销毁，留下悬空的 C++ 定时器句柄；下次自动轮询解引用它 → **不可捕获的 native 崩溃**（`EXCEPTION 0xC0000005`）。`base.Undo` 本机不可用，模型改动也跟着丢 | 升级到 **v1.0.7**（关闭按钮已隐藏，窗口不可再被销毁）。已发生则重启 ANSA，桥会自动回来；未保存的改动回不来 |
| **桥窗口的 X 关不掉 / 标题栏少了关闭按钮** | v1.0.7 故意隐藏（防上面那条崩溃） | 不是 bug，别去绕。停轮询点工具栏 **Stop**；停 server 就结束 `ansa-mcp-sum-server` 进程 |
| **`execute_script` 返回 `data.missing`，说某函数不存在** | v1.0.4 起的**执行前预检**拦下了一个本机不存在的 ANSA 调用（多半是幻觉出来的函数名） | 这是保护，不是故障。看 `data.missing` 列出缺哪些，用 `ansa_api_doc_search` / `ansa_api_doc_lookup`（§4.7）查到正确名字再重跑。确认调用合法只是探测不到时才 `validate=False` |
| **工具数与文档对不上（例如探针报 73、实际 78）** | 文档或探针脚本里的旧数字没跟着版本更新；客户端连着的 server 进程还是旧的 | 数字以 `tools/list` 实际返回为准。改 `server.py` 后**必须让客户端重连 server**，否则工具数停在旧值（见 §6 末尾的进程时间比对） |
| 工具调用一直超时 | ANSA 没加载桥 / Auto Poll 没开 | 先看 `status.json` 的**年龄**，不要只看 `status` 字段 —— 轮询活着时最多 5 秒写一次盘，`running` 却超过 30 秒没更新就是定时器没在跳。命令队列里最老一条躺超过 1 分钟同理。修法见下面两行 |
| **Auto Poll 点了没反应、也不报错** | 定时器"active 但不再投递回调"，旧版 `_init_bridge()` 见到 `BCTimerIsActive()==True` 就静默返回 | 当前版本会检测"上次真正 tick 是多久前"并自动重建，且每个分支都打印原因。若仍不恢复，走下一行 |
| 上面的都试过还不行 | GUI 主线程被占住，定时器发不出回调 | 把 `scripts/bridge_doctor.py` 投进队列，然后点 **Run Once**（这条路直接调处理函数，绕开定时器）。它会打印 `_busy` / `_stop_requested` / `_poll_timer` / `BCTimerIsActive` / uptime 快照，清软闸门，并**把磁盘上最新的 autoload 脚本热重载进内存** |
| **改了 `ansa_mcp_sum_autoload.py`，行为却没变** | `ansa.ImportCode` 只在 ANSA 启动时执行一次该文件，之后**磁盘上的文件再也不会被读**；`importlib.reload(ansa_mcp_sum.plugin)` 也刷不到它（那是另一个文件） | 投 `scripts/bridge_doctor.py` 再点 **Run Once**：它把当前文件 `exec` 回它自己的命名空间（期间把 `ansa.session.defbutton` 换成空操作，避免工具栏按钮翻倍）。**不需要重启 ANSA** |
| **`Run Once` 能跑，但工具调用还是超时** | 这两件事不矛盾：`Run Once` 直接调 `plugin.ansa_mcp_process_one()`，绕过 `_busy` 守卫 | **别把 `Run Once` 成功当作轮询正常的证据**，按上面一行处理 |
| 日志只有 `NOTICE: script [...] not found`、没有 `[ansa-mcp-sum]` 输出 | `ANSA_TRANSL.py` 没加载 / 里面的 `ansa.ImportCode` 路径不对 | 确认 `C:\Users\Admin\.BETA\ANSA\version_25.1.4\ANSA_TRANSL.py` 存在且 `ImportCode` 指向的文件存在 |
| 日志里看不到"mcp-sum 的脚本被读取" | **正常现象** | `ImportCode` 不打印 `Reading script from`；有 `bridge loaded` 就说明加载成功 |
| 控制台每 5 秒一行 `heartbeat ticks=` | 旧版 autoload | 换成当前版本（心跳改为 15 分钟一行、执行命令才逐条打印） |
| 命令凭空消失 | 超过 `timeout+60s` 被 `cleanup_stale_commands()` 丢掉 | 第一现场在 `<HOME>/logs/dropped.log`，里面有 id、age、limit |
| `ansa_mcp_sum` 导入失败 | `PROJECT_SRC` 路径不对 / 没装包 | 检查 autoload 的 `PROJECT_SRC`；在 ANSA 里 `import ansa_mcp_sum` 看报错 |
| `mcp` 未安装 | venv 不对 | 用装了 `mcp` 的解释器跑 `pip install -e .` |
| 命令执行了但 GUI 没变化 | 在 `-b` 批处理模式 | 本项目只支持 GUI；`ANSA_TRANSL.py` 里 `-b` 会被跳过 |
| `stop.flag` 导致不执行 | 上次 Stop 留下的文件 | 删掉 `ANSA_MCP_SUM_HOME/stop.flag` 再点 Auto Poll |
| 刚开 ANSA 命令被延后 | 30s 模型宽限期 | 等模型加载完，或手动点 **Run Once** |
| 命令投进去了，点 `Run Once` 后 `status.json` 显示 `processed 0` + `dropped_stale ≥1` | 命令被 `cleanup_stale_commands()` 当过期丢弃（看 `logs/dropped.log` 一行 `age=… limit=…`） | 1.0.2 起过期按**命令自己的** `timeout_seconds` 算；若服务端仍在跑旧代码（内存里的插件早于文件改动），limit 会回落到 660s —— 投递后尽快点 `Run Once`，或先让 `bridge_doctor.py` 把新代码热重载进内存 |
| `capabilities` 返回 `stale: true` | 清单是旧版探测脚本生成的 | 重新点 **Probe Caps** 生成 2.0 清单 |

---

## 12. 一键清单（部署时照抄）

```powershell
# 1) 安装（受管 venv）
C:\Users\Admin\.workbuddy\binaries\python\envs\default\Scripts\pip install -e D:\ansa-mcp-sum

# 2) 在 mcp.json 加 ansa-mcp-sum server（见第 5 节）

# 3) ANSA_TRANSL.py 加载 autoload（见第 6 节，GUI 模式）

# 4) 启动 ANSA GUI（看到 MCP-Sum 工具栏、Auto Poll 已自动跑）
# 5) 终端起 server：ansa-mcp-sum-server
# 6) MCP 客户端连上 → 先 recall() 读回上次的偏好，再干活，看 GUI 实时变化
```

**开 ANSA 之前、关 ANSA 之后：`recall()` 都有效**（记忆不走桥）。修完东西顺手
`remember_note(...)` 记一句，下一个会话的自己会谢你。
