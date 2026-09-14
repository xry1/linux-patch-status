# Linux Patch Status

Record my linux patch status.

仓库：[xry1/linux-patch-status](https://github.com/xry1/linux-patch-status)。
Pages 发布地址：<https://xry1.github.io/linux-patch-status/>（首次启用并部署成功后可访问）。

可部署到 GitHub Pages 的 Linux patch 邮件工作台。页面内包含完整邮件，
可搜索主题、筛选状态、阅读 Thread messages、隐藏引用和导出 JSON。

## 状态与阅读工作台

- 接收、Reviewed-by、Acked-by、Tested-by 和待办是独立信号，可以重合。
- 同标题的 v1/v2 等版本合并展示。只按规范化标题合并；截断的 stable 失败通知还需匹配修复提交，避免误合并相似标题。
- 最新版本作为主状态；旧版本反馈留在时间线。stable 回移失败只影响对应分支，不覆盖维护者树收录。
- 补丁、系列封面信、讨论分开计数；统计不等于唯一补丁数或主线 commit 数。
- “最近变化”保存最后一次内容变化；“未读回复”保存在当前浏览器，不跨设备同步。“等待反馈”按最新投递日期计算，不等于失败。
- “全部已读”一次标记当前归档所有主题，包含筛选外的邮件；之后导入的新邮件仍显示为未读。
- 支持标题、全文、发件人、commit ID、CVE 搜索，以及类型、子系统、活动日期筛选和分页。
- 每条证据可直达本地邮件；地址栏和“复制邮件直达链接”可分享具体邮件。阅读器支持版本筛选、引用折叠和 diff 高亮。
- `Fixes:` 引入提交与修复/回移引用分开。邮件提及与 OSV 命中仅显示候选，查询失败也单独显示；没有经过外部证据核实的记录不会标记为已核实。

本次功能升级使用已有归档作为变化跟踪起点，不把重新分类伪装成新邮件。

## 邮箱与飞书提醒

新增 163 学校 / 企业邮箱 IMAP 增量监测，GLM 中文摘要、待办及英文回复草稿，以及飞书群机器人提醒。默认每 5 分钟检查，同一轮的 series 回复合并推送；首次运行不推送历史邮件。

先双击“配置邮箱提醒.cmd”，再运行“启动邮箱监测.cmd”。凭据通过隐藏输入收集，用 Windows DPAPI 加密保存在 Git 忽略的本地目录。具体设置见 [邮箱接入说明](MAIL_MONITOR.md)。

GLM 支持自定义服务商地址和协议；本机使用 loliapi 的 Chat Completions 接口及 `glm-5.3`。配置界面可修改 API 地址、模型和服务商 Key，旧配置仍兼容智谱官方接口。

本地网页新增“邮箱提醒与 AI 摘要”入口。新邮箱邮件及 AI 草稿仅保存在本机，GitHub Pages 仍由公开 Lore 归档更新；发布脚本会拒绝把私人邮箱报告发布出去。该功能尚需用户配置客户端授权码、GLM API Key 和飞书机器人后才能实际联网运行。

## Patch series 分组

默认按系列展示，可切换“仅看系列”或“逐条主题”。点击系列标题展开封面和按 `1/N、2/N` 排序的子补丁；系列作为一个分页项目，不会在两页间拆开。阅读器支持在同系列主题间切换，也能从子补丁原始邮件直接打开被回复的封面。

2026-09-14 的归档关联出 11 组系列，包含 28 个子补丁和 12 个封面主题。其中 `nvmet` 的 v1 封面与改名后的 v2/v3 封面归入同一系列。4 组系列的最新成员均已有主线证据，封面显示对应的系列进度。

关联依据是用户原始投递中的编号、版本和 `In-Reply-To` / `References`。相同封面的不同版本归在一起；改名封面需要明确邮件关联及重合成员。共同回复某封邮件、标题相似或投递日期接近，本身都不足以合并。stable 批量回移邮件不会混入原始 series。

系列进度按最新系列投递列出的子补丁主题的当前状态汇总；早期封面、旧成员和每次投递的编号组成仍可查看。缺失或重复编号会显式提示，不能误报全部接收。分组不改写各子补丁的状态、邮件、未读 ID 或直达链接。

Applied 按投稿去重：独立补丁计 1 个，同一系列的封面、子补丁及历史版本合计最多计 1 个。最新成员中已有子补丁接收的系列计 1 个，部分接收会标明进度；仅封面有当前接收证据时也计 1 个，但不推断所有子补丁已接收。已知子补丁回退不能由封面的旧接收信号覆盖，已从最新版本移除的成员不会重新计入。切换分组、逐条展示不会改变总数。

JSON 导出的 `applied_submissions` 保存去重总数及每项对应的主题和依据 ID；`signal_counts.applied` 使用此总数，原始主题信号保存在 `topic_signal_counts`。其余信号仍按主题计数，`signal_count_units` 标明各统计单位。

搜索或筛选命中系列内任一主题时保留系列上下文；未命中的兄弟主题会明确标注。“补丁仅有接收邮件”筛选只列实际补丁，系列封面的整体进度由分组单独展示。重新导入 mbox 会重新计算分组并保留所有归档邮件。

## Applied 校准与核实

Applied 分成两类依据：主线历史已核实，或归档中有明确的接收回复；已核实回退的主题显示 Reverted，不再计入当前 Applied。
`queued before finish_queued` 等代码描述、条件语句及“其他实现已经合入”的讨论不构成接收证据。

2026-09-14 按系列去重后：Applied 为 **47 个投稿 = 42 个独立补丁 + 5 组系列**，其中 1 组仅部分接收（`mtd: spi-nor: core: Fix RWW wait locking`，1/2），4 组全部子补丁已核实进入主线。
底层仍保留 55 个当前收录主题的信号（51 个补丁 + 4 个封面），其中 43 个有主线证据，12 个有明确接收邮件但尚未验证主线归属；这不是去重后的 Applied 总数。另有 1 个 Reverted。
主线历史共有 44 个主题；`gtp: annotate PDP lookups under RTNL` 的原始提交 `0be5c3f0fbef3679f50f345b9237b8f9ea5de4e9` 被 `860b693bca593c10e8294b79648799d96f6953d1` 明确回退，因此移出当前 Applied。
44 个主线提交中，43 个由 Runyu Xiao 署名作者，1 个是其 Signed-off-by 参与的提交。
每条依据可以在阅读器中查看。主线验证是指定日期、指定 HEAD 的历史归属核实；回退核实仅限已收集的提交，并未全面扫描所有后续回退，不保证代码此后没有被修改。
原始接收邮件、主线提交和回退提交均保留在时间线。只有验证了主线祖先关系、且 `This reverts commit` 精确指向原 SHA 的证据才撤销 Applied；建议或投递 revert 的邮件本身不会确认回退。回退提交自身被已核实提交再次回退时，可以恢复 Applied；同标题的新提交按各自 SHA 判断。

主线核实同时检查题目和作者 / Signed-off-by 归属。标题相同但不属于用户的提交仅作为参考展示，不能增加 Applied 计数。
`applied_verifications.json` 保存核对时的主线 HEAD、提交、祖先关系和归属；导入 mbox 时自动沿用这些证据，不会退回仅靠关键词判断。
`docs/applied-audit.json` 保存本次校准前后变化和逐主题依据。这份报告是本次校准的快照。

在 Windows PowerShell 中重新核实当前主线（只读访问 GitHub，全部成功才替换证据文件）：

```powershell
./Verify-Applied.ps1
# 如当前网络需要已有代理：
./Verify-Applied.ps1 -Proxy 'http://127.0.0.1:7897'
python build_pages.py
```

补充已知回退证据（沿用快照中的主线 HEAD，不重新请求所有原始提交）：

```powershell
./Verify-Applied.ps1 -RevertsOnly -RevertCommit '860b693bca593c10e8294b79648799d96f6953d1'
python build_pages.py
```

正常运行 `Verify-Applied.ps1` 时会重新核实并保留已有回退证据，不会因只搜索用户 Signed-off-by 而丢掉其他作者的回退提交。

核实程序不更改系统证书或全局代理。搜索或比较失败时保留此前证据。
只更新证据时应同步本地工作台使用的 `applied_verifications.json`，并在没有导入任务运行时重新启动本地服务。
日常邮件更新仍可离线重建；不会自动发起 GitHub 核实，也不会执行新的 CVE 查询。

## 更新状态

网页分别显示最近检查归档、邮件内容更新和 GitHub Pages 最近成功部署时间。
前两项来自导入记录；部署时间通过公开的 GitHub Actions API 查询，网络或限额导致失败时会显示无法读取，并保留部署记录链接。

即使新归档没有新增邮件，也应运行发布脚本：脚本保留内容未变化的 `docs/index.html`，仅更新很小的 `docs/sync-status.json`。
GitHub Pages 仍需一次部署才能公开新的检查结果。

下载失败或遇到需要人工验证时，可保留原有邮件并发布检查失败状态：

```powershell
python publish_pages.py --sync-error "Lore 下载需要人工验证"
```

重复执行发布脚本不会代表重新检查了 Lore；只有导入归档才更新成功检查时间。

运行状态与发布回归测试：`python -m unittest test_analysis`。

网站是公开的静态归档；邮件正文、邮件地址及导出的 JSON 都可由访客读取。
上传原始 mbox 和解析在本地完成，仓库只需要保存生成的网站及相关脚本。
网页不会自动访问 Lore，也不需要在线 Python 服务。

## 首次上线

1. 在自己的 GitHub 账号创建一个公开仓库，例如 `linux-patch-status`。
   使用空仓库，不勾选 README、license 或 .gitignore，避免与本地初次提交冲突。
2. 在此目录打开终端，确认有 Python 3.10+ 和 Git。
3. 如果尚未配置 Git 提交身份，执行下面两条命令；只影响此仓库。

```powershell
git init --initial-branch=main
git config user.name "Joexile"
git config user.email "89785826+xry1@users.noreply.github.com"
```

4. 此项目已连接到 `xry1/linux-patch-status`，首次发布：

```powershell
python publish_pages.py --repository xry1/linux-patch-status
```

Git 如果弹出登录提示，请在自己的浏览器中登录；不需要把 token 写入代码或发给别人。
脚本不会替你创建远程仓库，也不会强制推送或改变已有 origin。

5. 在仓库 **Settings → Pages** 设置：

- Source：**Deploy from a branch**
- Branch：**main**
- Folder：**/docs**
- 点击 **Save**

GitHub 完成部署后，地址通常为 `https://你的用户名.github.io/linux-patch-status/`。
以 Settings → Pages 显示的实际地址和部署结果为准。
后续每次 push 到 main，GitHub 会重新发布 docs 目录。

如果远程仓库已经有提交，应先 `git clone` 该仓库，再将站点文件放入克隆目录。
不要另外建立无关的本地历史，也不要使用强制推送。

## 日常更新：沿用本地工作台

1. 在 Lore 搜索结果页下载 **full threads** 的 `.mbox.gz`。
2. 在原来的本地工作台导入归档，等待“导入完成”。
3. 双击本目录的 **发布网站.cmd**。

在当前工作区内，脚本会读取相邻 `outputs/runyu-patch-status-cve.html` 的最新内容，
生成 `docs/index.html` 并提交、推送。**生成网站.cmd** 只生成页面，可先检查再发布。
本地工作台无需关闭；不要在归档仍在处理时导出，以免导出上一次的数据。

## 独立使用这个仓库

如果只克隆了这个仓库，没有原来的 outputs 目录，可以直接将新归档合并进站点：

```powershell
python build_pages.py --archive "C:\路径\新的归档.mbox.gz"
python publish_pages.py
```

也可以把 `.mbox.gz` 拖到 **生成网站.cmd** 或 **发布网站.cmd** 上。
导入按 Message-ID 去重；重复或部分归档会与已有站点数据合并，保留历史邮件。
原始归档和临时文件不会被发布脚本加入 Git。
如果指定的本地报告缺少站点已有的邮件，导出会停止，避免用旧报告覆盖新数据。

从指定的本地报告生成并发布：

```powershell
python publish_pages.py --report "C:\路径\runyu-patch-status-cve.html"
```

## 文件说明

- `docs/index.html`：包含数据和全部邮件正文的站点首页。
- `docs/update.html`：公开的更新方式说明。
- `build_pages.py`：导出本地报告，或合并新归档。
- `publish_pages.py`：构建、提交指定文件并推送；遇到冲突停止，不强制覆盖。
- `patch_status_dashboard.py` / `patch_dashboard_template.html`：邮件解析和页面模板。

Applied 按投稿去重，其余统计按主题及邮件信号推断，均不等于主线 commit 数；Applied 也可能仅指某个维护者树。
CVE 线索需要单独核对，不能据此认定某个 patch 获得了 CVE。
