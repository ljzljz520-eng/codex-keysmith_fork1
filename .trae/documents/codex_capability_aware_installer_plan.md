# Codex 版本能力感知与分层配置安装 实施计划

## 背景与问题

安装器 [codex-instruct.py](file:///Users/kkcarrot/swe-project/codex-keysmith_fork1/codex-instruct.py) 当前无条件向 `~/.codex/config.toml` 顶层写入 `model_instructions_file = "./<name>.md"`（见 [render_model_instructions](file:///Users/kkcarrot/swe-project/codex-keysmith_fork1/codex-instruct.py#L15364-L15409)），既不探测已安装的 Codex 版本，也不区分指令的两个层级：

1. **config.toml 运行配置层**：键名随上游快速演化。对上游仓库（openai/codex）多个已发布 tag 的源码核实结果：
   - `0.2.x – 0.9.x`：无文件型指令键，只有内联 `instructions`。
   - `0.10.0 – 0.89.x`：键名是 `experimental_instructions_file`（`Option<PathBuf>`，相对路径相对 **进程 cwd** 解析）；`model_instructions_file` 作为未知键被 serde 静默忽略。
   - `0.90.0 – 0.160.x`（当前最高稳定版 0.160.1）：正式键为 `model_instructions_file`（`AbsolutePathBuf`，相对路径相对 **config 文件所在目录** 解析）；旧键 `experimental_instructions_file` 在 0.90–0.13x 被解析但**已废弃并忽略**（启动告警），0.140 起彻底移除。三层 base 指令优先级为：CLI 覆盖 `base_instructions` > `model_instructions_file` 文件 > 内联 `instructions`；另有 `developer_instructions`（独立 developer 消息，不是同一覆盖关系）。
   - 结论：在 <0.90 的 Codex 上写 `model_instructions_file` 会写出语法正确但**永不生效**的配置——正是本次要消除的故障类。
2. **AGENTS.md 指令发现层**：自 2025-05（#885，0.2 时代）起 Codex 原生发现全局 `~/.codex/AGENTS.md` 及项目目录链 AGENTS.md，拼接进 developer 消息；它与 config.toml 的 base 指令文件是**两个独立层**，不构成互斥覆盖。现有 [instruction_mode_report](file:///Users/kkcarrot/swe-project/codex-keysmith_fork1/codex-instruct.py#L15464-L15487) 把 AGENTS.md 归类为 `competing_files`，属于错误建模。

## 目标

- 探测/接收 Codex 版本 → 查**版本化 capability descriptor** → 选择该版本确实支持的安装策略；不支持的键绝不静默写入、绝不声先生效。
- 未知版本 / 无法取得版本证据：部署与 `--reactivate` 输出**阻断式兼容报告**并以非零退出；`--status` 只读展示不阻断。
- 将 config.toml 运行配置层与 AGENTS.md 指令发现层分别建模；预览逐目录展示每层来源、运行时优先级、以及将被原样保留的未知字段/失效键。
- 保持现有事务、备份、manifest、卸载与重激活机制的安全属性；旧 manifest 向后兼容。

## 研究结论（关键约束）

- 零依赖单文件脚本、py38、stdlib only（TOML 为自研保守扫描器，不能引入 tomllib/tomli）。
- 写入管线：`deploy → inspect_directory → render_model_instructions → 事务 journal/备份 → manifest`；卸载/重激活对目标键的引用散落在 10355-10490、14378-14768 等处，当前全部硬编码 `model_instructions_file` 与 `./<name>`。
- manifest 有 schema_version=1 与严格校验（[_validate_manifest](file:///Users/kkcarrot/swe-project/codex-keysmith_fork1/codex-instruct.py#L8767-L8783)）；新增字段必须可选并赋予旧部署默认值。
- 大量测试以子进程方式直接跑 CLI 且环境中没有 codex 二进制；fail-closed 后需统一注入假 `codex`（用 `tests/conftest.py` 预处理 PATH，而不是逐个改测试）。
- GUI 的 [parser.js](file:///Users/kkcarrot/swe-project/codex-keysmith_fork1/gui/src/lib/parser.js) 对无法识别的预览行容错（action 行全收集、未知 kv 标签忽略、非零退出由 gatePreview 阻断），Python 侧新增行不破坏其语义完整性；本次不改动 GUI。
- 可复用现有子进程探测范式（4700-4800 行：timeout、shell=False、capture_output）。

## 文件与模块

- `codex-instruct.py`：全部核心改动（新增能力/分层建模与 CLI 参数，泛化目标键并串联 inspect/preview/manifest/reactivate/uninstall）。
- `tests/test_capability.py`（新建，必要）：版本解析、descriptor 选择、策略渲染、阻断报告、分层解析与未知字段保留的单元/CLI 测试。
- `tests/conftest.py`（新建，必要）：为全部子进程测试在 PATH 前置一个报告现代版本（0.160.1）的假 `codex`，保持 fail-closed 默认下既有测试语义不变。
- 既有测试文件：仅在出现断言漂移时最小调整（优先不改）。
- 不新增/修改文档文件；GUI 与 envelope 脚本不动。

## 数据模型设计（新增于 codex-instruct.py 常量区附近）

1. `CAPABILITY_DESCRIPTOR_SCHEMA_VERSION = 1` 与不可变描述符表（tuple of dataclass(frozen=True)）：
   - `legacy-inline-only`：`>=0.2.0,<0.10.0`；无文件型指令键；`file_install_strategy=None`（不支持文件安装，阻断并建议升级）；`agents_md_global=True`。
   - `experimental-instructions-file`：`>=0.10.0,<0.90.0`；目标键 `experimental_instructions_file`；`reference_style="absolute"`（规避相对路径相对 cwd 的歧义，写 codex_dir 下 MD 的绝对路径）；`agents_md_global=True`。
   - `model-instructions-file`：`>=0.90.0,<0.161.0`；目标键 `model_instructions_file`；`reference_style="config-relative"`（保持 `./<name>.md`）；`deprecated_keys=("experimental_instructions_file",)`（存在即作为失效键报告，不擅自删除）；已知兄弟键 `instructions`（文件优先于它）、`developer_instructions`（独立 developer 消息）；`agents_md_global=True`。
   - 每个条目记录 `evidence`（核实依据，如 "upstream tag rust-v0.90.0 config/mod.rs"）。
   - 区间外（含 >0.160.x 的新版本、预发布 0.161+/0.162-*、0.2 以前）：`unknown` → 阻断。
2. `CodexVersion`（major/minor/patch 元组 + raw 字符串 + prerelease 标记；比较只用数字元组）。
3. `VersionEvidence`：`status ∈ {probed, pinned, unavailable, unparseable}`，version、来源（`--codex-bin`/PATH/`--codex-version`）、原始输出、executable 路径。
4. `ResolvedCapability`：evidence + descriptor（或 None）+ `install_key`、reference_style + blockers/reports 列表。
5. 分层报告：
   - `RuntimeConfigLayer`：source=`~/.codex/config.toml`（user 层）、固定优先级说明（CLI/`-c` > 受信项目 `.codex/config.toml` > `--profile` > user > system `/etc/codex/config.toml` > 默认值）、managed 目标键及将写入的引用、同层已识别指令键（instructions/developer_instructions/experimental 旧键）及其相互优先级、`preserved_unknown_keys`（键名+行号，保持字节不动）、同目录 `*.config.toml` profile 文件遮蔽提示。
   - `InstructionDiscoveryLayer`：mechanism=`AGENTS.md native discovery`、global 节点（`codex_dir/AGENTS.md` 是否存在/是否非空）、语义说明（全局→项目根→cwd 拼接，后者冲突优先；direct developer/user 指令高于 AGENTS.md；**与 config 层并存、非互斥**）。
   - `LayerReport` 汇总两者供状态/预览使用。

## 实施步骤（依赖顺序）

1. **版本探测与解析**
   - 新增 `parse_codex_version(text) -> Optional[CodexVersion]`：从输出中取首个严格 semver（容忍 `codex`/`codex-cli` 前缀与后缀）。
   - 新增 `probe_codex_version(executable: Optional[str])`：候选 = `--codex-bin` → PATH `codex`（Windows 含 `.cmd/.exe/.bat` 候选）；仅执行 `--version`，`shell=False`、5s 超时、capture_output、不设特殊 cwd；FileNotFoundError/超时/非零/不可解析全部归一为证据状态，不抛异常。
   - 新增 `resolve_capability(explicit_version, codex_bin)`：pin 优先（格式非法直接 parser.error），其次探测，最后 unavailable；按描述符区间匹配，区间外给出带相邻版本边界的阻断文案。
2. **泛化 TOML 分析（保持零依赖与保守语义）**
   - `_analyze_toml_root(content, target_key=DEFAULT_INSTRUCTION_KEY)`：把当前只筛选目标键的扫描扩展为同时返回 `root_scalar_keys: List[(key, lineno, raw_preview)]` 与 `table_headers`；错误消息在非默认键时使用通用措辞；重复目标键/命名空间占用检查改为针对传入键（同时对旧键保留同等保护）。
   - `render_model_instructions(content, reference, target_key, analysis)`：reference 与键均参数化（现有签名包一层默认参数适配器，保持既有测试/调用兼容）；插入锚点逻辑中“model 语句后插入”泛化为“最后一个已知标量语句后/第一个 table 前”。
3. **分层解析**：新增 `analyze_runtime_config_layer(codex_dir, content, analysis, capability)` 与 `analyze_instruction_discovery_layer(codex_dir, capability)`；列出未知顶层标量键与 table 名（去重、保序、只记键名与行号，不回显敏感值）；标记失效旧键与内联指令键的优先级关系；扫描 codex_dir 下 `*.config.toml` 仅用于遮蔽提示。
4. **inspect_directory 串联**
   - 签名增加可选 `capability: Optional[ResolvedCapability]`；据此计算 `install_key`/`install_reference`（config-relative=`./<name>`；absolute=`str(codex_dir/name)`），调用参数化后的 render；不可用时不渲染、`config_changed=False`，并写入 `plan.blockers`（阻断式兼容报告，含证据、原始版本输出、descriptor 边界、建议参数 `--codex-version`/`--codex-bin` 与升级 keysmith 提示）。
   - 旧键存在、内联键共存等以 warnings/报告形式挂到 plan，供预览展示。
5. **manifest 与回滚/卸载/重激活策略化**
   - manifest `config` 节增加可选 `key`（默认 `model_instructions_file`）与 `reference`（默认 `./<md.path>`），新增可选 `capability` 节（descriptor_id、codex_version、evidence、descriptor_schema_version）；`_validate_manifest` 将三者加入 optional_keys 并做值校验。
   - `_build_deployment_manifest` 写入实际使用的键/引用与能力快照。
   - 卸载所有权检查、字段恢复、reactivate 计算（10355-10490、14378-14768、10434 等）改为从 manifest 读取键与引用，旧 manifest 走默认值；`ensure_model_instructions` 增加键/引用参数。
   - reactivate 同样先做 capability 解析（目标键以当前 Codex 能力为准；与 manifest 记录不一致时阻断报告，不跨键猜写）。
6. **CLI 与输出**
   - 新增 `--codex-bin PATH` 与 `--codex-version X.Y.Z`（互斥校验；scenario/scaffold/restore-hooks/uninstall/recover 不接受二者，parser.error）。
   - `deploy()`：解析一次能力，传入每个目录的 inspect；dry-run 预览逐目录输出：
     - `Compatibility:` 行（版本、证据状态/来源、descriptor id、选中键与引用、是否生效）；
     - `Runtime config layer:` 块（来源、user 层及其运行时优先级、将写入键、已识别指令键及优先级、`preserved unknown keys:` 清单、profile 遮蔽提示）；
     - `Instruction discovery layer:` 块（AGENTS.md 机制、global 文件状态、拼接/非覆盖语义）；
     - 阻断时打印 `[Blocked] 兼容性...`（中英双语，沿用现有 `→ [阻塞]` 样式），dry-run 非零退出；写路径复用现有 preflight blockers 拒绝写入。
   - `show_status()`：只读解析能力并输出同样三块（unavailable/unknown 仅展示与警告，不改变 status 退出码语义）；将现有 `competing_files` 表述改写为“独立发现层（拼接、非覆盖）”，保留原函数返回键以免破坏内部调用。
   - 输出双语走现有 `_localized`；英文行措辞保持 GUI parser 可容错形态。
7. **测试**
   - `tests/conftest.py`：会话级创建临时 bin 目录写入可执行假 `codex`（`--version` 输出 `codex-cli 0.160.1`），前置进 `os.environ["PATH"]`，并提供工厂辅助（测试可自行生成其他版本假二进制）。
   - `tests/test_capability.py` 覆盖：版本字符串解析（前缀/后缀/畸形）；各边界版本策略选择（0.5 不支持、0.20→experimental+绝对路径、0.50 同、0.90/0.100/0.140/0.160→model+./、0.161/0.162-alpha/9.9 阻断、无二进制阻断、`--codex-version` pin 生效与非法值报错）；渲染只写受支持键；旧键/未知键保留且进报告；AGENTS 层与 config 层独立呈现；manifest 新字段往返与旧 manifest 默认值；reactivate/卸载按 manifest 键操作；CLI dry-run 阻断时 exit=1 且不落盘。

## 依赖与注意事项

- 所有外部事实均来自上游已发布 tag 源码与官方开发者文档（2026-10 核实）；descriptor 表是**维护性数据**，新版本发布后需人工更新区间与 evidence，未知即阻断是刻意设计。
- 不引入第三方依赖、不联网；探测只调用本地 `codex --version`。
- 探测失败不得影响只读路径之外的文件系统；证据缓存于单次进程。
- `experimental` 策略写绝对路径会暴露 home 路径于 config.toml——在该策略预览中明确告知；卸载凭 manifest 引用精确移除。
- 安全默认不变：无 `--yes` 仅预览；所有写入仍走既有事务 journal、指纹与备份。

## 验证

- `python3 -m pytest tests/ -q` 全绿（含新测试与既有测试）。
- `python3 -m py_compile codex-instruct.py`；若环境有 ruff 则跑 `ruff check codex-instruct.py tests/`。
- 手工冒烟（临时 PATH 假二进制，版本分别 0.50.0 / 0.90.0 / 0.160.1 / 9.9.0 / 缺失）：
  - `--dry-run --lang en`：核对策略键、引用形态、两层报告、未知字段清单、阻断退出码；
  - 0.50 与 0.160 各执行一次 `--yes` 部署→`--status`→`--uninstall`，核对写入键、manifest capability 节与干净回滚；
  - config 中预置未知键（`my_team_override = "x"`、`[features]` 表、失效旧键），确认字节保留且出现在报告中。

## 风险

- **默认 fail-closed 改变无 codex 二进制场景（部分桌面用户）的体验**：缓解——`--codex-bin`/`--codex-version` 显式通道；报告中直接给出用法；Tauri sidecar 的自动探测接线留待后续（本次不改 GUI）。
- **conftest 全局 PATH 注入影响其他测试**：仅追加临时目录、会话级一次性创建、不删除既有 PATH；不产生文件写入副作用；版本探测 5s 内返回。
- **旧版（0.10–0.89）绝对路径策略与备份/回滚交互**：以 manifest 记录的实际引用为唯一所有权依据，回滚优先整文件备份，字段恢复路径使用同一键与引用。
- **描述符区间过期**：上界故意收紧到已核实的 0.160.x；新稳定版发布时更新表与 evidence，阻断报告提示用户更新 keysmith，而不是静默放行。
