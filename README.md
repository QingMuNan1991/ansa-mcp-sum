# ansa-mcp-sum

**在可见的 ANSA GUI 上，用 MCP 驱动 78 个具名工具、5 个资源（当前 v1.0.7）。**

把 `ansa-mcp` 的**文件 IPC 传输层**（GUI 模式、模型操作实时可见）与 `ansa-tcp-bridge` 的
**49 个工具**合并成一个 MCP server。合并后共 **78 个工具**：65 个向 ANSA 派发命令
（收敛到 **64 种命令类型**），13 个是服务端辅助函数——含 3 个不依赖桥的记忆工具、
4 个内嵌 ANSA API 文档工具（v1.0.4 起，见 §5.6）。
外加一层**执行前预检**：`validate_script` 与 `execute_script(validate=True)` 会在脚本真正跑起来
之前，就拦下"引用了本机不存在的 ANSA 函数"的调用。

> Combined ANSA MCP bridge = ansa-mcp's GUI file-IPC transport + the 49 tools from
> ansa-tcp-bridge, exposed as 78 named MCP tools (65 dispatched + 13 server-side).

* 面向部署的操作手册 → **[USAGE.md](USAGE.md)**（逐步安装、配置、排错表）
* 本文档偏重**架构、测试与实测沉淀**——即"这个项目到底验证过什么、哪些坑是真的踩过"。

---

## 1. 它解决什么问题

| 项目 | 传输层 | 能看到 ANSA GUI | 工具数 |
|---|---|---|---|
| `ansa-tcp-bridge` | TCP / IAP（`-b` 批处理） | ❌ | 49 |
| **`ansa-mcp-sum`** | **文件 IPC 命令队列（GUI）** | ✅ | **78** |

一句话：**用 ansa-mcp 的传输层，把 tcp-bridge 的工具全部跑在 GUI 模式下**。
每一步操作（建网格、抽中面、跑检查）都能在 ANSA 窗口里直接看到。

---

## 2. 架构：MCP 层是外壳，文件 IPC 才是真正的执行路径

```
MCP 客户端 (LLM / mcp_cli.py)
   │  stdio JSON-RPC
   ▼
ansa-mcp-sum-server  (src/ansa_mcp_sum/server.py, FastMCP)
   │  send_command(type, **kwargs)          ← 全部 65 个派发工具的唯一出口
   │  ├─ plugin_ready_error()  读 status.json 判心跳，不新鲜就拒发
   │  ├─ atomic_write_json(commands/cmd_<id>.json)
   │  └─ wait_for_result(results/<id>.json)
   ▼
ANSA_MCP_SUM_HOME/commands/          ← 磁盘文件即队列
   │  ANSA GUI 主线程 guitk.BCTimerCreate 每 200 ms 轮询
   ▼
ANSA GUI 内 plugin.py  HANDLERS[<type>]  →  tools_impl.<fn>(command)
   │  调用 base.* / mesh.* / connections.*
   ▼
ANSA_MCP_SUM_HOME/results/<command_id>.json   →  server 读回并返回
```

**关键认识：`server.py` 从不直接接触 ANSA。** 它只做三件事——判心跳、写命令文件、轮询结果文件。
真正的执行发生在 ANSA 主线程里。

由此推出一个非常实用的结论：**MCP 层断了 ≠ 桥坏了。**

| 维度 | MCP 通道 | 文件通道（`scripts/mcp_cli.py`） |
|---|---|---|
| 本质 | 本地子进程 JSON-RPC（stdio） | 磁盘收发文件 |
| 能力面 | 78 个带 schema 的工具 | 65 个派发命令（64 种类型），手写 `type` + 参数 |
| 参数校验 | JSON Schema 拦一道 | **无** |
| 路径白名单 | `_validated_workspace_path()` | **无** |
| 心跳门控 | 有 | **无**（可 `--no-wait` 盲投，下次轮询执行） |
| 断了的症状 | 工具消失、报 `Not connected` | **无感知，照常通** |

排障时这就是个二分法：MCP 不通时先 `mcp_cli.py ping`——**通则问题在 MCP 层（客户端握手 /
server 进程 / 配置）；不通则问题在桥或 ANSA 侧**（看 `status.json` 时间戳是否推进、
`commands/` 是否积压）。

代价也要清楚：文件通道少掉 schema 校验、路径白名单、心跳门控三层保护，
第三条尤其危险——**ANSA 没开也会返回 `queued`，容易误判成"已执行"**。
所以它只用于排障与盲投，日常仍走 MCP。

---

## 3. 目录结构

```
src/ansa_mcp_sum/            共 7,550 行（另有 10.4 MB 内嵌 API 索引）
  __init__.py     (40)    __version__ 单一声明（当前 1.0.7，带完整版本史注释）
  config.py       (106)   环境变量 → AnsaMcpConfig        · 双栖（ANSA 内外都 import）
  ipc.py          (171)   原子写命令文件 / 轮询结果文件      · 双栖
  ansa_api.py     (751)  ★ 事实层：ANSA 到底有什么 API（VERIFIED 86 / REJECTED 33 / OPTIONAL 3）
  tools_impl.py   (1324)  49 个 ANSA 侧实现（在 ANSA 解释器里跑）
  plugin.py       (1832)  ANSA 侧派发器：HANDLERS[64 种命令] + 状态/心跳 + 审计落盘
  audit.py        (302)  ★ 运行时审计：logs/audit.jsonl，一行一条命令，带大小轮转
  knowledge.py    (664)  ★ 踩坑登记表 + 实测流水线（ansa://pitfalls / ansa://workflows 的数据源）
  memory.py       (216)  ★ 跨会话记忆：memory/prefs.json + memory/notes.md
  api_doc.py      (306)  ★ 内嵌 ANSA API 文档检索（v1.0.4：search / lookup / list_modules / list_categories）
  server.py       (1853)  MCP server：78 个 @mcp.tool + 5 个 resource
  ansa_api_index.json (10.4 MB)  ★ 内嵌 API 索引：5892 个函数 / ANSA v25.1.4（v1.0.6 起随包分发）
scripts/
  ansa_mcp_sum_autoload.py  (446)  ★ ANSA GUI 自加载：4 个工具栏按钮 + 200ms 轮询定时器
  mcp_cli.py                (107)  绕过 MCP 层的文件通道 CLI
  check_mcp_sum_health.py   (262)  MCP 通道逐环自检（含"进程是否跑的是当前源码"）
  probe_mcp_sum_server.py   (249)  走线的会话探针：initialize + tools/list + resources/read
  probe_ansa_capabilities.py(687)  真机能力探测（需在 ANSA 内跑）→ capabilities.json
  regression_check.py      (1306)  离线回归：208 项断言，全绿
  audit_command_keys.py     (179)  离线审计：服务端参数 ↔ 插件消费 契约
  audit_ansa_api_usage.py   (210)  离线审计：源码调用 ↔ 事实层（扫描 8 文件）
  bridge_doctor.py          (227)  ★ 桥诊断+热重载：经 Run Once 跑，不改文件也能让新代码进内存（含 RELOAD_MARKER 单点声明）
  s2_diag / s3_geomcheck / s4_standards / s5_midsurf / s6_verify   端到端样例流水线
                        （被 knowledge.py 的 ansa://workflows 资源引用，是它的证据留档，故保留）
```

> `scripts/` 曾累积 64 个一次性探索脚本（`s7`–`s56` 的 Map Block / 碎面修复 / 桥崩溃排查线，
> 以及早期 `probe_*` / `count_*` / `discovery` 临时探针）。2026-09-13 已清理，只保留上表
> 14 个**承载运行与门禁**的文件（见 §10.27）。

> 每个模块扮演什么角色、在哪个进程里运行、依赖谁、加新工具要改哪几个文件
> —— 见 **[MODULES.md](MODULES.md)**。

---

## 4. 快速开始

完整步骤见 **[USAGE.md](USAGE.md)**，这里只列最小路径。

```powershell
# 1) 装进受管 venv（mcp 已锁 1.29.1）
C:\Users\Admin\.workbuddy\binaries\python\envs\default\Scripts\pip install -e D:\ansa-mcp-sum
```

```jsonc
// 2) mcp.json（WorkBuddy: C:\Users\Admin\.workbuddy\mcp.json；CodeBuddy 是另一个文件，配一边另一边不生效）
{
  "mcpServers": {
    "ansa-mcp-sum": {
      "command": "C:\\Users\\Admin\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\ansa-mcp-sum-server.exe",
      "args": [],
      "env": { "ANSA_MCP_SUM_HOME": "C:\\Users\\Admin\\.ansa-mcp-sum" }
    }
  }
}
```

```python
# 3) ANSA_TRANSL.py（GUI 模式才加载；带 -b 时跳过）
import ansa
try:
    _gui = '-b' not in [str(a) for a in ansa.session.ProgramArguments()]
except Exception:
    _gui = True
if _gui:
    # 注意：scripts/ 不是 Python 包，不能 import；ImportCode 直接执行该文件
    ansa.ImportCode(r'D:\ansa-mcp-sum\scripts\ansa_mcp_sum_autoload.py')
```

```powershell
# 4) 启动 ANSA GUI（看到 MCP-Sum 工具栏）→ 另开终端起 server
ansa-mcp-sum-server
```

