# MODULES.md — `src/ansa_mcp_sum/` 各模块职责

10 个文件，共 **6,877** 行。核心是一件事：**把"ANSA 里能干什么"和"外面能命令什么"分开，中间只隔一层磁盘文件。**

v1.0.2 第二批新增三个模块（`audit.py` / `knowledge.py` / `memory.py`）。它们的共同点是
**都不碰 ANSA，也不参与命令派发**：审计是观察者，知识是数据，记忆是偏好。
放在这个包里而不是塞进 `server.py`，是因为它们各自有独立的失败模式 ——
日志写不进去、知识条目和事实层不一致、偏好文件损坏 —— 这些都不该和 MCP 层纠缠在一起。

---

## 1. 先看依赖关系

```
┌─ 外部进程：MCP server ────────────────────────────────────┐
│                                                           │
│   server.py    1747 行     73 tool / 5 resource           │
│      │ import                                             │
│      ├── config.py    106 行 ┐                            │
│      ├── ipc.py       170 行 ┘← 双栖共享层（两边各持一份）│
│      ├── audit.py     274 行 ← 读历史（写是插件的活）      │
│      ├── knowledge.py 630 行 ← pitfalls / workflows 数据   │
│      └── memory.py    216 行 ← prefs / notes               │
└──────┬────────────────────────────────────────────────────┘
       │
       ═══ 磁盘文件即队列：commands/ → results/ ═══   ← 唯一会合点
       │
┌──────┴────────────────────────────────────────────────────┐
│ ANSA GUI 进程内                                            │
│                                                           │
│   plugin.py    1573 行     HANDLERS[63] + 状态/心跳 + 审计 │
│      │ import                                             │
│      ├── config.py / ipc.py / audit.py   ← 同一套代码      │
│      │                                                    │
│      ▼                                                    │
│   tools_impl.py 1304 行    49 个工具实现                   │
│      │ import                                             │
│      ▼                                                    │
│   ansa_api.py    621 行    事实层 + deck 语义              │
└───────────────────────────────────────────────────────────┘

__init__.py  14 行 —— 只放 __version__，无依赖，两个进程都 import
```

**三个要点：**

1. `server.py` 与 `plugin.py` 是**并列关系**，不是上下游 —— 它们之间没有 import，
   只隔着一个目录里的 JSON 文件。
2. `server.py` **完全不 import** `ansa_api` / `tools_impl` / `plugin`（见 §4 硬性边界）。
3. `audit.py` 被两边 import，但**只有插件写**。两个进程共用一个日志文件、又没有锁，
   一条被撕成两半的 JSONL 比缺一条更糟（见 §3.8）。

---

## 2. 运行时归属：谁在哪个进程里跑

| 模块 | 外部 MCP server 进程 | ANSA GUI 进程内 | 说明 |
|---|---|---|---|
| `server.py` | ✅ 唯一入口 | ❌ 永不 | 由 MCP 客户端以子进程启动 |
| `config.py` | ✅ | ✅ | **双栖**，两边各持一份实例 |
| `ipc.py` | ✅ | ✅ | **双栖**，同一套原子读写 |
| `audit.py` | ✅ 读 | ✅ 读 + **写** | 日志是单写者：只有 ANSA 侧知道真实耗时与模型路径变化 |
| `knowledge.py` | ✅ | ❌ | 纯数据，无副作用 |
| `memory.py` | ✅ | ❌ | 只有 server 侧读写偏好与笔记 |
| `plugin.py` | ❌ | ✅ | 由 `ANSA_TRANSL.py` → autoload 脚本加载 |
| `tools_impl.py` | ❌ | ✅ | 被 plugin.py 连带 import |
| `ansa_api.py` | ❌ | ✅ | 被 tools_impl.py 连带 import |
| `__init__.py` | ✅ | ✅ | 一行版本号 |

