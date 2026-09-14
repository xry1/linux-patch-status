# Linux Patch Status

Record my linux patch status.

仓库：[xry1/linux-patch-status](https://github.com/xry1/linux-patch-status)。
Pages 发布地址：<https://xry1.github.io/linux-patch-status/>（首次启用并部署成功后可访问）。

可部署到 GitHub Pages 的 Linux patch 邮件工作台。页面内包含完整邮件，
可搜索主题、筛选状态、阅读 Thread messages、隐藏引用和导出 JSON。

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