**加载成功的证据**是 ANSA 控制台出现
`[ansa-mcp-sum] bridge loaded. home=... buttons=[MCP-Sum] mode=AUTO`。
日志里看不到 "Reading script from: ...autoload.py" 是**正常的**——`ansa.ImportCode` 直接执行、
不打印该 banner；同理 `NOTICE: script [...] not found` 是 ANSA 在探测它自己的 5 个 transl 候选路径。

### 工具栏按钮

| 按钮 | 作用 |
|---|---|
| **Run Once** | 主线程手动处理当前队列（点一次处理一批） |
| **Auto Poll** | 启动后台轮询（默认开启） |
| **Probe Caps** | 采集本机能力清单 → `<HOME>/capabilities/capabilities.json` |
| **Stop** | 停轮询 + 写 `stop.flag` + `status.json` 置 `stopped`（服务端门禁立即生效） |

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ANSA_MCP_SUM_HOME` | `~/.ansa-mcp-sum` | 命令/结果目录根 |
| `ANSA_MCP_SUM_AUTO_POLL` | `1` | 设 `0` = 只加载按钮、不起定时器（按需模式）；**须与 ANSA 同环境生效** |
| `ANSA_MCP_TIMEOUT_SECONDS` | `600` | 单条命令等待 ANSA 回结果的超时 |
| `ANSA_MCP_HEARTBEAT_STALE_SECONDS` | `2147483647` | 按需桥不靠心跳，基本永不过期 |
| `ANSA_MCP_MAX_SCRIPT_CHARS` | `65536` | `run_python_script_in_ansa` 的脚本大小上限 |

其它运行时常量：轮询 `POLL_MS=200`；模型宽限期 `MODEL_GRACE_SECONDS=30`（刚开 ANSA 尚无模型时命令延后）；
空闲心跳 `HEARTBEAT_EVERY_TICKS=4500`（= 15 分钟）；过期命令丢弃阈值 `STALE_COMMAND_AGE_SECONDS = timeout + 60`。

---

## 5. 工具、资源与运行时状态（78 个工具 / 5 个资源）

`server.py` 注册 **78 个 `@mcp.tool`**，其中 **65 个会写命令文件派发到 ANSA**（收敛到 **64 种命令类型**，
因为 `import_file` 与 `restore_model` 共用同一个类型），**13 个是服务端-only**：

| 类别 | 工具 | 说明 |
|---|---|---|
| 跨会话记忆（3） | `recall` · `remember` · `remember_note` | 刻意不走桥：**桥没起来的时候，恰恰是最需要"我上次在干什么"的时候**（§5.4） |
| 内嵌 ANSA API 文档（4） | `ansa_api_doc_search` · `ansa_api_doc_lookup` · `ansa_api_doc_modules` · `ansa_api_doc_categories` | 读包内 10.4 MB 索引，**既不需要 ANSA 在线，也不依赖另外那个 `ansa-api` MCP**（§5.6） |
| 服务端 helper（6） | `ping` · `select_faces_by_query` · `preview_selection` · `autosave_model` · `batch_benchmark_test` · `read_last_log` | 不真派发；`autosave_model` 转调已派发的 `save_model_as_ws` |

> 注：`check_ansa_connection` 与 `restore_model` 现在**会派发命令**（分别复用 `ping` 与 `import_file`
> 命令类型），所以它们不算服务端-only —— 旧版文档把它们列进这一栏是过时的。

<details>
<summary><b>65 个派发工具的分组清单</b></summary>

**A. 会话与文件 I/O（10）**
`ping_ansa` · `check_ansa_connection` · `open_model` · `new_model` · `save_model` · `save_model_as` ·
`export_nastran` · `export_lsdyna` · `export_step` · `run_python_script_in_ansa`

**B. 实体操作（18）**
`count_entities` · `list_entities` · `get_entity` · `set_entity_fields` · `create_entity` ·
`delete_entities` · `search_entities_by_name` · `get_bounding_box` · `get_node_coordinates` ·
`change_element_type` · `create_part` · `create_set` · `add_to_set` · `get_model_summary` ·
`list_model_includes` · `calc_element_mass` · `calc_shell_area` · `calc_solid_volume`

**C. 检查与质量（9）**
`check_intersections` · `check_penetrations` · `check_free_nodes` · `run_quality_check` ·
`count_failed_elements` · `check_geometry` · `check_sharp_edges` · `check_rigid_dependencies` ·
`calc_mesh_quality`

**D. 网格（5）**
`mesh_shells` · `mesh_volume` · `set_shell_mesh_params` · `delete_mesh` · `run_batch_mesh`

**E. 连接（4）**
`apply_connectors` · `check_connections` · `list_connectors` · `create_connection_point`

**F. 显示控制（5）**
`show_only` · `show_also` · `hide` · `near` · `neighb`

**G. 脚本执行与预检（2）**
`execute_script` · `validate_script`（v1.0.4；见 §5.6）

**H. 原 ansa-mcp 保留（12）**
`get_capabilities` · `get_model_info` · `geometry_inventory` · `import_file` · `list_faces` ·
`face_properties` · `delete_faces` · `restore_model` · `save_model_as_ws` · `check_mesh_quality` ·
`surface_mesh` · `export_solver_deck`

</details>

### 5.1 每个工具都带 MCP 注解

78 个工具全部声明了 `readOnlyHint` / `destructiveHint` / `idempotentHint`，客户端据此决定
要不要二次确认、能不能并发。分组规则：

| 注解常量 | 数量 | 含义 | 代表工具 |
|---|---|---|---|
| `READ_ONLY` | 39 | 只读，不碰模型也不写文件 | `count_entities` · `calc_mesh_quality` · `recall` · `ansa_api_doc_*` |
| `VIEW_ONLY` | 5 | 只改可见性，重复执行结果相同 | `show_only` · `near` · `hide` |
| `WRITE` | 12 | 写输出，幂等，不丢既有数据 | `save_model_as_ws` · `export_*` · `remember` |
| `WRITE_ADDITIVE` | 5 | 新增，跑两次就有两个 | `create_set` · `remember_note` · `autosave_model` |
| `DESTRUCTIVE` | 17 | 删除 / 替换既有状态 | `mesh_shells` · `delete_entities` · `execute_script` |

> 39 + 5 + 12 + 5 + 17 = 78。v1.0.4 新增的 5 个工具（4 个 API 文档 + `validate_script`）全是 `READ_ONLY`。

两个刻意的例外，宁可保守不可误导：

* **`check_penetrations`** 标 `DESTRUCTIVE` 而非只读 —— 兄弟检查项（`check_free_nodes` 等）都是
  纯读，但它带 `auto_fix=True`，真会改模型。
* **`apply_connectors` / `mesh_volume` / `run_batch_mesh`** 都是破坏性的，即使它们在实现上
  "只是生成"：生成物覆盖了原本为空的位置，撤回代价和删除一样高。

`mcp` 装得极简、没有 `ToolAnnotations` 时，注解退化为 `None`（FastMCP 的默认值），
**78 个工具照常注册** —— 不会因为一个元数据字段拒绝启动。

### 5.2 两个同名但语义不同的工具

| 工具 | 路径语义 |
|---|---|
| `save_model_as(filepath)` | **绝对路径**，直接存（迁移自 tcp-bridge） |
| `save_model_as_ws(output_path)` | 相对路径落到 `ANSA_MCP_SUM_HOME` 下（workspace 校验，防写飞） |

> 实现层两者都接受 `path` / `filepath` / `output_path`，但推荐按上表用。

### 5.3 资源：把仓库里的知识接到模型眼前

MCP 的 resource 是唯一能让客户端**主动取**知识的通道。本项目暴露 5 个：

| URI | 内容 | 为什么存在 |
|---|---|---|
| `ansa://status` | 插件原始状态 JSON | 排障 |
| `ansa://capabilities` | 本机 ANSA 能力清单（probe 产出） | 判"这个函数本机到底有没有" |
| `ansa://pitfalls` | **11 条实测踩坑**（症状 / 原因 / 正确写法 / 判据） | 见下 |
| `ansa://workflows` | **5 段实测流水线**（诊断 → 体检 → 载标准 → 中面+网格 → 验证） | 每步带真实 API 与验收标准 |
| `ansa://memory` | 偏好 + 笔记 + 命令历史摘要 | 跨会话续接（§5.4） |

`ansa://pitfalls` 是把 §7 的内容**重新写成数据**。动机很直白：§7 里的 10 条硬核教训
原先只存在于这份 Markdown 里，**而 MCP 客户端不会去读一个仓库的 README** ——
于是每换一个会话，模型都要重新踩一遍。BETA 官方的 AI-Assistant 用"全量文档 + Python API
的 RAG 索引"解决这件事；单机工具链的等价做法就是把同一份知识写成机器可读的资源。

登记表里每条都带 `must_resolve`：它断言"这些 API 确实存在"。回归门会拿这张表去比对
`ansa_api.VERIFIED_API`，**把某个名字挪进 `REJECTED_API` 会让构建失败**，而不是留下一句
已经过期的自信注释。

资源只在你主动取的时候才到达，所以最要命的几条还同时**写进了对应工具的 description**：

```
execute_script / run_python_script_in_ansa / get_entity / get_bounding_box /
get_node_coordinates / check_geometry / calc_mesh_quality / count_failed_elements /
count_entities / export_nastran / export_lsdyna / export_step      （共 12 个）
```