⚠️ **两个进程不共享内存。** 它们靠 `ANSA_MCP_SUM_HOME` 指向**同一个目录**来"会合"——
一边写 `commands/`，另一边读；一边写 `results/`，另一边读。
两边都是模块级 `CONFIG = AnsaMcpConfig.from_env()`，所以环境变量必须在**各自进程启动时**就设好，
中途改环境变量对已启动的进程无效。

⚠️ `audit.CONFIG` / `memory.CONFIG` 也是**模块级**的，同样是各自进程一份。
这带来一个测试上的便利：回归里把 `audit.CONFIG` 临时指到一个写不进去的路径，
就能验证"审计写失败会返回 False 并被计数"，而不必真的把磁盘弄坏。

---

## 3. 逐个模块

### 3.1 `__init__.py` — 版本号唯一来源

只放一个 `__version__ = "1.0.2"`，外加一段说明为什么这里是**唯一**的声明处。

谁都需要它，所以它不能依赖任何东西。`server.py` 和 `plugin.py` 都从它取版本号写进各自的输出
（`status.json` 与 `ping` 的返回），便于事后对齐"跑的是哪一份代码"。
`pyproject.toml` 也通过 `dynamic = ["version"]` + `attr = "ansa_mcp_sum.__version__"` 读同一个值。

> 已修（v1.0.2）：此前 `pyproject.toml` 里另有一份 `version = "0.1.0"`，两处独立声明会漂移 ——
> 实测 editable 安装的 `dist-info` 长期停在 `0.1.0`，从 `status.json` 根本看不出跑的是哪一版。见 README §10.1。

### 3.2 `config.py` — 配置与目录

把环境变量收成一个 frozen dataclass `AnsaMcpConfig`，并统一给出所有路径属性。

**没有业务逻辑，只有"配置从哪来、目录在哪"。**

| 成员 | 作用 |
|---|---|
| `from_env()` | 读 4 个环境变量，给默认值 |
| `home` / `commands_dir` / `results_dir` / `runs_dir` / `logs_dir` / `scripts_dir` / `artifacts_dir` / `status_file` / `stop_file` | 路径属性，纯拼接 |
| `ensure_dirs()` | 幂等建目录 |

> `artifacts_dir`（`<HOME>/artifacts/`）是 v1.0.2 新增的：HOME 根目录只放桥的状态
> （`status.json`、`commands/`、`results/`、`runs/`、`logs/`），实验脚本的临时产物一律进这里。
> 此前 `_proc_list.txt` / `geom_ids_before.json` / `geomfix_trace.json` 直接躺在根目录，
> 与 `status.json` 混在一起，分不清哪些是桥真正拥有的文件。

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `ANSA_MCP_SUM_HOME` | `~/.ansa-mcp-sum` | 命令/结果/日志根目录（两侧靠它会合） |
| `ANSA_MCP_TIMEOUT_SECONDS` | `600` | 单条命令等待上限；**插件侧的丢弃阈值由它推导** |
| `ANSA_MCP_HEARTBEAT_STALE_SECONDS` | `2147483647` | 心跳老化窗口（近乎无限，按需桥不靠心跳） |
| `ANSA_MCP_MAX_SCRIPT_CHARS` | `65536` | `run_python_script_in_ansa` 的脚本体上限 |

### 3.3 `ipc.py` — 170 行

**传输层。本文件与原版 `D:\ansa-mcp` 逐字节相同 —— 刻意不动。**
它是整个项目里唯一没被改过的模块，因为"磁盘文件当队列"这个设计本身没有错。

纯 stdlib，无内部依赖，不做任何业务判断。

