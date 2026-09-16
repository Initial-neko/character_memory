# V1 Technical Debt Register

这份清单只记录**仍然真实存在**的维护债务，不把“可以重构”自动等同于“应该现在重构”。

## 已处理

### Sticker CLI ownership drift

已将 `sticker_import_cli.py` 从 character-local 写入语义收敛到当前 global Sticker ownership：

- 新导入写 `resolve_sticker_dir(settings)`；
- `--character` 仅保留为 deprecated compatibility 参数；
- legacy persona-local manifests 继续只读兼容；
- Web / CLI / Runtime 对 Sticker ownership 的定义重新一致。

### Windows CI visibility

CI 增加 `windows-latest` smoke job，安装 `api + dev + media` 依赖并覆盖：

- cross-platform scripts；
- Windows/native media bootstrap；
- Sticker CLI regression。

该 job 不下载 ASR/TTS 模型，不做实时推理，只守住安装和 native bootstrap contract。

## 仍需处理：高优先级

### Dependency lock / frozen install

当前仓库没有提交 `uv.lock`，CI 仍使用普通 `uv sync`。虽然关键依赖已有 pin/bounds，但长期仍可能因 transitive dependency 漂移而改变环境。

目标：

```text
commit uv.lock
CI -> uv sync --frozen / --locked
setup scripts -> respect committed lock
```

这项应在可实际生成并验证 lockfile 的开发环境中完成，不手写 lockfile。

## 仍需观察：中优先级

### Deprecated browser audio capture API

`web/dictation.js` 当前使用 `AudioContext.createScriptProcessor()`。该 API 已是历史接口；V1 尚能工作，不应无测试地直接替换。后续应评估 `AudioWorklet`，并保持现有 16 kHz WAV / draft-only dictation contract。

### Large edge modules

以下文件已经偏大，但“文件大”本身不是拆分理由：

- `api.py`
- `group_conversation_service.py`
- `storage/sqlite.py`
- `visual_web.py`
- `web/app.js`
- `web/groups.js`
- `web/voice.js`

只有当继续修改导致 ownership 混乱、测试困难或频繁冲突时，再按稳定 contract 拆分。

## 延后到大版本：低收益高 churn

### Top-level `*_web.py` package layout

route modules 位于 package 顶层属于历史结构债，但当前职责清楚。V1 不为了目录美观迁移到 `transport/http/`，避免全仓 import churn。

### Historical `p0_*.css`

正式页面仍加载多份 milestone 命名 CSS。这是样式命名债，不是运行时缺陷。后续若统一样式系统，应一次性按组件/页面 ownership 重组，而不是逐个改名。

### TTS Provider Runtime / Lab coupling

`:9002` 同时承担 Kokoro provider runtime 与 audition lab。当前正式链路稳定，拆服务会增加启动、配置和端口复杂度；除非 Lab 与 production lifecycle 真正冲突，否则不在 V1 拆分。

### Life / Inspector

`life/` 与 `ui.py` 仍有入口和测试，不是 dead code。产品方向虽冻结其扩张，但删除属于大版本决策，不做零碎删减。

## 原则

处理技术债时优先级如下：

```text
真实故障风险
> 可复现性 / 平台稳定性
> 语义冲突
> 高频修改冲突
> 可读性
> 目录/命名美观
```

任何“大重构”都需要先证明它解决了当前真实问题，而不是仅仅让结构更像理想架构。