工具描述是客户端每次都会下发的东西，这一条路比 resource 可靠得多。

> 实现注记：这里最初写成 `"""doc""" + knowledge.pitfall_brief([...])`，**注解是错的** ——
> Python 只在首个语句是**裸字符串字面量**时才设 `__doc__`，表达式会被静默忽略，
> 于是这些工具的 description 变成了空字符串（比不加注解更糟）。现在改用
> `@_with_pitfalls(...)` 装饰器（跑在 `@mcp.tool` 之前），并有一条回归断言专门守这个形态。

### 5.4 跨会话记忆

两类状态，分工明确（都在 `<HOME>/memory/` 下）：

| 文件 | 形态 | 放什么 |
|---|---|---|
| `prefs.json` | 键值，带 `updated` / `note` / `previous_value` | 本项目的 deck、标准 `.ansa_mpar` 路径、目标边长、质量基线 |
| `notes.md` | 追加式 Markdown | 装不进键的观察（"这个件壁厚 1.86–3.88，所以取 3.0"） |

三个工具：`remember(key, value, note)` / `recall(key="")` / `remember_note(text, tag)`。
`recall()` 空参返回全量快照（偏好 + 笔记 + 历史摘要），传键返回值，键不存在时返回
`NotFound` **并列出已经存在的键** —— 而不是一个空成功。

当前 `<HOME>/memory/prefs.json` 里已经有 7 条本项目实测事实（deck=1、3.0 目标边长、
标准文件路径、98.9% 中面基线…），下一次会话 `recall()` 就能拿到。

### 5.5 运行时审计：`logs/audit.jsonl`

**为什么需要它**：`scripts/audit_*.py` 是**离线代码审计**（扫源码），跟"运行时谁动了我的模型"
毫无关系。§7.6 那条已知崩溃路径——ANSA 进程直接消失——当时**任何地方都没有记录**：
`results/` 是空的、`status.json` 时间戳停在起始时刻、系统事件日志什么都不写。事后连
"当时在跑什么"都答不上来。

现在一条命令一行 JSON：

```json
{"ts":1757...,"event":"command","id":"a1b2c3d4e5f6","type":"mesh_shells",
 "args_hash":"sha1:9c1f...","deck":1,"model_before":"C:/.../Casting_initial.ansa",
 "model_after":"C:/.../Casting_initial_midmesh_3mm.ansa","model_changed":true,
 "duration_s":54.2,"ok":true,"error_type":null,"pid":12964}
```

三个设计决定：

* **只由插件写**（ANSA 进程内）。另外两件只有 ANSA 侧知道的事：**真实耗时**（客户端只知道
  自己等了多久，一次超时就让这个数失去意义），以及**模型路径的前后变化**。
  服务端不重复写同一个文件 —— 两个写者共用一个文件，一条被撕成两半的 JSONL 比缺一条更糟。
  客户端侧的事实（超时、校验拒绝）本来就有家：`runs/<stamp>-<id>/`。
* **大 payload 只存指纹**。一次 `execute_script` 最大 64 KB，原样写进去会把审计日志变成
  脚本的第二份副本。改为 `args_hash`（sha1 前 12 位）+ `payload_chars`，既保持"同一调用"
  可判定，又不落原文。
* **写失败要能看见**。审计是观察者，绝不能让它把一条成功的命令变成失败；但也绝不能不吭声 ——
  写失败会累加到 `status.json` 的 `audit_write_errors`。**一份安静地什么都不写的日志，
  比没有日志更危险，因为它是被信任的。**

另有两条被记录的**非命令事件**：`dropped_stale`（队列里被当过期丢掉的命令，含超龄多少秒）
和 `started` / `stopped`（生命周期标记，让日志里的空档有意义 —— 在它之后的命令属于另一个
ANSA 会话）。日志按 4 MB 轮转，保留 2 份（`audit.1.jsonl` / `audit.2.jsonl`），无外部 cron。
读取时**容忍被撕开的最后一行**：跳过而不是抛异常。

`read_last_log` 现在会在 `logs.audit` 里同时返回历史摘要（命令数、失败数、最慢耗时、
按类型的计数），所以"那条命令到底跑没跑"是个能回答的问题。

### 5.6 内嵌 ANSA API 文档 + 执行前预检（v1.0.4 起）

写 ANSA 脚本时最贵的一类错，是**调用了一个本机根本不存在的函数**：`GetEntityCardValues` 对未知
字段静默返回空字典、`CollectEntities` 用错类型只返回 0、`getattr(mod, "Fn", None)` 兜底则把错误
推迟到操作跑到一半时的 `TypeError`。以前只能靠人在真机上一个个试。

v1.0.4 把**另外那个 `ansa-api` MCP 的检索能力整个搬了进来**，并加了一层**执行前预检**：

| 能力 | 工具 | 说明 |
|---|---|---|
| 模糊检索 | `ansa_api_doc_search(query, module, category, top_n)` | 三层：关键词（模块名/函数名加权）→ 描述+签名模糊 → 外部 txt 兜底。返回签名、参数、返回值、示例 |
| 精确查函数 | `ansa_api_doc_lookup(function_name, module)` | 按精确名（大小写不敏感，支持 `mesh.Mesh` 这类限定名）取完整文档 |
| 列模块 | `ansa_api_doc_modules()` | 各模块函数计数 |
| 列类别 | `ansa_api_doc_categories()` | 各类别函数计数 |
| **预检** | `validate_script(script)` / `execute_script(validate=True)` | 解析脚本里每个 `ansa.<module>.<func>` 与 `<module>.<func>`，对照**活着的** ANSA build，返回缺失调用列表——**在真正跑起来之前**就拦下幻觉 |

**索引来源**：`ansa_api_index.json`（10.4 MB，5892 个函数，ANSA v25.1.4）**随包分发**
（`package-data`），所以项目自包含、可直接分享，**不需要另外装 `ansa_tools`**。解析顺序是
`ANSA_API_INDEX_PATH` 环境变量 → 包内副本 → `ansa_tools` 资源（仅作向后兼容兜底）。
`api_doc.py` 是纯逻辑模块：没有 FastMCP 对象、import 无副作用，只暴露
`search / lookup / list_modules / list_categories` 返回 markdown。

> **预检默认开启**：`execute_script` 的签名是 `execute_script(script, timeout_seconds=None, validate=True)`。
> 只有当调用确实合法、但用 `dir(ansa)` 探测不到时，才显式 `validate=False` 绕过。
> v1.0.5 修好了拒绝路径：`ipc.error_response` 现在接受可选 `data`，所以一个引用了不存在函数的脚本
> 会返回**结构化的** `data.missing / data.checked`，而不是在构造错误响应时抛 `TypeError` 崩掉。

---

## 6. 测试与验证

### 6.1 三道离线门（不需要开 ANSA，退出码非 0 即失败，可直接接 CI）

```bash
export PYTHONPATH=src
python scripts/regression_check.py                 # 208 项断言
python scripts/audit_command_keys.py               # 服务端参数 ↔ 插件消费 契约
python scripts/audit_ansa_api_usage.py --strict    # 源码调用 ↔ 事实层（命中 REJECTED 即失败）
```

**实测结果（2026-09-13，当前 v1.0.7）**：

| 门 | 结果 |
|---|---|
| `regression_check.py` | **208/208 PASS**，exit 0 |
| `audit_command_keys.py` | 命令类型 **64** · 注册处理器 **64** · **可达 64/64**，无未消费 kwarg，exit 0 |
| `audit_ansa_api_usage.py --strict` | 扫描 **8** 文件 · ANSA 调用 **55**（已核实 55 / optional 0 / **未登记 0**）· **REJECTED 命中 0**，exit 0 |

> **v1.0.7 收尾：三条滞后断言已修**（实现一直是对的，过期的是断言；判据见 §10.26）。
> 曾让门变红的四处：`check("__version__ is the shipped 1.0.3", ...)`（写死字面量，每次发版必假失败）、
> `check("all 73 tools are registered", ...)`（真实 78）、
> `check("... callable(namespace.get("_teardown_timer"))")`（v1.0.7 已改名 `_reset_bridge_refs`），
> 以及 `bridge_doctor.py` 把 `_teardown_timer` 当"新代码已生效"的**标记符号** ——
> 该处最隐蔽：改名后 doctor 会永远报 `code_is_current = False`。
> 修法：版本断言改为"X.Y.Z 形态"（不再写死）、工具数保留为**有意的计数跳线**、
> 标记符号抽成 `bridge_doctor.RELOAD_MARKER` 单点声明。
> 另把 3 个"未登记"的 ANSA 调用补进 `VERIFIED_API`：
> `guitk.BCWindowShowTitleBarButtons` 与两个 `BCEnumTitleBarButton` 常量
> （`BCMinimizeButton` / `BCMaximizeButton`）。

> 8 个被扫文件里的 `audit.py` / `knowledge.py` / `memory.py` / `api_doc.py` **预期各自
> 0 次 ANSA 调用** —— 把它们列进去，是为了让"这几个模块是纯的"变成一条被检查的断言，
> 而不是一个假设。（`api_doc.py` 于 v1.0.7 加入，见 §10.26。）