| 分组 | 函数 | 作用 |
|---|---|---|
| 原子写 | `atomic_write_text` / `atomic_write_json` | 先写 `.tmp`，再 `os.replace()` —— 读者永远看不到半个文件 |
| 读 | `read_json` | 顺带校验 JSON 根必须是 object |
| 路径 | `command_path` / `result_path` / `make_run_dir` | 命令与结果的文件名约定；每次调用建一个 run 目录留档 |
| 守卫 | `ensure_under_workspace` | 把路径限制在 HOME 下，防写飞 |
| 响应 | `success_response` / `error_response` | 全项目统一的返回结构（`ok` / `data` / `artifacts` / `logs` / `warnings` / `error`） |
| 等待 | `wait_for_result` | 50ms 轮询结果文件，超时抛 `TimeoutError` |

> 改动这一层会让两侧同时受影响 —— 这也是它至今零改动的原因。

### 3.4 `ansa_api.py` — 725 行 ★ 事实层

**回答一个问题：ANSA 25.1.4 到底有什么 API，签名是什么。**

原版没有这一层：函数名散落在各 handler 里，靠 `getattr(mod, "Fn", None)` 试探，
缺失时得到一个 `None`，直到**破坏性步骤跑完之后**才爆 `TypeError: 'NoneType' object is not callable`。

| 数据 / 函数 | 规模 | 作用 |
|---|---|---|
| `VERIFIED_API` | **83 条** | 已核实存在的 API，每条带签名与返回值语义 |
| `REJECTED_API` | **33 条** | 已确认**不存在**的名字，每条带替代方案（`base.RemoveHoles → base.FillHoleGeom(...)`） |
| `IMPORT_STYLE` | — | 记录 `from ansa import base` 才可用；`import ansa.base` 报 `'ansa' is not a package` |
| `ANSA_TOP_LEVEL_CALLABLES` | 8 个 | `ansa` 顶层确实只有这些 |
| `ApiMissing` | — | 显式异常（**不返回默认值**，缺失就必须失败） |
| `resolve(path)` / `has(path)` | — | 唯一的取函数入口，缺失即抛，`hint` 取自 `REJECTED_API` |
| `DECK_NODE_TYPE` / `DECK_AGNOSTIC_TYPES` | — | deck → 实体类型映射（`NASTRAN: "GRID"` vs `LSDYNA: "NODE"`） |
| `deck_name` / `deck_id` / `normalize_deck` / `node_type_for` | — | deck 语义换算 |
| `count` / `collect` / `resolve_entities` | — | deck 感知的实体访问 |
| `count_nodes` / `off_elements` / `element_mass` / `run_mesh_quality_check` | — | 4 个高阶动作封装（把"多次 API 调用 + 语义判断"合成一次调用） |

**它的价值不在记录本身，而在于可审计**：`scripts/audit_ansa_api_usage.py --strict`
扫描 `plugin.py` / `tools_impl.py` 的每一个 ANSA 调用，命中 `REJECTED_API` 即退出码非 0。
"这个函数不存在"于是从**运行期崩溃**变成了**静态检查能拦住**的事。

### 3.5 `tools_impl.py` — 1,324 行

**49 个工具的实现**（`HANDLERS` 的另外 14 个键由 `plugin.py` 自己实现或转调，见 §3.6）。
每个函数签名统一为 `fn(command: dict) -> dict`，返回**纯 JSON 可序列化**的字典。

分组（按工具族）：

| 组 | 代表函数 |
|---|---|
| 会话 / 文件 I/O | `open_model` `new_model` `save_model` `save_model_as` `export_nastran` `export_lsdyna` `export_step` `run_python_script_in_ansa` |
| 实体操作 | `count_entities` `list_entities` `get_entity` `set_entity_fields` `create_entity` `delete_entities` `search_entities_by_name` `create_part` `create_set` `add_to_set` |
| 测量 | `get_bounding_box` `get_node_coordinates` `calc_element_mass` `calc_shell_area` `calc_solid_volume` |
| 检查 / 质量 | `check_intersections` `check_penetrations` `check_free_nodes` `run_quality_check` `count_failed_elements` `check_geometry` `check_sharp_edges` `check_rigid_dependencies` `calc_mesh_quality` |
| 网格 | `set_shell_mesh_params` `mesh_shells` `mesh_volume` `delete_mesh` `run_batch_mesh` |
| 连接 | `apply_connectors` `check_connections` `list_connectors` `create_connection_point` |
| 显示 | `show_only` `show_also` `hide` `near` `neighb` |

