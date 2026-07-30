# Source Bundle Ingestion 实施方案

状态：**已确认并实施（2026-07-30）**

涉及组件：

- `Luna Skill`
- `deckflow-core`
- `deckflow-extract`

## 1. 结论

采用以下职责边界：

```text
Luna Skill
  └─ 调用一次 deckflow parse
       ├─ deckflow-core：流程编排、合约校验、canonical Source Bundle 组装与原子提交
       └─ deckflow-extract：纯解析、可选本地引擎安装、输出临时 Parse Bundle
```

用户和 Luna Skill 不感知 Parse Bundle，也不执行 importer。

整个“解析并组装 Source Bundle”过程不需要 AI：

- `brief` 是调用方传入的原始任务描述；
- `deck-language` 是调用方指定的最终 Deck 语言；
- `deckflow-extract` 可以检测并记录原始材料语言，但不翻译；
- AI 只在后续 Planning、内容组织和成稿阶段使用。

## 2. 目标

1. Luna Skill 尽量轻量，只调用 `deckflow-core`。
2. `deckflow-extract` 保持纯解析器，不依赖 Luna Source Bundle 合约。
3. `deckflow-core` 成为 canonical Source Bundle 的唯一组装和提交入口。
4. 用户只看到最终 `<project>/source-bundle/`，不看到中间转换。
5. `--upgrade auto` 在调用方明确传入时，能够完成本地增强引擎安装、校验和重选。
6. 任何解析、校验或提交失败都不得改写既有 Source Bundle。

## 3. 非目标

- 不在 extract 或 core 中调用模型。
- 不在解析阶段理解、扩写或翻译 `brief`。
- 不让 Luna Skill 复制文件、转换 manifest 或清洗 provenance。
- 不兼容旧的 `deckflow-core parse --out <parse-bundle>` 接口。
- 不要求 `deckflow-extract` 理解 project、brief、Deck 语言或 Luna fingerprint。
- 不把临时 Parse Bundle 定义为用户产物。

## 4. 公开接口

Luna Skill 只调用：

```bash
deckflow parse <input> \
  --project <deck-project> \
  --brief "<user task>" \
  --deck-language <bcp47> \
  [--title "<title>"] \
  [--replace] \
  [--upgrade never|auto] \
  [--mode local|cloud]
```

字段语义：

| 参数 | 所有者 | 语义 | 是否使用 AI |
| --- | --- | --- | --- |
| `<input>` | Luna / 用户 | 一个显式的本地原件 | 否 |
| `--project` | Luna | 已存在的 Deck project | 否 |
| `--brief` | Luna / 用户 | 用户任务原文或调用方已有的任务摘要 | 否 |
| `--deck-language` | Luna / 用户 | 最终 Deck 的目标语言，BCP 47 | 否 |
| `--title` | Luna / 用户 | Source Bundle 可选标题 | 否 |
| `--upgrade auto` | Luna / 用户 | 明确授权安装本地增强引擎；默认 `never` | 否 |
| `--mode cloud` | Luna / 用户 | 明确授权上传原件并使用云解析 | 否 |

`--deck-language` 不等于原文语言。例如英文 PPTX 可以使用
`--deck-language zh-CN`。extract 记录的原文语言可以是 `en-US`，后续 AI
规划阶段才负责中文成稿。

`--brief` 和 `--deck-language` 保持必填。core 不根据文件内容猜测 Deck
目标语言，也不把原文语言自动当作成稿语言。`brief` 只做非空校验并清除
首尾空白，不理解、不扩写、不翻译。

## 5. deckflow-extract 内部接口

core 内部调用：

```bash
deckflow-extract parse <input> \
  --out <temporary-parse-bundle> \
  --replace \
  --anchors on \
  --upgrade never|ask|auto \
  --mode local|cloud
```

extract 的职责只包括：

1. 检测格式并选择解析引擎；
2. 在明确的 `--upgrade auto` 下安装缺失的增强能力；
3. 安装完成后执行 import self-check，并重新选择引擎；
4. 输出 Parse Bundle：
   - `parse-manifest.json`
   - `document.md`
   - `assets/`