> `--verbose` 会把 `opt` 前缀标出来：那些是 `ansa_api.OPTIONAL_API` 里登记过的、随构建而异的
> 属性（`base.GetCurrentFileName` 等）。它们算"已知"而非"未登记"——
> 否则一份永远挂着例外的报告，会让一个真的拼写错误混在噪音里过关。

这三道门守的是本项目最容易犯的三类错：**参数名写错**、**服务端发了插件不认的键**、
**调用了一个根本不存在的 ANSA 函数**。它们跑得很快，改完代码先跑一遍再上真机。

### 6.2 真机能力探测（需要 ANSA 在跑）

```bash
python scripts/mcp_cli.py run_python_script_in_ansa --no-wait \
    --script-file scripts/probe_ansa_capabilities.py --kw function_name=main
```

或在 ANSA 里点 **MCP-Sum → Probe Caps**。产物：`<HOME>/capabilities/capabilities.json`。

**本机实测摘要（ANSA 25.1.4，probe 2.0）**：

| 项 | 值 |
|---|---|
| `requirements_met` | **true** |
| `verified_function_count` | **80 / 80** |
| `missing_functions` / `unavailable_modules` / `probe_errors` | `[]` / `[]` / `[]` |
| `unexpectedly_present` | `[]`（事实层没有过期） |
| `gui_available` / `undo_available` | `true` / **`false`**（`base.Undo` 本机不可用） |
| `open_questions` | 11 条：10 条 `NOT_FOUND`（如 `base.Show/Hide/SetVisibility`、`mesh.MeshVolume`），1 条 `OK`（`mesh.RemeshShells` **存在**） |
| `functions` 分类计数 | base 54 · REJECTED 17 · mesh 12 · CAND 11 · connections 7 · guitk 3 · ansa 2 · session 2 |

> `import_style` 字段记录**每个模块实测用哪种导入方式才成功**。旧版 probe 1.0 只用单一
> `importlib` 策略，把 `ansa.constants`、`ansa.base.checks.*` 误判成"不可用"——**1.0 的清单不可信**，
> 服务端会返回 `stale: true / action: reprobe`。

### 6.3 通道健康自检

```bash
python scripts/check_mcp_sum_health.py            # 逐环定位断点
python scripts/check_mcp_sum_health.py --deep     # 额外做一次 stdio 握手（约 12 秒）
```

覆盖 8 环：`mcp.json` 条目 → 解释器 → `PYTHONPATH` → 模块可导入 → **server 进程存在** →
**进程是否跑的是当前源码** → 桥 `status.json` → 文件通道 ping。

第 6 环是 2026-09-13 真机联调后补的。前 4 环都是**另起一个解释器**验证当前源码，
回答的是"源码里有什么"，不是"客户端正连着的那个进程有什么"——两者可以不一致：
实测源文件 13:05 改完、server 进程 12:57 起来，于是**那次新加的工具在客户端全部表现为"调不到"**
（当时的现场是 73 个工具的源码只被客户端看到 70 个）。判据是**进程启动时间 vs `server.py` 的 mtime**。

`--deep` 现在走的是**完整会话**而不只是 `initialize`（`probe_mcp_sum_server.py` 于第二批重写）：
`initialize` → `tools/list`（**78 个**、注解齐全、无空描述、**12 个**带踩坑提示）→
`resources/list`（5 个 URI）→ `resources/read`（逐个读回并解析）→
`tools/call remember` / `recall`。

为什么要走到读回来为止：**注册了但读不出来的资源等于没加**，而这一点在本地单元测试里看不到 ——
本地是直接调 Python 函数，不走 JSON-RPC 那一层。加 `--home <path>` 可换一个隔离状态目录，
不动真实 HOME。实测 **18/18** 通过（v1.0.7 收尾前这里是 17/18：唯一失败项是探针脚本里
仍写着旧值 `tools/list 返回 73 个工具`，实际 78 个 —— 同样是断言滞后，见 §10.26；
最后一项是探针擦掉自己写入的标记 —— 默认 HOME 就是真实那个，健康检查不该在操作者的记忆里留垃圾）。

```bash
python scripts/probe_mcp_sum_server.py --home "$TEMP/ansamcp-probe"   # 隔离 HOME，全走线
```

### 6.4 一个真实的端到端样例

`scripts/s2`–`s6` 是一次完整作业的留档（铸件 `Casting_initial.ansa`，1101 个几何面、
无实体无网格，包围盒 501×50.5×122.3 mm）：

| 步骤 | 脚本 | 实测结果 |
|---|---|---|
| 1 模型诊断 | `s2_diag.py` | `is_solid_desc=1`，shell 占比 **0.0%** → 确需抽中面；壁厚 1.86–3.88 mm |
| 2 几何检查 | `s3_geomcheck.py` | 20 项检查；自算拓扑 **2933/2933 边全被 2 面共享**（完全流形、0 自由边、0 重复面） |
| 3 载入标准 | `s4_standards.py` | `target_element_length` average→**3.**，质量标准 77/80 精确匹配 |
| 4 抽中面 | `s5_midsurf.py` | `MidSurfAuto(1.0, faces, False, False, 3.0)` → **54.2 s / 13,510 壳单元** |
| 5 成果验证 | `s6_verify.py` | p50 2.74 / p90 **3.01 mm**；中面质量分 **98.9%**；QCHECK **1.273**；文件 13.98 MB 落地 |

---

## 7. 实测沉淀：这些坑是真踩过的

### 7.1 返回值约定不统一（写判定时的头号坑）

| 函数族 | 成功值 |
|---|---|
| `base.Open` / `Save` / `SaveAs` / `SetANSAdefaultsValues` / `DeleteEntity` | **0** |
| `base.OutputNastran` / `OutputLSDyna` / `OutputAnsys` / `OutputAbaqus` | **1** |
| `mesh.Mesh` / `mesh.CreateBestMesh` / `mesh.*` | **1** |

按 `ret == 0` 判成功，会让**每一次成功导出都带一条警告**。逐函数真值见 `ansa_api.VERIFIED_API`。

### 7.2 `MidSurfAuto` 只认位置参数（静默失效，最阴的一个）

```python
base.MidSurfAuto(thick=1.0, faces=faces, length=3.0)   # ✗ 关键字被忽略：返回 0、耗时 0.0s、模型零变化
base.MidSurfAuto(1.0, faces, False, False, 3.0)        # ✓ 位置参数：(thick, faces, exact_middle, connect_weldings, length)
```

关键字传参**不报错**，看起来完全像成功。判成败必须看**耗时**与 **SHELL 计数**，
不能只看返回值。同类现象还有 `base.CalculateOffElements()`——返回 `{'TOTAL OFF': n, ...}`，
n 取决于当时载入了哪套质量准则；**准则全 OFF 时它恒为 0**，不代表网格更好。报这个数必须附准则状态。

### 7.3 `CheckReport.try_fix()` 不做几何修复

语义是**把报告标记为已修复**（`is_fixed`）。实测两次对照实验：

```
修复前:  status=error, 5 issues, has_fix=True
try_fix: tried=1, ok=true
修复后:  status=ok,    0 issues, has_fix=False   ← 检查"通过"了
────────────────────────────────────────────────
ID 集合差:  8821 / 9280 消失 → ID 1 / 2 出现（删掉重建，换号）
几何统计:   逐位相同（min 面积 0.007mm²、154 个 <1mm² 面、177 边面 1 个、总面积 196074.8）
```

即：**删掉问题面再重建，几何一像素没动，然后把检查状态改成 ok**。
另一条路 `删除碎面 + FillHoleGeom(..., always_produce_new_faces=False)` 同样几何守恒——
缺口形状由那几条 CONS 唯一决定，曲面延展不进去，只能新建一个形状相同的面。

结论：`ProblematicSurfaces` 报的面是**相邻面之间必要的过渡面**，不是冗余碎面。
下游中面质量分 98.9% 已达标的情况下，**强改几何是高风险低收益**。

### 7.4 事实层（`ansa_api.py`）的硬规约

| 规约 | 说明 |
|---|---|
| `import ansa.constants` → ✗ | `ansa` 不是 package。必须 `from ansa import constants` |
| `ansa.ImportCode` | 只在**顶层** `ansa` 上；没有 `ansa.session.ImportCode` |
| `DECK_NODE_TYPE` | NASTRAN→`GRID`，LSDYNA/ABAQUS→`NODE`。用错**不报错、只返回 0** |
| `CollectEntities(deck, container, type, recursive)` | 4 个位置参数；实体类型是纯字符串 |
| `base.GetEntityType(deck, entity)` | **必须 2 个参数**；参数个数错误会被误读成"类型不支持" |

**禁止再用 `getattr(mod, "Fn", None)` 兜底**——那会把"函数不存在"变成操作执行到一半时的
`TypeError: 'NoneType' object is not callable`。统一走 `api.resolve()`，缺失时抛 `ApiMissing`。

| 规约 | 说明 |
|---|---|
| `resolve(path)` | 唯一的解析入口；缺失即抛 `ApiMissing` |
| `OPTIONAL_API` | **登记表**，只装真正随构建而异的属性 |
| `resolve_optional(path)` | 只对登记过的名字返回 `None`；未登记的名字照样抛——逃生阀不能变成默认路径 |
| `current_model_path()` | 要"当前打开的文件"时用它；拿不到返回 `None`（只用于上报，不用于判定命令成败） |