内部的私有辅助（不对外）：

| 辅助 | 作用 |
|---|---|
| `_jsonable` / `_json_safe` | 把 ANSA 返回的任意对象洗成 JSON 安全值（NaN/Inf → null） |
| `_deck` / `_ctx` / `_resolve_entity_type` | deck 归一与类型名解析 |
| `_count` / `_count_safe` / `_collect` | deck 感知的计数与收集 |
| `_card_values` | 取卡片字段（**必须显式传字段元组**，见 README §7.6） |
| `_path_arg` | 同时接受 `path` / `filepath` / `output_path` 三种键名 |
| `_export` | 导出类工具的公共出口 |
| `_check_groups` / `_find_check` / `_run_check` | 在 `base.checks.*` 里按关键词定位检查对象并执行 |
| `_find_api` / `_discover_selection_api` | 关键词式 API 发现（用于官方文档未覆盖的场景） |
| `_set_visibility` / `_visibility_unsupported` | 可见性批操作；**不支持时明确报错而非静默无效** |

**结果契约（最重要的一条）**：返回的 dict 只有在**不带** `error` / `errors` 且 `ok != False` 时才算成功。
这条契约由 `plugin._call_tool` 强制执行 —— 因为迁移过来的工具是**靠返回值报失败**的
（`{"opened": False, "error": "..."}`），旧写法只找 raise，会把每次真失败都报成 `ok: true`。

### 3.6 `plugin.py` — 1,591 行

**ANSA 进程内的派发器。** 由 autoload 脚本 import，200ms 轮询 `commands/`，执行完写 `results/`。

五个职责：

**① 状态与心跳**

| 符号 | 作用 |
|---|---|
| `BRIDGE` | 8 字段状态机：`bridge_state`(idle/executing/stopping/stopped) / `current_command_id` / `last_command_result` / `last_error` / `processed_count` / `dropped_stale` … |
| `write_status()` | 写 `status.json`，并入 `BRIDGE` + `queue_depth` |
| `write_idle_status()` | 空闲心跳，**5 秒节流**（否则 200ms 一跳 = 18,000 次/小时写盘） |
| `_safe_current_context()` | 当前 deck → `deck_name` + `node_type` + 模型路径 + `model_loaded` |

**② 队列清理**

`cleanup_stale_commands()` —— 丢弃超过 `timeout + 60` 的命令，**每次丢弃写 `logs/dropped.log`**。
（原版是硬编码 120s 且静默 `unlink`，客户端会毫无线索地干等到超时。）

**③ 原版直系的 13 个 handler（手写实现）**

`handle_ping` / `handle_execute_script` / `handle_get_capabilities` / `handle_get_model_info` /
`handle_geometry_inventory` / `handle_import_file` / `handle_list_faces` / `handle_face_properties` /
`handle_delete_faces` / `handle_save_model_as_ws` / `handle_check_mesh_quality` /
`handle_surface_mesh` / `handle_export_solver_deck`

这些从原版继承，各自直接构造 `success_response` / `error_response`，
因此内部保留了一批自有辅助（`_face_geometry_info`、`_model_entity_counts`、`_surface_mesh_faces` …）。

> 原版的 `save_model_as` 在这里被**改名**为 `handle_save_model_as_ws`（保留 workspace 校验），
> 腾出的 `save_model_as` 这个名字交给了迁移版 —— 所以 `HANDLERS` 里"原版块"实际占 **14 个键**，
> 14 + 49 = **63**。

**④ 49 个迁移 handler（薄包装）**

```python
def handle_open_model(command):
    return _call_tool(tools_impl.open_model, command, "open_model")
```

