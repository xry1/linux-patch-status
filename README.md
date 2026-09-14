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

## Applied 校准与核实

Applied 分成两类依据：主线历史已核实，或归档中有明确的接收回复。
`queued before finish_queued` 等代码描述、条件语句及“其他实现已经合入”的讨论不构成接收证据。

2026-09-14 校准结果：56 个收录主题 = 52 个补丁 + 4 个系列封面。
其中 44 个主题的提交已验证为主线历史的祖先，12 个主题有明确接收邮件但尚未验证主线归属。
44 个主线提交中，43 个由 Runyu Xiao 署名作者，1 个是其 Signed-off-by 参与的提交。
每条依据可以在阅读器中查看。主线验证是指定日期、指定 HEAD 的历史归属核实，不保证代码此后没有被修改或回退。

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

统计按主题及邮件信号推断，不等于主线 commit 数；Applied 也可能仅指某个维护者树。
CVE 线索需要单独核对，不能据此认定某个 patch 获得了 CVE。