> v1.0.2 之前这里有两处"规则例外"：`plugin.py` 用循环 `getattr(base, name, None)` 探测
> `GetCurrentFileName` / `CurrentFileName`，靠 audit 脚本的"探测位置"豁免才没被标记；
> `autoload` 脚本里的 `ansa.session.defbutton` / `ProgramArguments` 则一直挂在"未登记"栏。
> 现在前者收进 `OPTIONAL_API`、后者登记进 `VERIFIED_API`，**未登记数归零**。
> 规则可以带逃生阀，但不能带说不清来源的例外。

### 7.5 运维层面的两个陷阱

* **心跳老化 ≠ 桥死了。** 插件空闲时 `status.json` 按 5 秒节流写盘，时间戳不推进很正常。
  判桥活性**只能用 `mcp_cli.py ping`**。
* **过期命令会被自动丢弃。** `STALE_COMMAND_AGE_SECONDS = timeout + 60`，超龄命令在下次轮询时
  丢弃并记入 `logs/dropped.log`——所以上游崩溃后重启 ANSA，不会在开机时再撞一次同一条命令。

### 7.6 一条已知的崩溃路径（未完全定位）

对**字段不匹配的 FACE 实体批量调用 `base.GetEntityCardValues(deck, entity)`**（不传字段元组）
曾导致 ANSA 进程直接消失：无结果落盘、`status.json` 时间戳停在起始时刻、系统事件日志无崩溃记录。
把以下 4 类调用排除后，同一套脚本完整跑通且结果可复现：

`GetEntityCardValues` 无字段调用 · `CheckDescription.read_descriptions()` ·
`Check.parameters()` · `Or()/RedrawAll()` 可见性批操作

**纪律**：逐条单独试，每条之间确认 `status.json` 时间戳已推进；
`GetEntityCardValues` 永远显式传**语义匹配**的字段元组并包 `try/except`。

---

## 8. 已知限制

* **仅 GUI 模式**。`-b` 批处理下 `ANSA_TRANSL.py` 里的加载被跳过（本项目的立身基础就是 GUI）。
* **`mesh_volume` 无法按实体限定**——已验证的 API 不支持，工具会明确拒绝而不是静默网格化整个模型。
* **`ProblematicSurfaces` 无法自动修复**（见 §7.3），`has_fix=True` 只代表 GUI 里有交互式手段。
* **本机 `base.Undo` 不可用**（`undo_available: false`），脚本没有撤销退路——改模型的脚本务必谨慎。
* **路径白名单（`_validated_workspace_path`）只作用于三个工具**：`save_model_as_ws`、
  `export_solver_deck`、以及 `delete_faces` 的可选 `save_as` 参数；`save_model_as(filepath)`
  与文件通道（`mcp_cli.py`）都**没有**路径校验。
* **能力清单是快照**。换 ANSA 版本或装了新模块后需重新 Probe Caps。
* 项目**未纳入版本控制**（无 git 仓库），三道离线门也还没接进 CI。
* **预检依赖"活的" ANSA**：`validate_script` / `execute_script(validate=True)` 要把脚本里的调用
  对照正在运行的 ANSA build 校验，所以**桥没起来时无法预检**（此时会被插件就绪门禁挡下）。
* **能力清单还没接进工具注册**。`load_capabilities()` 的 docstring 承诺"缺 API 的工具对模型隐藏"，
  但 **78 个工具目前全是静态注册**，清单只是 `ansa://capabilities` 的一份报告。
* **工具数断言是"计数跳线"**：`regression_check.py` 里的 `all 78 tools are registered` 是**有意的**
  字面量 —— 增删工具必须在同一次提交里改它。这是它与版本号断言的区别（后者已改为"X.Y.Z 形态"，
  不再写死），别为了"全绿"把这条也改成范围判断。

> 已不再是限制（本清单曾列出）：
> * v1.0.2 第二批：**运行时审计**已落地为 `logs/audit.jsonl`（见 §5.5）；
>   **跨会话记忆**已落地为 `remember` / `recall` / `remember_note` 与 `memory/`（见 §5.4）。
> * v1.0.7 收尾：**离线门滞后**已修（3 条回归断言 + 1 条探针断言 + `bridge_doctor` 的标记符号）；
>   **事实层未登记数**已归零（补进 3 个 guitk 名字）；`api_doc.py` 已纳入审计扫描（见 §10.26）。

---

## 10. 变更记录与路线图

### 10.1 v1.0.2 —— 审查第一批（小改动、零行为风险）

| 项 | 改动 | 验证方式 |
|---|---|---|
| 版本治理 | `__version__` 单一来源，`pyproject` 走 `dynamic`；依赖收紧为 `mcp>=1.0,<2` | 构建元数据实测 `Version: 1.0.2` / `Requires-Dist: mcp<2,>=1.0`；回归断言比对 `status.json` |
| 超时对账 | 超时 payload 补 `command_id` / `result_path` / `queue_depth` / `bridge_state` / `elapsed_s`，并明确警告"超时不取消命令" | 回归里真触发一次 0.2 s 超时并逐字段断言 |
| 工具注解 | 70 个工具全部带 `readOnlyHint` / `destructiveHint` / `idempotentHint` | 回归读 FastMCP 注册表（不是读源码文本）：无漏标、无"既只读又破坏"的矛盾 |
| 产物归位 | 新增 `<HOME>/artifacts/`；`s7` / `s10` / `s11` 三个实验脚本不再硬编码 `C:\Users\Admin\...`，也不再往 HOME 根目录丢文件 | 已把 `_proc_list.txt`、`geom_ids_before.json`、`geomfix_trace.json` 移入 `artifacts/` |
| 事实层收口 | 取消 `plugin.py` 的 `getattr` 探测，改走 `api.current_model_path()`；新增 `OPTIONAL_API` 登记表 | `audit_ansa_api_usage --strict` 未登记数 **5 → 0** |

三道门：**204/204 · 63/63 · REJECTED 0**，全部 exit 0。
旧版全量备份在 `D:\ansa-mcp-sum_backup_20260913_124913`（48 文件 / 883 K）。

### 10.15 v1.0.2 —— 审查第二批（纯新增能力，不动现有路径）

| 项 | 新增 | 验证方式 |
|---|---|---|
| 运行时审计 | `audit.py` + `logs/audit.jsonl`：一行一条命令，含 `args_hash` / `duration_s` / `model_before`→`model_after`；`dropped_stale` 与 `started`/`stopped` 同流；4 MB 轮转 | 回归在独立 HOME 里真派发命令、真丢一条过期命令，逐字段断言；另断言 4 KB 脚本原文**不落盘**、被撕开的末行不抛异常、写失败返回 `False` 并累加 `BRIDGE["audit_write_errors"]` |
| 踩坑知识 | `knowledge.py`（11 条 × 症状/原因/正确写法/判据）+ `ansa://pitfalls`、`ansa://workflows` 两个 resource | 回归断言条目字段完整、id 唯一、**每条 `must_resolve` 的 API 都在 `VERIFIED_API` 里**（把名字挪进 `REJECTED_API` 会让门失败） |
| 知识下发 | `@_with_pitfalls` 装饰器，10 个易踩坑工具的 description 里直接带正确写法 | 走线断言 73/73 有描述、10 个含踩坑段；源码形态断言禁止 `"""doc""" + brief()`（它会静默产生空 description） |
| 跨会话记忆 | `memory.py` + `memory/prefs.json` + `memory/notes.md` + `remember`/`recall`/`remember_note` + `ansa://memory` | 回归 `importlib.reload` 模拟新会话，断言值仍在；断言未知键返回 `NotFound` 且附已知键列表、空键/超大值/空笔记被拒、损坏的 prefs 被上报而非当空 |
| 通道自检补环 | `check_mcp_sum_health.py` 新增"进程启动时间 vs `server.py` mtime"比对 | 实测直接报出 `[WARN] server 进程早于 server.py 的修改时间`，并给出后果与修法 —— 此前这一步要人工比时间才看得出 |
| 走线探针 | `probe_mcp_sum_server.py` 重写为完整会话（工具 + 资源 + 工具调用），并在结束时擦掉自己写的标记 | 实测 **18/18**：73 工具、5 资源、`resources/read` 逐个解析、`remember`→`recall` 闭环、真实 HOME 不留垃圾 |
| 通道健康自检 | `check_mcp_sum_health.py` 的 deep 环节改读探针的结论行与退出码，不再 grep `"protocolVersion"` | 走线 18/18，健康自检报 `会话探针（走 JSON-RPC 真实链路）` |
| 审计范围 | `audit_ansa_api_usage.py` 的 SHIPPED 加入 3 个新模块（预期 0 次 ANSA 调用） | 扫描 4 → 7 文件，调用数仍为 52、未登记 0，证明新模块是纯的 |

工具数 70 → 73、资源 2 → 5、回归断言 91 → 178。**版本号按第一批的约定仍为 1.0.2。**
新版备份在 `D:\ansa-mcp-sum_backup_batch1_20260913_125908`（第二批改前的状态）。

### 10.16 v1.0.2 —— 首次真机联调修复（2026-09-13，ANSA 25.1.4 在线）