配合 `_call_tool` / `_result_failed` / `FAILURE_KEYS`：捕获 `ApiMissing` 与一般异常，
把 `tools_impl` 的返回值包装成统一响应，并**识别返回式失败**。

**⑤ 主循环**

| 函数 | 作用 |
|---|---|
| `ansa_mcp_process_one()` | 处理队列里**一条**命令；更新 `BRIDGE`；`finally` 里删除命令文件 |
| `ansa_mcp_loop()` | 阻塞式轮询（`stop.flag` 退出） |
| `ansa_mcp_stop()` | 停轮询 **+ 写 `status="stopped"`**（只写 flag 会让服务端门禁继续放行） |
| `HANDLERS` | 63 个命令类型的注册表 —— **唯一的派发入口** |

### 3.7 `server.py` — 1,754 行

**MCP server 本体。它从不直接接触 ANSA**，只做三件事：判心跳、写命令文件、轮询结果文件。

**依赖约束（硬性）**：本文件**不 import** `ansa` / `ansa_api` / `tools_impl` / `plugin`。
所以它能在没装 ANSA 的机器上正常启动、正常完成 MCP 握手，
把问题报成 `PluginNotRunning`（而不是在 import 阶段就崩掉）。

| 分组 | 符号 | 作用 |
|---|---|---|
| 心跳门禁 | `read_status` / `status_is_fresh` / `plugin_ready_error` | 发命令前先检查 `status.json`；桥停了就拒发并给出行动指引 |
| 命令通道 | `send_command(type, **kwargs)` | **全部 63 个派发工具的唯一出口**：写 `cmd_<id>.json` → 等结果 → 落 `run_dir` |
| 能力清单 | `load_capabilities()` | 读 `capabilities.json`；版本不符返回 `stale: true / action: reprobe` |
| 路径守卫 | `_validated_workspace_path` / `_validated_existing_workspace_path` | 只作用于 `save_model_as_ws` / `export_solver_deck` / `delete_faces(save_as)` 三处 |
| 选面逻辑 | `_select_faces_from_properties` / `_vector_value` / `_point_distance` / `_normalized_query` | 筛选与排序在服务端算；**原始面数据仍来自一次 `face_properties` 派发** |
| 批量测试 | `_staged_benchmark_files` / `_write_batch_report` | `batch_benchmark_test` 的报告生成 |
| 工具定义 | `tool_*` × 70 | **模块级普通函数**，签名即 MCP 的 JSON Schema |
| MCP 注册 | `@mcp.tool()` × 70 / `@mcp.resource` × 2 | 只在 `mcp` 包可用时执行 |
| 入口 | `main()` | `mcp.run()`（stdio） |

**为什么工具是两层命名**（`tool_ping()` 与 `ping()`）：

```python
if FastMCP is not None:          # mcp 包缺失时整块跳过
    mcp = FastMCP("ansa-mcp-server")

    @mcp.tool()
    def ping() -> str:
        return tool_ping()
```

`tool_*` 是纯函数，不依赖 `mcp` 包 → **`regression_check.py` 可以直接 import 并断言它们**，
无需安装 MCP SDK、也无需启动 server。装饰器那层只是把签名暴露给客户端。

**7 个"服务端-only"工具** —— 它们**没有自己的命令类型**，而是在 server 进程内做纯计算、
或复用已有命令类型：

| 工具 | 实现方式 |
|---|---|
| `select_faces_by_query` | 派发一次 `face_properties`，然后在**服务端**排序筛选（选面逻辑不在 ANSA 侧） |
| `preview_selection` | 复用 `select_faces_by_query`，只算"将要删哪些 id"，**不改模型**，返回 `requires_confirmation` |
| `read_last_log` | 纯读最近一次 run 目录 |
| `check_ansa_connection` | 复用 `ping` 命令 |
| `restore_model` | 复用 `import_file`（mode=open） |
| `autosave_model` | 复用 `save_model_as_ws`（转调已派发的那条） |
| `batch_benchmark_test` | 编排多次 `import_file`，再生成 JSON/CSV 报告 |

