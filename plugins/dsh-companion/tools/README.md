# 陪伴插件的本地服务

不用记命令，先看现在有什么：

    node tools/stack.mjs status

会打印每个服务的端口、状态、PID、是否 ready。

## 服务表

| 名字 | 端口 | 作用 | 冷启动 |
| --- | --- | --- | --- |
| `asr` | 50131 | 语音转写（SenseVoice） | 约 2 秒 |
| `mic` | 50132 | 麦克风采集（设备 19） | 约 1 秒 |
| `gsv` | 9014 | 语音合成（momo） | 约 45 秒 |
| `web` | 50140 | 测试实例 | 约 5 秒 |

## 启动 / 停止

    node tools/stack.mjs up            # 缺哪个起哪个，然后等它们 ready
    node tools/stack.mjs up mic gsv    # 只起指定的
    node tools/stack.mjs down          # 全部停掉
    node tools/stack.mjs down gsv      # 只停一个

## 自测

    node tools/stack.mjs check

依次检查 GSV / ASR / MIC 健康、TTS 合成、把合成音频喂回 ASR、麦克风信号。
任何一段失败退出码非 0，不需要真人开口。

## 常见问题

- **说了半天没反应**：先 `status` 看 mic 是否 `ready`；再 `check` 看 `ASR round-trip`。
  mic 会在 5 秒没有新音频帧时自动重启采集，`/health` 里的 `stale` 能看到是否刚重启过。
- **连接失败 / fetch failed**：语音链路是 asr + mic + gsv 三件套，缺一个就起不来。
- **风扇响**：GSV 加载模型时会热一下；`web` 用无头浏览器跑 Live2D 才会持续占 CPU，正常用浏览器不受影响。