前两批都是离线验证的（回归 + 探针 + 离线审计）。第一次把 73 个工具接到**真正开着的 ANSA** 上跑，
暴露出三个离线门看不见的缺陷。它们的共同形态是：**问错了问题，而答案看起来像模型的属性** ——
所以每一条的判据都写在"错答案的产物"上，而不是"调用是否返回"。

| 缺陷 | 真机症状 | 根因 | 修法 |
|---|---|---|---|
| **模型路径访问器全死** | `audit.jsonl` 里 `open_model` 打开了 18 MB 模型，`model_before` / `model_after` 仍是 `null`；`status.json` 从不出现 `current_model_path` | 候选表里的 `base.GetCurrentFileName` 与 `base.CurrentFileName` 在 25.1.4 里**都不存在**（`hasattr` → False）。而源码注释写着"第一个能解析"——事实层里躺着一句未经核实的结论 | 候选首位改为 `base.DataBaseName`（实测返回当前模型路径，0.0 ms）；新增 `current_model_path_accessor()` 区分"没开模型"与"本构建没有访问器"；审计新增 `model_snapshot_error`，让后者不再和前者长得一样 |
| **NASTRAN 节点坐标读成空** | `get_bounding_box(deck=1, entity_type='GRID')` 报 "no node carried X/Y/Z card values"，而模型里有 16 381 个可见 GRID；`get_node_coordinates` 更糟 —— 每个节点都回 `[0.0, 0.0, 0.0]`，看不出是错的 | NASTRAN 的 GRID 把坐标放在 `X1/X2/X3`。`GetEntityCardValues` 对未知字段名返回**空字典而不是异常**，硬编码的 `('X','Y','Z')` 于是静默失效 | 新增 `NODE_COORD_FIELD_CANDIDATES` + `node_coord_fields()`（从真实实体自发现、按 deck 缓存）+ `read_node_xyz()`；`get_node_coordinates` 不再把缺失值兜底成 `0.0` —— "没读到"和"值就是 0"是两件事 |
| **审计残行会吃掉下一条** | 回归自身撞出来的：写一条不带尾换行的半截行之后，**下一条**事件的 JSON 被焊接到残行上，整行无法解析 —— 丢的不是残行，是新事件 | `append_event` 直接以 `"a"` 追加，不检查文件是否以换行结尾。`read_recent` 跳过残行只解决了读的一半 | 新增 `_newline_guard()`：追加前读 1 字节，上一行没写完就先补一个换行 |

真机验证（同一会话内**热重载插件**后即刻生效，未重启 ANSA）：

```
get_bounding_box(1, 'GRID')    -> ok  coord_fields=["X1","X2","X3"]  20000 点
                                  min=[-540.21, -700.04, -170.00]  max=[0.20, 1200.00, 120.00]
get_node_coordinates([1,2,3])  -> 节点 1 = [-418.06424, 244.09517, 51.38365]   （修复前是 [0,0,0]）
open_model(result.ansa)        -> 审计 model_before=initial.ansa  model_after=result.ansa  changed=True
open_model(initial.ansa)       -> 恢复用户要求的初始状态
```

回归断言 164 → **178**（新增 4 个测试函数，含"残行之后写入的事件仍可读"）。
踩坑登记表 10 → **11** 条：新增 `nastran-grid-coords-are-x1x2x3`，并挂到受影响的
`get_bounding_box` / `get_node_coordinates` 的工具描述上。

> **热重载**：`autoload` 的 `_poll_once()` 是按模块属性查表调用 `plugin.ansa_mcp_process_one()` 的，
> 所以在 ANSA 里 `importlib.reload` 这几个模块即可让插件侧改动立即生效，不必重启 ANSA。
> **但 server 侧不行** —— MCP 客户端是在自己启动时拉起 `ansa_mcp_sum.server` 子进程的，
> 改完 `server.py`（工具表、resource、记忆工具）必须让客户端重连该 server 才会看到，
> 否则工具数会停在旧值。

> 这次联调还留下两条**已确认、未修**的问题，已记入 §10.17。

### 10.18 v1.0.2 —— 桥定时器不再"假装在跑"（2026-09-13）

起因是一个问题：「点了 `Auto Poll`」和「`ANSA_MCP_SUM_Bridge` 那个小窗口」是不是一回事。
答案是一回事 —— 窗口不是开关，它只是 `guitk.BCTimerCreate(parent)` 需要的宿主，
`Auto Poll` 按钮的实现体就是 `_init_bridge()`，启动时的自动轮询调的也是它。

但顺手测出来一件更要紧的事：**那个按钮点了等于没点**。证据不是"感觉慢"，是三个可复现的数：

| 观测量 | 值 | 含义 |
|---|---|---|
| `status.json` 年龄 | **557 s** | 轮询活着时每个 tick 都会走到 `write_idle_status()`，节流只有 5 s —— 所以这不是节流 |
| `commands/` 里最老一条 | 283 s 无人取 | 队列有货、没人搬 |
| 新投 `ping_ansa` | 20 s 超时 | 端到端确认 |

根因在判据本身：

```python
# 旧：只要对象"active"就认为一切正常，而且什么都不打印
if _poll_timer is not None and guitk.BCTimerIsActive(_poll_timer):
    return True
```

`BCTimerIsActive` 描述的是**对象状态**，不是**有没有在投递回调**。定时器可以 active
却一个回调都不发（`_busy` 重入锁卡住 / GUI 主线程被模态框占住 / 被 hide），
这时 `Auto Poll` 成了"点了没反应也不报错"的按钮，而 `status.json` 一直写着 `running`。

`Run Once` 照常能用，恰恰是这件事难发现的原因 —— 它直接调 `plugin.ansa_mcp_process_one()`，
绕过 `_busy` 守卫。**所以 Run Once 成功不能当作轮询正常的证据。**

| 改动 | 内容 |
|---|---|
| 判据换成"最近有没有真的 tick 过" | 新增 `_last_tick_at`，**只在通过守卫、真正干活的 tick 里刷新**。active 但静默超过 `STALL_SECONDS = 10` → 重建，而不是信任对象状态 |
| 每个分支都必须说话 | 旧的无操作路径连 `print` 都没有。现在"已经在跑"、"命令在执行中"、"可能停摆，开始重建"都要打印 |
| `_busy` 卡住时给出可见痕迹 | 写一次 `status.json`（`poll callback re-entered while a command is still in flight`），但**不自动清** —— 长命令（一次完整质量检查约 20 min）是正常的，清了会让两条命令并发。超过 `BUSY_LIMIT_SECONDS = 1800` 才强清并告警 |
| 新增 `scripts/bridge_doctor.py` | 经 `Run Once` 跑（这条通道绕开定时器，所以桥死着也能用）。扫 `sys.modules` 反查 autoload 命名空间 —— `ansa.ImportCode` 把文件体放哪儿不由你决定，按模块私有全局名反查比猜模块名可靠。打印 `_busy` / `_stop_requested` / `_poll_timer` / `BCTimerIsActive` / uptime 快照，再强清软闸门 + 重建 |
| 健康自检第 6 环收紧 | 判据从"300 s"改成 **30 s**（理由同表：写盘节流只有 5 s），并列出三个成因；队列环节从"有几条"改成"最老一条等了多久"，≥ 60 s 判 FAIL |

顺带纠正一条**流传了很久但没依据**的说法：注释、`USAGE.md` 与技能文档里都写着
"关掉那个窗口就会静默停止轮询"。官方文档里 `guitk.constants.BCOnExitHide` 写的是
*hides the window*（`BCOnExitDestroy` 才是 destroy），而隐藏的父对象不会停掉子定时器。
这句话已从三处删掉、改为"未经验证、与官方文档相悖"。窗口的提示文字也改成只是"它承载定时器"。

> 两个 API 事实值得记下（都已进 `ansa_api.REJECTED_API`，写错会被离线审计拦下）：
> `guitk` 里**既没有 `BCTimerDestroy`，也没有 `BCDestroyWindow`**。25.1.4 的定时器只有
> `Create/Start/Stop/IsActive/ChangeInterval`，"重建定时器"只能是 `BCTimerStop` 旧的 +
> 丢引用，旧对象交给 GC。窗口则**不能销毁** —— `BCDestroy` 的文档明确要求用
> `BCWindowAccept`/`BCWindowReject` 关窗口，而 `BCWindowCreate(name, ...)` 的 `name`
> 是它 gui 数据的 xml tag、文档要求唯一；所以恢复时**复用同一个窗口**
> （`_ensure_window`）而不是重建，否则每恢复一次就多叠一个同名窗口。
> 之前 `_teardown_timer()`/`bridge_doctor.py`/能力探针里那三处 `BCDestroyWindow` 全部包在
> `try/except` 里，所以它一直"成功"地什么都没做。

回归断言 178 → **190**：新增 3 个测试函数（把一个假 `guitk` 装进 autoload 模块，再驱动状态），
覆盖"冷启动真的建了定时器 / 健康的定时器不重建 / active 但停摆必须重建 / 命令在跑不算停摆 /
`_busy` 卡死超限时清锁不抛异常 / 重入与停止时 tick 时钟必须冻结（否则停摆永远测不出来）"。

### 10.19 v1.0.2 —— 改完脚本不必重启 ANSA（2026-09-13）