5. 输出结构化的 fidelity、coverage、gaps、decision、recommendations 和
   engine acquisition 结果。

extract 不接收：

- `--project`
- `--brief`
- `--deck-language`
- Luna Source Bundle 的 replace / confirmed 语义

## 6. `--upgrade auto` 闭环

core 对外只提供 `never|auto`，默认 `never`。extract 可以保留 `ask` 作为
provider 内部或开发接口，但 Luna 不依赖该状态。`--upgrade auto` 本身视为
明确安装授权，不再只返回一条不可执行的建议。

预期流程：

1. extract 根据当前格式和现有引擎计算最佳缺失 capability；
2. 使用当前 Python 解释器将依赖安装到 provider 自有 sidecar；
3. 使用隔离子进程验证目标 import；
4. 验证成功后激活 sidecar；
5. 重新执行 engine selection；
6. 使用增强引擎解析；
7. 在 Parse Bundle 中记录净化前的 acquisition outcome，供 core 映射；
8. 安装失败时可以使用已有低阶引擎继续解析，但必须返回
   `repairable`、结构化失败原因和 capability，不能伪装为增强成功。

recommendations 只输出结构化意图，例如：

```json
{
  "action": "install",
  "capability": "pptx",
  "cost": {
    "kind": "download",
    "size_mb": 37,
    "reversible": true
  },
  "rerun_required": true
}
```

不在 recommendation 中硬编码 `deckflow-extract install ...` 或重新解析命令。
命令名、认证层级和调用方式由 core 或调用方决定。

## 7. core 内部处理流程

```text
校验 input/project
    ↓
解析或按需获取 deckflow-extract
    ↓
在系统临时目录创建短生命周期 Parse Bundle
    ↓
调用 extract
    ↓
校验 Parse Bundle schema/hash/path/usable/gaps
    ↓
在 project sibling staging 目录组装完整 Source Bundle
    ↓
校验 canonical manifest、文件 hash、coverage、provenance、fingerprint
    ↓
原子替换 <project>/source-bundle
    ↓
删除临时 Parse Bundle，只返回 canonical outputs
```

### 7.1 调用前校验

- input 必须由位置参数显式提供；
- input 必须是存在的 direct regular file；
- 拒绝 URL、目录、symlink、hardlink 和特殊文件；
- project 必须是存在的 direct directory；
- input 不能位于 `<project>/source-bundle/` 内；
- `brief` 必须非空；仅清除首尾空白；
- `deck-language` 必须是合法 BCP 47 tag；
- report 不能覆盖 input 或 Source Bundle 内文件。

### 7.2 Parse Bundle 验收

core 必须独立验证，不能只相信 extract 的 stdout：

- 支持的 Parse Bundle schema；
- `tool.name == deckflow-extract`；
- manifest、document、assets 均不得路径逃逸；
- bundle 内拒绝 symlink 和 hardlink；
- manifest 中 input SHA-256 必须与显式 input 完全一致；
- asset 文件 hash 必须闭合；
- `decision.usable` 必须为 `true`；
- 不得存在 `severity: blocking` 的 gap；
- PPTX 的 blocking gap 与其他格式同样阻止 canonical commit；
- stdout status、manifest decision 和实际文件必须一致。

### 7.3 canonical Source Bundle 映射

| Parse / 调用输入 | canonical Source Bundle |
| --- | --- |
| 显式 input 原件 | `src/<safe-name>` + `manifest.sources[]` |
| `document.md` | `materials/<source-id>.md` + `manifest.materials[]` |
| `document.locator_profile` | `content.materials[].locator_profile` |
| Parse assets | `assets/` + `manifest.assets[]` |
| asset locator | `manifest.assets[].locator` |
| `brief` | `content.json.brief` |
| `deck-language` | `content.json.language` |
| 解析检测语言 | `manifest.imports[].source_language` |
| provider / engine version | `manifest.imports[].provider/engine` |
| Parse manifest hash | `manifest.imports[].parse_manifest_sha256` |
| fidelity | `manifest.imports[].fidelity` |
| Parse coverage | `manifest.imports[].coverage` |
| gaps | `manifest.imports[].gaps` |
| recommendations | `manifest.imports[].recommendations` |
| diagnostics | `manifest.diagnostics[]`，绑定 `source_ref` |

