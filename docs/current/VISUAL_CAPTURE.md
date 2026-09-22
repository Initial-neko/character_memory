# Visual Capture — Camera / Screen Vision

Visual Capture 让 Browser 把摄像头或屏幕中的少量关键帧作为**当前一轮**视觉上下文交给同一个 Character。

最重要的概念边界：

```text
Visual Capture = 看
Visual Generation = 画
```

Capture 不生成新图，也不建立第二个视觉 Agent。它只是给现有 PersonRuntime 增加当前 turn 的 Vision context。

## 1. Supported sources

当前 Browser 支持：

```text
CAMERA
DISPLAY
```

- `CAMERA` 使用 `navigator.mediaDevices.getUserMedia()`；
- `DISPLAY` 使用 `navigator.mediaDevices.getDisplayMedia()`；
- audio 不随视觉 stream 上传。

Camera/Screen 可以在通话 UI 中开启，也可以复用同一 Visual Capture client contract 发送到 Direct / Group conversation。通话 Session、麦克风和视觉采集彼此独立：用户可以关闭麦克风后继续单独共享屏幕或摄像头，AI TTS 输出也不会因为本地麦克风关闭而停止。

## 2. Browser flow

核心前端：

```text
web/visual_capture.js
web/visual_client.js
web/voice.js
```

大致流程：

```text
Camera / Display stream
        ↓
periodic lightweight sampling
        ↓
change-based candidate cache
        ↓
select a few chronological keyframes
        ↓
JPEG data URLs
        ↓
visual message request
```

当前 `visual_capture.js` 的实现默认会：

- 大约每 `800 ms` 做一次候选采样；
- 将送往模型的长边压到约 `512 px`；
- 只在画面变化明显或经过一段时间后保留候选；
- candidate cache 有上限；
- 发送时优先保留首尾、变化较大的帧并保持时间顺序。

这些是当前实现参数，不是需要长期冻结的产品 API。稳定 contract 是“选择少量代表性关键帧，而不是连续上传视频”。

## 3. HTTP routes

后端入口：

```text
POST /v1/visual/direct/messages
POST /v1/visual/groups/{conversation_id}/messages
```

请求包含正常用户文字和 `visual_frames[]`。

每帧包含：

```text
filename
base64 image data URL
source: CAMERA | DISPLAY
captured_at_ms
```

Direct 还带 `character_id / conversation_id`；Group 使用 path 中的 conversation id，并继续支持 mention 解析。

## 4. Hard limits

后端固定保护：

```text
frames per request   <= 5
single frame         <= 2 MiB
total frame payload  <= 6 MiB
mime                 JPEG / PNG / WebP
```

后端会重新 base64 decode 并 sniff MIME，不信任 Browser 声明的文件类型。

超过任何限制都应在进入模型前返回明确 400，而不是继续把过大 data URL 塞进 LLM request。

## 5. Persistence boundary

Visual Capture frame bytes **不作为聊天附件长期保存**。

后端将用户文字正常持久化为 User Event，但 frame 二进制只存在于本轮 request/reaction context：

```text
User text
  -> durable Event

Visual frames
  -> transient image_data_urls
  -> ReactionScheduler
  -> same PersonRuntime / Vision model turn
  -> discarded after the turn
```

Durable Event 只保存少量 capture metadata，例如：

```json
{
  "visual_capture": {
    "frame_count": 4,
    "sources": ["CAMERA"],
    "captured_at_ms": {
      "first": 1234,
      "last": 4567
    }
  }
}
```

没有时间值时 `captured_at_ms` 可以省略。

这与普通 image attachment 不同：普通附件会进入 `MediaStorage / media_assets`，而 Visual Capture frame 不会。

## 6. Same PersonRuntime

Direct：

```text
User text + transient frames
  -> durable direct Event
  -> ReactionScheduler.enqueue_direct(... image_data_urls=frames)
  -> existing PersonRuntime
```

Group：

```text
shared user fact + transient frames
  -> conversation_events
  -> ReactionScheduler.enqueue_group(... image_data_urls=frames)
  -> existing GroupConversationService / member PersonRuntime
```

所以 Camera / Screen 不拥有独立 Memory、Mental State 或 Persona。

如果人物之后需要记住视觉内容，应由正常 Person reaction / Memory admission 形成语义 Memory，而不是把原始帧当 Memory。

## 7. Direct / Group semantics

### Direct

Visual request 会保留用户原始文字作为 UI display text，并在模型 context 中附加“本轮同时提供实时视觉关键帧”的内部提示。

### Group

Group visual request 继续使用共享 room fact 与普通 mention ordering。所有参与判断的成员看到的是同一次 room user fact，并可结合本轮 frames 理解。

frame bytes 不会被复制成每个 Character 各一份 durable fact。

## 8. Call integration

通话会话可以组合使用麦克风、Camera 和 Screen Capture，但三者不是绑定关系：

```text
Call Session
├─ microphone      optional / 可随时 mute
├─ camera/display  optional / 可独立保持
└─ AI TTS output   independent
```

语音输入仍保持原有顺序：

```text
speech
  -> ASR
  -> transcript validity gate
  -> only valid transcript selects/sends visual frames
```

当麦克风关闭而 Camera/Screen 仍在共享时，当前通话目标中的普通文字消息会自动选择最近约 15 秒内的少量关键帧，并走现有 visual message route。若用户切换到其他 Direct/Group conversation，视觉帧不会跟随到非通话目标。

当前 validity gate：

```text
trim 后空字符串        -> reject
纯空白/标点/符号        -> reject
任意汉字                -> accept
ASCII Latin/digit >= 2 -> accept
其它                    -> reject
```

例如：

```text
""      reject
"……"    reject
"?"     reject
"a"     reject

"嗯"     accept
"你好"   accept
"OK"    accept
"GPT"   accept
"123"   accept
```

无效 ASR：

- 不创建 chat message；
- 不上传当前视觉关键帧；
- UI 回到 listening。

这样可以避免一次纯噪声识别同时误提交一组 Camera/Screen 数据。

## 9. Lifecycle and privacy

Browser stream 生命周期由用户明确控制：

- Start Camera；
- Start Screen Share；
- Stop；
- 浏览器/系统结束 track 时自动停止。

切换来源会先停止上一条 stream，再开始新的来源。

当前设计不做：

- 后台持续录像；
- raw video persistence；
- raw frame history browser；
- 把整个屏幕共享 session 上传服务器；
- 跨 turn 自动重用过去 capture bytes。

因此 Visual Capture 更接近“这一句话说出口时，我顺便给你看几张当前画面”，而不是监控/录像系统。

## 10. Failure boundary

Visual Capture 是可选输入能力：

- Camera/Screen 权限失败不应破坏纯文本聊天；
- frame validation 失败必须阻止该 visual request，而不是写入不可解释的坏 frame；
- source User Event 一旦成功接受，后续 LLM/Provider failure 仍遵守普通 durable-fact 优先级；
- Capture 与 ImageGen failure 相互独立。

## 11. Regression expectations

至少持续覆盖：

- Direct / Group route 都存在；
- 5-frame / 2-MiB / 6-MiB 限制；
- JPEG/PNG/WebP MIME validation；
- frame bytes 不进入 MediaAsset/Event payload；
- metadata summary 正确；
- Browser CAMERA / DISPLAY start/stop；
- selected frames 不超过后端上限；
- Voice invalid transcript 不发送 frames；
- valid transcript + capture 仍进入同一个 PersonRuntime。

相关长期回归清单见 [`EVALS.md`](EVALS.md)。