**为什么必须专门解决这件事**：`ansa.ImportCode` 只在 ANSA 启动时执行一次
`scripts/ansa_mcp_sum_autoload.py`，之后代码活在 ANSA 自己的命名空间里，**磁盘上的文件
再也不会被读**。`Auto Poll` 按钮读不到它，`importlib.reload(ansa_mcp_sum.plugin)` 也读不到
它（那是另一个文件）。所以"改完脚本点一下按钮"这个直觉是错的——修好的代码在重启前一直是
文本。这也解释了本次现场：用户点了 `Auto Poll` 而行为没变。

**解法**：`bridge_doctor.py` 新增 `_reload_autoload()`，把当前文件 **`exec` 回它自己的
命名空间**。一个坑：文件里四个工具栏按钮是模块级 `@defbutton` 装饰的，直接重跑会给每个
按钮再加一份，所以 exec 期间把 `ansa.session.defbutton` 换成空操作，结束后还原。

按钮不需要重新注册也能用上新代码：按钮持有的是旧函数对象，但它们的 `__globals__` 正是被
刷新的那个命名空间，`_init_bridge` 等名字在**调用时**才查表，所以 `Auto Poll` 点击后走的
已经是新逻辑。

| 项 | 内容 |
|---|---|
| 新增 | `bridge_doctor._reload_autoload(ns)`：compile + exec（吞掉 `defbutton`），报告 `reload` / `code_is_current`；若重载后定时器仍未起（例如 `ANSA_MCP_SUM_AUTO_POLL=0`），再显式调一次 `_init_bridge()` |
| 新增 | `_ensure_window()`：窗口只创建一次，恢复时复用（见 §10.18 那条 API 事实） |
| 新增 | `_init_bridge()` 的 attach 重试：仅当"窗口本来就在"却挂不上定时器时，才丢引用重建一次；失败路径**不再**清 `_bridge_window`（那是唯一句柄，清了会造出同名第二窗口） |
| 测试 | 回归新增 `test_the_autoload_script_can_be_hot_reloaded_without_an_ansa_restart`：真实 exec 一次载荷 4 个按钮的脚本 → 重载后 `defbutton` 调用数必须不变（4 → 4）→ 再故意不带守护重跑一次证明它会变成 8（说明守护是承重的）→ 重建时定时器换新、**窗口对象不变** |
| 文档 | 本文件的计数、`USAGE.md` 排错表、技能 `ansa-python-api-25` §15.3 同步 |

结论：**不需要重启 ANSA**。点一次 `MCP-Sum > Run Once` 即可（doctor 命令在队列里），
之后 `Auto Poll` 与看门狗走的都是新代码。

### 10.20 v1.0.2 —— 命令过期按"它自己的超时"判（2026-09-13，真机事故后修）

现场：`mcp_cli.py --timeout 1800` 把 doctor 命令投进队列，用户 14:36 点了 `Run Once`，
结果 `status.json` 显示 `processed 0` + `dropped_stale 1`，`dropped.log` 里一条：

```
2026-09-13T14:36:47  dropped  244fc2b1c25c  age=832.0s  limit=660s
```

**根因**：`cleanup_stale_commands()` 用全局 `STALE_COMMAND_AGE_SECONDS`
（`CONFIG.timeout_seconds` + 60 = 660s）判过期，**完全无视命令 payload 里的
`timeout_seconds`**。而 `mcp_cli.py --timeout N` 是写进 payload 的，客户端真的会等 N 秒。
于是 `--timeout 1800` 的命令在 660s 就被删，客户端还要再空等 19 分钟 ——
现象与"桥死了"无法区分。

| 项 | 内容 |
|---|---|
| 修复 | 新增 `_command_age_limit(payload)`：`payload["timeout_seconds"] + COMMAND_EXPIRY_GRACE_SECONDS(60)`；读不到或非法时回退 `STALE_COMMAND_AGE_SECONDS` |
| 修复 | `dropped.log` / 审计里的 `limit` 改为**实际生效的** limit，不再是全局常量 |
| 测试 | 回归新增 `test_a_long_client_timeout_is_not_dropped_early`：1800s 的命令在 age=832s 必须**仍在队列**（复现事故现场）、越过自身超时后必须被收走、payload 缺失/非法时回退默认 |
| 断言 | 199 → **204** |

**教训**：任何"多久算过期"的判据，只要生产者与消费者是两端，就必须读**那一端自己声明的**
数字，不能用本端配置里的默认值代替 —— 否则默认值一旦与调用方不一致，失效方式是静默丢弃。

### 10.21 v1.0.3 —— 热重载不再"叠"桥窗口（2026-09-13，真机事故后修）

现场：`bridge_doctor` 经 `MCP-Sum > Run Once` 投进队列，因 §10.20 之前的旧
`ansa_mcp_process_one()` 不删命令文件，该命令被反复派发 82 次；每次 `bridge_doctor`
都把 `ansa_mcp_sum_autoload.py` 用 `exec(code, g)` 重新执行进 autoload 自己的
namespace。模块体里有一行 `_bridge_window = None`，于是每次重载都把**活的窗口句柄**
抹成 None，紧接着 `_init_bridge()` 以 `_bridge_window is None` 又建一个
`ANSA_MCP_SUM_Bridge` 窗口 —— 而 25.x 没有 `guitk.BCDestroyWindow`，旧窗口只是被
**遗弃**不销毁。82 次重载 → 几十个窗口叠在同一份 xml tag 下（"几十个窗口"症状）。
§10.20 的 claim 机制已堵住"重复派发"，本条目堵"重载后窗口句柄被抹掉"这一环。

| 项 | 内容 |
|---|---|
| 修复 | 窗口句柄改存 `plugin.GUI_HANDLES["bridge_window"]`（`plugin` 模块**从不被重新执行**，随进程常驻）；autoload 模块体开头改读 `plugin.GUI_HANDLES.get("bridge_window")` 而非写死 `None`；`_ensure_window()` 建窗后立即回写 `GUI_HANDLES` |
| 效果 | 任意次 `bridge_doctor` 重载、`Auto Poll` 反复点，都复用**同一个**窗口，不再堆叠 |
| 测试 | 回归新增 `test_hot_reload_preserves_the_bridge_window`：用真实的 `exec(code, g)` 重载路径，断言重载前后 `namespace["_bridge_window"] is 同一对象` 且 `GUI_HANDLES` 仍指向它 |
| 断言 | 204 → **205**（含本条目） |
| 版本 | `1.0.2` → `1.0.3`（`__init__.py` 单一来源，`pyproject` 走 `dynamic`） |

**教训**：凡是"跨热重载必须存活"的对象，绝不能放在会被 `exec` 重置的脚本模块全局里，
要放进一个从不重新执行的宿主模块（这里是 `ansa_mcp_sum.plugin` 的 `GUI_HANDLES`）。

### 10.22 v1.0.4 —— 内嵌 ANSA API 文档 + 执行前预检（2026-09-13）

把另外那个 `ansa-api` MCP 的检索能力搬进本项目，并加一层"**跑脚本之前先验函数是否存在**"的预检。

| 项 | 内容 |
|---|---|
| 新增模块 | `api_doc.py`（306 行）：纯逻辑，无 FastMCP、import 无副作用，暴露 `search` / `lookup` / `list_modules` / `list_categories` |
| 新增工具（4） | `ansa_api_doc_search` · `ansa_api_doc_lookup` · `ansa_api_doc_modules` · `ansa_api_doc_categories`，全 `READ_ONLY` |
| 新增预检 | `validate_script` + `execute_script(validate=True)`（**默认开**）：解析脚本里每个 `ansa.<module>.<func>` / `<module>.<func>`，对照**活着的** build，返回缺失调用 |
| 事实来源 | `ansa_api_index.json`（5892 函数 / ANSA v25.1.4），三层检索：关键词（模块名·函数名加权）→ 描述+签名模糊 → txt 兜底 |

工具数 73 → **78**（+5）；派发 63 → **65**（`validate_script` 也写命令文件）。
动机见 §5.6 开头：**"调用了一个本机不存在的函数"是最贵的一类脚本错误**，它要么静默返回 0、
要么在操作跑到一半时才抛 `TypeError`。

### 10.23 v1.0.5 —— 预检的拒绝路径不再崩

| 项 | 内容 |
|---|---|
| 现场 | `execute_script(validate=True)` 对一个引用了不存在函数的脚本，本应返回"缺哪些调用"，却抛 `TypeError` |
| 根因 | `ipc.error_response(...)` 的签名里**没有** `data` 关键字 |
| 修法 | `error_response` 增加可选 `data` 参数；拒绝现在返回结构化的 `data.missing` / `data.checked`，而不是在构造响应时崩掉 |

### 10.24 v1.0.6 —— 索引随包分发，项目自包含

| 项 | 内容 |
|---|---|
| 动机 | 之前 `api_doc` 读的是 `ansa_tools` 包里的索引，单装本项目的人拿不到 |
| 修法 | 把 `ansa_api_index.json`（10.4 MB）放进 `src/ansa_mcp_sum/`，并在 `pyproject.toml` 的 `package-data` 里声明，随 wheel 一起分发 |
| 解析顺序 | `ANSA_API_INDEX_PATH` 环境变量 → 包内副本 → `ansa_tools` 资源（仅作向后兼容兜底） |

### 10.25 v1.0.7 —— 桥崩溃（EXCEPTION 0xC0000005）根治（2026-09-13，真机事故后修）