**2 个 resource**：`ansa://status`（原始 status.json）、`ansa://capabilities`（蒸馏后的能力报告）。

> `tool_autosave_model` 的 docstring 里留了一条原版 bug 的完整记录，值得当样例看：
> ① 原版调用 `tool_save_model_as(output_path, silent=True)`，而那个函数只接受一个位置参数
> → **每次调用都 `TypeError`**；
> ② 用的是相对路径 + 不校验的那个版本，而 `base.SaveAs` 把相对路径解析到 **ANSA 的 CWD**，
> 不是 `ANSA_MCP_HOME`。
> 改走带 workspace 校验的 `tool_save_model_as_ws` 后，两处一起修好。

### 3.8 `audit.py` — 302 行 运行时审计

一行一条 JSON 写进 `logs/audit.jsonl`。**唯一的写者是 `plugin.py`**，因为只有 ANSA 侧知道两件
客户端拿不到的事：

* **真实耗时** —— 客户端只知道"我等了多久"，一次超时就让这个数失去意义；
* **模型路径的前后变化** —— 客户端看不见这条命令有没有换掉当前打开的文件。

| 成员 | 作用 |
|---|---|
| `record_command(...)` | 命令主记录：`id` / `type` / `args_hash` / `deck` / `model_before`→`model_after` / `duration_s` / `ok` / `error_type` |
| `record_dropped(...)` | 队列里被当过期丢掉的命令（含超龄多少秒）——回答"我发的那条命令呢" |
| `record_bridge(event, detail)` | `started` / `stopped` 生命周期标记，让日志里的空档有意义 |
| `read_recent(limit)` | 从当前文件 + 轮转文件倒着取，**跳过被撕开的末行而不是抛异常** |
| `summarize(limit)` | 给 `ansa://memory` 与 `read_last_log` 用的摘要（命令数/失败数/最慢耗时/按类型计数） |
| `args_fingerprint(command)` | payload 的 sha1 前 12 位，`HASHED_KEYS`（目前只有 `script`）替换为自身指纹 + 长度 |

两条不妥协的设计：

1. **绝不因为日志写失败而让命令失败**，但也**绝不静默** —— `append_event` 返回 `False`，
   `plugin._audit()` 把它累加进 `BRIDGE["audit_write_errors"]`，最终出现在 `status.json` 里。
2. **大 payload 不落原文**。一次 `execute_script` 上限 64 KB，原样写进去会让审计日志变成脚本的
   第二份副本。存指纹 + 字符数，既保持"是不是同一个调用"可判定，又不泄露内容。

### 3.9 `knowledge.py` — 664 行 踩坑登记表与实测流水线

`README.md` §7 的机器可读版本。**纯数据 + 两个 JSON 序列化函数，零依赖、零副作用。**

| 数据 | 内容 | 对应 resource |
|---|---|---|
| `PITFALLS` | 11 条 × `{id, severity, symptom, cause, correct_usage, guard, must_resolve, ref}` | `ansa://pitfalls` |
| `WORKFLOWS` | 5 段流水线 × `{steps[], cost, risk, script}`，每步带真实 API 与验收标准 | `ansa://workflows` |
| `pitfall_brief(ids)` | 生成工具 description 用的短警示块（被 `server._with_pitfalls` 调用） | 工具描述 |

**`must_resolve` 是这张表的诚实性开关**：它断言"这些 API 确实存在"，
`regression_check.py` 会拿它去比对 `ansa_api.VERIFIED_API`。把某个名字挪进 `REJECTED_API`
会让构建失败，而不是留下一句已经过期的自信注释。

`pitfall_brief` 对**未登记的 id 是宽容的**（只返回已知项），这是刻意的：一个拼错的 id
不该让 MCP server 起不来。代价是这个错会被静默吞掉 —— 所以回归里另有一条断言，
从 `server.py` 源码里把 `@_with_pitfalls(...)` 引用的 id 抠出来，逐个比对 `pitfall_ids()`。