同一物理 asset 可按 hash 去重，但不同 locator 的逻辑出现必须保留为不同
asset record。

### 7.4 provenance 净化

canonical Source Bundle 禁止写入：

- `input.origin`
- input、project、Source Bundle 或 Parse Bundle 的绝对路径
- 临时目录路径
- provider command / argv
- 安装命令
- credentials、token、API key
- cloud raw response
- provider sidecar 绝对路径
- 安装时间戳、耗时和 Python runtime tag

允许记录：

- provider 名称与版本
- engine 名称
- capability
- Parse manifest SHA-256
- 净化后的 fidelity、coverage、gaps、recommendations
- 安装 capability、成功 / 失败状态、预计 / 实际体积及非敏感错误摘要

### 7.5 原子提交

1. staging 目录必须位于 project 同一文件系统；
2. 先在 staging 中复制或创建完整 bundle；
3. staging 内完成全部 schema、hash、coverage、reference 和 fingerprint 校验；
4. 若存在旧 bundle，先原子移动到 sibling backup；
5. 原子移动 staging 到 canonical path；
6. final rename 失败时恢复 backup；
7. 成功后删除 backup；
8. 任意失败不得改变旧 bundle；
9. 最终清理临时 Parse Bundle。

## 8. append、replace 与 confirmed

已实施行为：

- 默认：向现有 draft / review-ready bundle 追加一个新 source；
- `--replace`：用本次 input 重建整个 Source Bundle；
- 已存在相同 input SHA-256：拒绝重复追加；
- `status: confirmed`：始终禁止由 `deckflow parse` 修改；
- 第一版不提供 `--replace-confirmed`；
- 若未来需要修改 confirmed bundle，必须由单独事务同时完成：
  - Source Bundle 降级为 `review-ready`；
  - 使引用旧 source fingerprint 的确认失效；
  - 标记 Intent、Deck Plan 和 build 依赖需要重新绑定。

禁止只替换 confirmed Source Bundle 而保留依赖它的旧确认状态。

## 9. 对外结果

成功时只返回 canonical 结果：

```json
{
  "status": "succeeded",
  "parse_status": "parsed",
  "outputs": [
    {
      "kind": "source-bundle",
      "path": "<project>/source-bundle"
    },
    {
      "path": "<project>/source-bundle/manifest.json",
      "sha256": "<sha256>"
    }
  ],
  "source_id": "source-001",
  "material_id": "material-001",
  "content_fingerprint": "<sha256>"
}
```

不得在 caller-facing envelope 中返回：

- 临时 Parse Bundle 路径；
- Parse manifest 路径；
- importer / conversion step；
- provider 命令行；
- credentials。

provider 的 fidelity、gaps、decision、recommendations 可以作为结构化诊断保留。

## 10. 状态和失败语义

| 条件 | core status | 是否提交 Source Bundle |
| --- | --- | --- |
| usable，无 blocking gap | `succeeded` | 是 |
| usable，但明确授权的增强安装失败且 fallback 可用 | `partial` | 是 |
| needs-input / blocked | `failed` | 否 |
| input hash mismatch | `failed` | 否 |
| schema / path / symlink / hardlink 违规 | `failed` | 否 |
| blocking gap | `failed` | 否 |
| existing confirmed bundle | `failed` / output conflict | 否 |
| canonical staging 校验失败 | `failed` | 否 |
| final rename 失败 | `failed`，恢复旧 bundle | 否 |

## 11. 最小改动范围

### deckflow-extract

只做以下必要修改：

1. 修复 `--upgrade auto`：安装、self-check、激活、重新选引擎；
2. 返回结构化 engine acquisition outcome；
3. recommendations 使用 capability，不输出硬编码命令；
4. 修复同 hash、不同 locator 的逻辑 asset 丢失。

core 每次向 extract 提供全新的临时输出目录，因此 extract 不需要为本方案
新增 Parse Bundle 覆盖和 rollback 逻辑。canonical Source Bundle 的原子提交
与回滚完全由 core 负责。