现场：Auto Poll 期间 ANSA **直接段错误消失**（`EXCEPTION 0xC0000005`）。这是**不可捕获的 native
崩溃** —— Python 的 `try/except` 拦不住，`results/` 空、`status.json` 时间戳不动。两条独立的修复：

| 缺陷 | 根因 | 修法 |
|---|---|---|
| 窗口被销毁后，下一次 Auto Poll 解引用**悬空句柄** → 崩 | 用户点了桥窗口标题栏的**关闭 (X)**，窗口连同它的子定时器一起被销毁；而 25.x 没有 `guitk.BCDestroyWindow`，句柄变成悬空的 C++ 指针 | `_ensure_window()` 把标题栏的关闭按钮**隐藏**，用户再也关不掉这个窗口 |
| `guitk.BCTimerIsActive(保留的定时器句柄)` 在句柄悬空时崩 | 这同样是 native 调用，Python 层无法兜底 | `_init_bridge` **不再**对保留句柄调 `BCTimerIsActive`；定时器健康纯看 Python 侧的 `_last_tick_at` 心跳；停摆的定时器经 `_reset_bridge_refs` **只丢引用、绝不解引用**，再建一个新的 |

> 这是"离线门看不见"的典型：回归与走线探针全过，但真机上点一下窗口的 X 就崩 ——
> 因为两类断言都只在**假 `guitk`** 上跑。教训延续 §10.21：
> **跨热重载 / 跨生命周期存活的原生句柄，任何一次解引用都可能是最后一次**；
> 能用 Python 侧状态判断的，就不要去碰 native 句柄。

### 10.26 v1.0.7 收尾 —— 让离线门重新全绿（2026-09-13）

三道门 + 走线探针在 v1.0.4–v1.0.7 期间陆续转红，追下去**实现全是对的，红的是断言与审计清单**。
共五处：

| # | 位置 | 症状 | 修法 |
|---|---|---|---|
| 1 | `regression_check.py` 版本断言 | 写死 `__version__ == "1.0.3"`，每次发版必假失败 | 改为"X.Y.Z 形态"（`re.fullmatch`），不再随版本漂 |
| 2 | `regression_check.py` 工具数 | `all 73 tools`，真实 78 | 改为 78，并注明这是**有意的计数跳线**（增删工具须同步改） |
| 3 | `bridge_doctor.py`（**真缺陷，非测试**） | 把 `_teardown_timer` 当"新代码已生效"的标记；v1.0.7 改名后 doctor **恒报** `code_is_current = False` | 抽成 `bridge_doctor.RELOAD_MARKER = "_reset_bridge_refs"` 单点声明，4 处引用统一 |
| 4 | `probe_mcp_sum_server.py` | 断言 `tools/list 返回 73 个工具`（模块 docstring 里也各有一处旧值） | 改为 78 |
| 5 | `audit_ansa_api_usage.py` | 3 个 ANSA 调用未登记（`unlisted: 3`）；纯模块 `api_doc.py` 未列入扫描 | 3 个 guitk 名字补进 `VERIFIED_API`；`api_doc.py` 加入 `SHIPPED` |

第 3 条是本次唯一的**功能性**缺陷：doctor 的整个价值就是"证明新代码进了内存"，
标记符号跟着改名失配后，这个诊断工具**自己静默失真**——每次都告诉你"代码没更新"。

**验证（2026-09-13，四条全部 exit 0）**：

```bash
python scripts/regression_check.py                 # 208/208
python scripts/audit_command_keys.py               # 64/64 可达
python scripts/audit_ansa_api_usage.py --strict    # 扫描 8 文件 · 调用 55 · 已核实 55 · 未登记 0 · REJECTED 0
python scripts/probe_mcp_sum_server.py --home "$TEMP/ansamcp-probe"   # 18/18
```

事实层同时涨到 **VERIFIED 86 / REJECTED 33 / OPTIONAL 3**。

**教训**：这三类"门变红"长得一样，处理却不同 ——
①**版本号字面量**：天生会过期 → 改成形态断言；
②**函数 / 符号名**：改名会静默失效 → 抽成单点常量，并让测试断言**当前**名字；
③**计数**：留着当跳线，但必须与工具面变化同一次提交。
判据只需问一句：**"这个数字 / 名字明年还会对吗？"** 不会，就别写死。

### 10.27 `scripts/` 瘦身 78 → 14，移除陈旧 `build/`（2026-09-13）

仓库里攒了 **64 个一次性探索脚本**（`s7`–`s56` 的 Map Block / 碎面修复 / 桥崩溃排查线，
以及更早的 `discovery.py` / `count_*.py` / `probe_*` 临时探针）。它们全仓库零代码引用，
是"当时在真机上试过什么"的留档，不是运行路径。按"是否承载运行与门禁"清理：

| 层 | 数量 | 保留理由 |
|---|---|---|
| 运行 / 门禁依赖 | 9 | `autoload` 被 ANSA `ImportCode`；`probe_ansa_capabilities` 被 autoload 调；`mcp_cli` 被健康检查 `subprocess` 调；4 道门 |
| 被 shipped 代码引用 | 5 | `s2_diag / s3 / s4 / s5_midsurf / s6_verify` 写在 `knowledge.py` 里，是 `ansa://workflows` 资源的证据留档 —— 删了资源会引用幽灵文件 |

同时清掉 `scripts/__pycache__`，并把顶层 **`build/`（11 MB、12 个文件，其中 `__init__.py`
与 `ansa_api.py` 已与 `src/` 不一致）** 移出仓库：它是 setuptools 构建产物，grep 全仓库
（代码 / 文档 / 配置）无任何引用，`python -m build` 即可重新生成。

**验证**：清理后三道离线门照旧全绿（`208/208` · `64/64` · `unlisted 0`），
说明删掉的确是不承载运行的东西。顺手修了两处会变成悬空的**文档注释**：
`mcp_cli.py` 的示例 `--script-file discovery.py` → `my_script.py`；
`audit_ansa_api_usage.py` 的 `SHIPPED` 注释不再点名已删的探针。

**教训**：删"历史脚本"前先做**引用图**，别按文件名猜 —— 本例中 5 个看似可删的 `s2`–`s6`
其实被 shipped 资源引用；而看似"正式"的 `build/` 才是真正没人要的。判据不是"名字像什么"，
而是"全仓库谁在引用它"。

### 10.17 还没做（按优先级）

| 批次 | 内容 | 为什么排后面 |
|---|---|---|
| ~~第二批~~ | ~~审计日志 · pitfalls/workflows resource · 跨会话记忆~~ | **已完成**（见 §10.15） |
| 第三批 | 路径白名单收口到 `send_command` · 能力清单接线裁工具 · 队列 FIFO · `runs/`+`results/` 回收 · 单实例锁 · 三道闸进 CI | 会改行为，需要真机回归 |
| 未排期 | 超时分级（查询 30 s / 检查 120 s / 网格 900 s） | **不能单独改**：`STALE_COMMAND_AGE_SECONDS` 是按 `timeout_seconds + 60` 算的，一旦单命令超时超过它，插件会先把命令当过期丢掉 —— 正是 §7.5 踩过的那个坑。要和 staleness 一起改 |
| 第三批 | `model_changed` 目前只比**文件路径** —— `mesh_shells` 把壳从 0 变成 65 908 时它仍是 `False`。要回答"模型变了吗"得比实体计数指纹 | 改的是审计语义，要真机跑一遍改模型的操作才知道指纹够不够便宜 |
| 第三批 | 单实例锁 | **不再是理论风险**：现场实测有**两个** `ansa_mcp_sum.server` 进程跑在同一个 HOME 上 |
| 未排期 | `get_entity` 的 `fields` 默认值 | §7.6 的崩溃路径是"不传字段元组"，而工具签名里 `fields=None` 仍是默认。要么改成必填，要么在插件侧强制补一套语义字段 —— 需要真机验证补哪一套是对的 |

---

## 9. 排错

常见现象的处置见 **[USAGE.md §11 排错表](USAGE.md)**。30 秒自查顺序：

```bash
python scripts/check_mcp_sum_health.py --deep     # ① 通道逐环
python scripts/mcp_cli.py ping                     # ② 桥是否应答（绕开 MCP 层）
tasklist | findstr ansa_win64                      # ③ ANSA 进程还在吗
```

三条一起看就能定性：ping 通 → 问题在 MCP 层；ping 不通且 ANSA 进程不在 → 只是 ANSA 关了
（重启 ANSA 桥会自动回来，`ANSA_TRANSL.py` 的 `ImportCode` 还在）；ping 不通但 ANSA 在 →
查 `<HOME>/logs/audit.jsonl` 与 `status.json`。

**"我发的那条命令到底跑了没有"** —— 按顺序看这三处就够，不用猜：

```bash
tail -5 "$HOME/logs/audit.jsonl"     # ① 跑过没有、多久、成没成、模型路径变没变
ls "$HOME/results/<command_id>.json" # ② 有结果文件 = 跑完了（超时后也要来这查）
ls "$HOME/commands/"                 # ③ 还躺在这里 = 从没被取走；超龄会被记为 dropped_stale
```

> 超时**不取消** ANSA 侧的操作。重试一条改模型的命令之前，先看 ① 和 ②。
