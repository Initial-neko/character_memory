# Draft — Character Browser 连续真实探索（实施交接）

> **尚未实现**，保持 Draft。父任务 [#304](https://github.com/Initial-neko/character_memory/issues/304)；依赖 A 能力面，不另造浏览器。

## 用一句话解释

角色自己决定从合法起点打开网页、看真实内容、选择下一步，再根据实际结果决定继续还是停止。

## 已核对基线

- \`browser_web.HeadlessBrowserWebFetcher\` 确有 Playwright Chromium，可访问公开网址、执行页面 JS，再提取正文、标题、链接；目前 \`fetch_many\` 每批新建 Playwright 和 Browser，逐页关闭，不是持久浏览会话。
- \`world_observation.WorldObservationService\` 使用 SearchProvider 找候选并读取页面；这是外部网页事实输入，不代表角色拥有自动点击/导航/截图能力。
- \`visual_capture_web.py\`、\`VISUAL.md\` 已允许**用户授权** DISPLAY/CAMERA 临时帧；不能把共享屏幕误当作角色可自主打开的 Browser。
- 现有 Browser 通过 public URL guard 拒绝内网；在内部 Space 页面场景，不能简单把 \`allow_private_network=True\` 扩成所有网址可访问。

## Codex 任务（实现时）

1. 保留 \`HeadlessBrowserWebFetcher.fetch/fetch_many\` 公开契约与现有测试原样兼容；增加单独的**有界 Browser Session**，支持 \`open\`、\`inspect\`、\`follow_link\`、只读 \`screenshot\`、\`close\`（具体签名由实现确定）。
2. \`inspect\` 返回**真实** title、final URL、可见正文/受控 DOM 摘要、稳定的链接引用和可选可见元素；输出限长，页面文字始终为 untrusted data。避免工具让模型执行任意 JS/CSS 选择器或直接访问网络底层。
3. follow 必须绑定**本会话 inspect 看到的链接引用**，不接受模型自由传入重定向目标来绕过 URL policy；需要验证导航后的实际 target，跨域与重定向继续守护。
4. 公开网页与 Character Memory 内部页面分别设置可验证 origin scope；内部页面只暴露已授权角色本来可见的内容，优先使用低权限只读会话/视图。**不能复用用户已登录的隐私浏览器 Cookie**，也不能直接开放全部 loopback/lan 地址。
5. 截图用于角色真实视觉判断时，保留来源、观测时间和允许范围；默认不把原始截图长期入库，更不能声称“角色当时看到了”未执行的页面。
6. 会话最大页面数、操作数、响应大小、墙钟、同时会话数固定有界；结束/异常/重启清理资源。已知 \`TECH_DEBT\` 记录 Playwright 路径 DNS 阻塞在停机期间可能悬挂，必须有 timeout/取消策略及回归，不能把 SSRF 防护改弱。

## 验收

- 在本地公共两页 fixture，从首页看到链接、选择下一页、读到 JS 渲染后的不同真实内容；不必预先指定第二个 URL。
- 有真实 Playwright 截图证据及 metadata；截图不能是造的设计图。
- 空页面/404/恶意重定向/内网地址/无权限内部页面/资源异常/网络超时/预算耗尽保持拒绝或失败，不可“自动成功”。
- 对已有 \`tests/test_world_browser_playwright.py\` 无回归，另添 session/navigation/SSRF/cleanup tests；按 #292 的规则提交必要视觉证据。
- 不具 Browser grant 的角色看不到工具；有 Browser grant 的角色也不会因此自动发送聊天或公开动态。

## 明确不做

不启用任意表单提交、文件下载、任意系统浏览器控制、桌面操作或未经授权的第三方账户操作；不把 GUI 截图作为永久人物记忆；不写“查某部番/检查布局”的固定业务流程。

## 未来正式 PR

需在 \`docs/current/SOCIAL_WORLD.md\` 与 \`ARCHITECTURE.md\` 说明会话、来源、失败/重启；PR 附真实 Playwright 测试、取消与网络安全回归、性能和费用。