明确不加入：

- Source Bundle writer；
- Luna schema validator；
- project / brief / Deck language 参数；
- canonical fingerprint；
- confirmed 状态机。

### deckflow-core

新增或修改：

1. 新的公开 `deckflow parse` 参数；
2. 临时 Parse Bundle 生命周期管理；
3. Parse Bundle trust-boundary validator；
4. canonical Source Bundle assembler / validator；
5. provenance scrub；
6. sibling staging + atomic rollback；
7. caller-facing 结果净化。

### Luna Skill

只修改调用命令和结果读取：

```text
收集 input/project/brief/deck-language
              ↓
调用 deckflow parse
              ↓
读取 status、diagnostics 和 canonical outputs
```

Skill 不再包含 importer，也不读取 Parse Bundle。

## 12. 验证矩阵

### 正常路径

- Markdown / text 直接生成 canonical bundle；
- PPTX L0 fallback 可用且无 blocking gap；
- PPTX `--upgrade auto` 成功安装 python-pptx 并使用增强引擎；
- 多次调用追加不同 source；
- `--replace` 重建；
- brief 和 deck language 原样写入，不发生翻译。

### 合约安全

- input SHA-256 mismatch；
- Parse manifest schema 不支持；
- document / asset 路径逃逸；
- symlink；
- hardlink；
- asset hash mismatch；
- `decision.usable: false`；
- 任意 blocking gap；
- PPTX blocking gap；
- absolute `input.origin` scrub；
- command / credential / sidecar scrub；
- 同 hash asset 的多 locator 保留；
- source/material/asset reference 不闭合；
- coverage 计数不闭合；
- fingerprint mismatch。

### 失败和回滚

- extract 非零退出但无 JSON；
- extract JSON 与 manifest 状态不一致；
- `--upgrade auto` 安装失败；
- staging 校验失败；
- final rename 失败；
- 既有 bundle 不是受信 canonical bundle；
- confirmed bundle 未授权；
- 重复 source hash；
- 所有失败均验证旧 bundle hash 未变化；
- caller-facing JSON 不含临时 Parse Bundle 路径。

### 干净环境

- 仅安装 `deckflow-core`；
- core 自动获取 pinned `deckflow-extract`；
- `DECKFLOW_HOME` 自定义目录；
- `--offline` 且 provider 缺失；
- `--upgrade auto` 的 sidecar 写入自定义 Deckflow home；
- 新安装的 `deckflow-extract install pptx` 和 core-driven auto install 均能
  通过 import self-check。

## 13. 已确认决策

1. 使用 `--deck-language` 表示最终 Deck 目标语言；
2. extract 单独检测并记录 `source_language`，两者互不覆盖；
3. 默认允许向 draft / review-ready Source Bundle 追加；
4. confirmed Source Bundle 不允许由 `deckflow parse` 修改；
5. 第一版不提供 `--replace-confirmed`；
6. `--upgrade` 默认 `never`，只有显式 `auto` 才允许下载；
7. Parse Bundle 完全不出现在 core 的 stdout / report；
8. 接受 `manifest.imports[]` 作为净化后的解析 provenance；
9. `brief` 只做非空校验和首尾空白清理，不使用 AI；
10. extract 只实施四项必要修改，其余全部由 core 负责；
11. 安装失败但 fallback usable 且无 blocking gap 时允许提交，core 返回
    `partial`；
12. 所有 canonical 组装、校验、原子替换和回滚均由 core 完成。

## 14. 实施验证

- `deckflow-extract`：138 tests passed，20 skipped；
- `deckflow-core`：149 tests passed，4 subtests passed；
- 变更文件通过定向 Ruff 检查；
- 干净 sidecar 环境中的 PPTX `--upgrade auto` 已完成安装、import self-check、
  engine reselect，并使用 `python-pptx` 输出 Parse Bundle；
- 真实 core 流程已验证首次生成、向 review-ready 追加、`--replace` 重建，
  以及 confirmed 拒绝写入且 manifest 字节不变；
- canonical manifest 已验证不含 Parse Bundle 路径、绝对 origin、provider
  command、credentials 或 sidecar 绝对路径。
