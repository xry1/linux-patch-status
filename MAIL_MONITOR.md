# 163 学校 / 企业邮箱、GLM 与飞书提醒

这套接入使用 IMAP SSL 只读收取相关 patch 邮件，用 GLM 生成中文摘要、待办和英文回复草稿，再向你配置的飞书群机器人发送提醒。不会发送邮件回复，也不会由模型修改 Applied、主线或 CVE 核实结果。

## 一次配置

1. 登录学校邮箱，在设置中启用 IMAP / 客户端服务，生成客户端授权码。账号默认为 `runyu.xiao@seu.edu.cn`，网易企业邮箱服务器预填 `imaphz.qiye.163.com:993`；以学校公布的客户端设置为准，可在配置时修改。普通 163 邮箱应使用 `imap.163.com`。如果学校禁用了客户端访问，需要由学校管理员开通。
2. 在飞书群中进入群设置，添加“自定义机器人”，保存它的 Webhook。建议启用签名校验并保存签名密钥。如果设置了关键词，将关键词设为 `Linux Patch`，或在配置窗口填写你自己的关键词。提醒发往这个群，手机是否弹出推送还取决于飞书群通知和系统设置。
3. 在你选用的 GLM 服务商处取得 API Key，并确认模型名称。支持自定义 API 地址及 `chat_completions`、`anthropic_messages` 协议。本机使用 [loliapi](https://loliapi.org/)，配置示例见下方；模型可用性及计费以服务商账户为准。
4. 双击 **配置邮箱提醒.cmd**。按提示输入邮箱地址、服务器、GLM API 地址、协议和模型，以及隐藏输入的授权码、服务商 API Key、飞书 Webhook 和可选签名密钥。不要把密钥粘贴到聊天或公开仓库中。更换服务商域名时，配置程序要求重新输入该服务商的 Key，避免误用原服务商的凭据。
5. 双击 **启动邮箱监测.cmd**，保持窗口运行。首次成功检查只建立同步起点，之后每 5 分钟检查一次新邮件。电脑睡眠、断网或关闭进程期间不会及时提醒，恢复后会追赶新增 UID。

配置脚本不会进行联网测试或发送测试消息。完成配置后的首次运行会验证邮箱登录，GLM 和飞书在第一批相关新回复到达时使用；没有模拟数据会被当成真实提醒发送。

### loliapi 配置

本机 `local/mail-monitor/config.json` 已设置为：

```json
{
  "glm_api_url": "https://loliapi.org/v1/chat/completions",
  "glm_protocol": "chat_completions",
  "glm_model": "glm-5.3"
}
```

使用 loliapi 提供的 API Key。相关邮件摘要请求会发给该网关及其上游服务。本机已用保存的 Key 读取模型列表，确认包含 `glm-5.3`；也已用合成文本验证 Chat Completions 调用。实际接入不依赖官网首页展示的模型标签。

配置时也可以填写 `https://loliapi.org/v1`，程序会补全接口路径。兼容网关默认不发送智谱官方的 `thinking` / `response_format` 扩展字段。若使用 Messages 协议，选择 `anthropic_messages` 并填写相应 `/v1/messages` 地址；当前 loliapi 配置使用已验证的 Chat Completions 接口，无需切换。

旧配置没有 `glm_api_url` 时仍沿用原来的智谱官方地址，不会静默把已有 Key 发给其他服务商。

本地网页原地址为 `http://127.0.0.1:8765/`，点击“邮箱提醒与 AI 摘要”，或访问 `http://127.0.0.1:8765/mail-monitor`。需要同时运行原来的“启动本地网页.cmd”。此地址只在运行程序的电脑上有效，手机查看摘要使用飞书。报告也可离线打开 `local/mail-monitor/report.html`。

## 收取范围与起点

- 只读选择文件夹，使用 `BODY.PEEK`，不修改邮箱已读状态、不移动或删除邮件。
- 先取邮件头，只对已知 patch 标题、已知回复链、自己的新 patch 投递下载正文。无关邮件正文不会下载或提交给 GLM。非常规改标题、缺失回复链、尚未被收录且不是本人投递的线程可能暂时识别不到，仍需 Lore 补齐。
- `Message-ID` 同时与本地邮箱记录和已有公开归档去重，抄送副本、重复运行不重复提醒。自己的投递作为上下文保留，但不提醒自己。
- 一轮检查最多读取 200 封新邮件的邮件头，下轮继续追赶。同一轮、同一 series 的回复合成一批，默认每轮最多调用 GLM 5 次、飞书 10 次。
- GLM 仅分析每批至多 30 封新邮件；每封正文最多 6000 字符、合计 24000 字符，引用和 diff 会省略。不把整个邮箱或所有旧线程交给模型，缺少上下文时应自行核对原文。
- 重置 UIDVALIDITY 时重新扫描 UID 空间并去重，以服务器的 INTERNALDATE 排除首次启用前的旧邮件；缺少该时间时暂停该批次，避免历史邮件被误报。
- 默认监测 `INBOX`。若学校邮箱规则将 patch 移到独立文件夹，请修改 `local/mail-monitor/config.json` 的 `imap.folder`；文件夹目前应使用 ASCII / IMAP modified UTF-7 名称。改变账号、服务器或文件夹后应使用新的本地数据目录，旧状态不能混用。

## 本地数据与公开网页

所有邮箱记录、草稿和运行状态写在 `local/mail-monitor/`，该目录已被 Git 忽略。凭据在 `credentials.json` 中使用 Windows DPAPI 加密，只能由同一 Windows 用户解密；运行时在进程内读取，不写进 HTML 或命令行参数。配置入口直接使用 Python，不需要调整 PowerShell 脚本策略。请仍保护好 Windows 账户和本地文件访问权限。

其他系统可使用 `PATCH_IMAP_PASSWORD`、`PATCH_GLM_API_KEY`、`PATCH_FEISHU_WEBHOOK` 和可选的 `PATCH_FEISHU_SECRET` 进程环境变量供给凭据。

本地邮箱阅读页包含可能尚未公开的邮件，发布程序会拒绝发布带有邮箱来源标记的报告。现有 GitHub Pages 继续从公开 Lore mbox 更新，不会被邮箱私信自动覆盖；之后下载的 Lore 归档可补齐公开讨论。AI 摘要当前只在本地报告和飞书中展示。

后台程序与现有每日 Lore 更新流程独立运行。首次配置不会自动安装开机任务，也不会改变已有定时任务；如果需要全天运行，可后续迁移到常在线 Windows 主机。迁移凭据需要在目标 Windows 用户下重新配置。

## 故障和重试

- 邮箱失败时保留上次同步位置和已有报告；本地页面显示错误。网络及认证错误不会打印服务器原文，以免泄露凭据。
- GLM 失败时仍发基本的新邮件提醒；后续最多再尝试两次补齐本地摘要，不重复发送已成功的飞书提醒。
- 飞书明确拒绝的请求最多自动尝试三次。网络超时、服务器错误、响应格式异常或进程在发送期间中断时，标记“发送结果未确认”，不会自动重发。先检查飞书是否已收到，再决定重试，以避免重复。
- 在没有持续监测进程运行时，可执行 `python -X utf8 mail_monitor.py --retry-uncertain`，重试未确认 / 已失败的批次；该操作可能重复发送此前已收到但未确认的提醒。
- 证书验证和服务器名称校验始终启用。IMAP 与 API 请求优先使用 Python 默认信任库；若 Python 没有默认 CA 证书及证书目录，自动加载 `certifi` 提供的根证书。本机 Inkscape 自带 Python 已附带 `certifi`，但默认 SSL 信任库为空，因此需要此后备加载。其他精简 Python 若提示缺少根证书库，可用运行脚本的同一 Python 执行 `python -m pip install certifi`，然后重新启动监测。
- Windows 上若 Python 仍无法构建 API 服务的证书链，程序使用 Windows 原生 HTTPS 进行正常证书校验；仅在 TLS 握手验证失败、请求尚未发出时使用此后备方式。普通网络超时不会自动换通道重发。凭据与请求正文通过标准输入传给固定的 HTTPS 调用程序，不出现在命令行参数中，禁止自动跟随重定向。
- 如单位网络需要自有 CA，可在运行前设置 `PATCH_MONITOR_CA_FILE` 为可信 CA 文件路径。显式设置此项、`SSL_CERT_FILE` 或 `SSL_CERT_DIR` 后，程序尊重指定的信任配置，不额外加入 `certifi` 或切换到 Windows HTTPS。IMAP 的 `SSLCertVerificationError` 发生在登录之前，应检查 CA、服务器名称与系统时间，无需因此重新填写授权码。
- 程序使用进程锁，防止重复启动同时推进游标或发送提醒。关掉监测窗口即可停止；本地阅读页仍可浏览已保存邮件。

开发验证：`python -X utf8 -m unittest test_mail_monitor -v`。测试使用本地假邮箱和替身接口，不发送真实提醒、不调用计费 API。

官方参考：[飞书自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)、[GLM 对话补全 API](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)。