### 3.10 `memory.py` — 216 行 跨会话记忆

两类状态，形态不同所以分开存：

| 文件 | 形态 | 例 |
|---|---|---|
| `memory/prefs.json` | 键值，每条带 `updated` / `updated_local` / `note` / `previous_value` | `deck=1`、`standard_mesh_params="F:\\ANSA_Standard\\mesh_ftrd_3mm.ansa_mpar"` |
| `memory/notes.md` | 追加式 Markdown，每次带时间戳与可选 tag | "这个件实测壁厚 1.86–3.88 mm，所以中面取 3.0" |

| 成员 | 作用 |
|---|---|
| `set_pref` / `get_pref` / `delete_pref` | 键值读写；`set_pref` 保留 `previous_value`，覆盖可追溯 |
| `append_note` / `read_notes` | 笔记追加（整文件原子重写，不是 `open("a")` —— 两个客户端共用一个 HOME 时，交错追加会把两段笔记缝成一段读不懂的东西） |
| `snapshot(limit)` | 偏好 + 笔记 + 命令历史摘要，`ansa://memory` 与 `recall()` 空参的返回体 |

三条约束写在代码里：`MAX_KEY_CHARS=120`、`MAX_VALUE_CHARS=4000`（超了明确报错，而不是截断）、
`load_prefs()` 对损坏文件返回 `{"_error": ...}` 而**不是** `{}` ——
把一个坏掉的偏好文件当成"什么都没记住"，会让人以为是自己忘了，而不是文件坏了。

**这三个工具不经过 ANSA 桥**，因为它们要回答的正是"桥没起来的时候我上次在干什么"。

---

## 4. 三条硬性边界

| 规则 | 为什么 |
|---|---|
| `server.py` 不 import `ansa` / `ansa_api` / `tools_impl` / `plugin` | MCP 侧要能在无 ANSA 的机器上跑；ANSA API 只在 ANSA 进程内调用 |
| ANSA API 调用只出现在 `ansa_api.py` / `tools_impl.py` / `plugin.py`（src 包内） | 便于 `audit_ansa_api_usage.py --strict` 全量扫描。`audit.py` / `knowledge.py` / `memory.py` 已纳入扫描范围，目前各自 0 次调用 —— 这是被检查的断言，不是假设 |
| `ipc.py` / `config.py` 不含业务判断 | 两侧共用，改一处影响两边 |

---

## 5. 加一个新工具，要动哪些文件

按顺序改 3 个文件（外加 1 个可选）：

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `tools_impl.py` | 写 `def my_tool(command: dict) -> dict:`，用 `api.resolve()` 取函数，失败就 return 带 `error` 的 dict |
| 2 | `plugin.py` | ① 加 `def handle_my_tool(command): return _call_tool(tools_impl.my_tool, command, "my_tool")` ② 在 `HANDLERS` 里注册 `"my_tool": handle_my_tool` |
| 3 | `server.py` | ① 加 `def tool_my_tool(...) -> str:` ② 在 `FastMCP` 块里加 `@mcp.tool()` 包装的 `my_tool()` |
| 4 | （可选）`ansa_api.py` | 若用到未登记的 ANSA 函数，先加进 `VERIFIED_API` —— 否则第 3 道门会拦 |

然后跑三道门（不需要开 ANSA）：

```bash
export PYTHONPATH=src
python scripts/regression_check.py                 # 行为断言
python scripts/audit_command_keys.py               # 服务端参数 ↔ 插件消费
python scripts/audit_ansa_api_usage.py --strict    # 源码调用 ↔ 事实层
```

`audit_command_keys.py` 会检查：**服务端发的每个参数键，插件侧是否真的消费**。
参数名打错会在这里被拦住 —— 而不是等真机跑起来才发现命令静默用了默认值。
