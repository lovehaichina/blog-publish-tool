# Fuwari 博客一键发布工具 (Blog Publish Tool)

Windows **单文件 exe** 发布工具，**完全自包含**——内置 Python 界面和 Node.js 构建运行时，任何 Windows 10/11（64 位）**双击即用，无需安装任何环境**。自动完成：**本地构建（内置 Node）→ SFTP 上传到 VPS → 刷新浏览器看到新文章**。

> 配套博客工程：[fuwari-blog](https://github.com/lovehaichina/fuwari-blog)

![GitHub stars](https://img.shields.io/github/stars/lovehaichina/blog-publish-tool) ![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg) ![Platform](https://img.shields.io/badge/platform-Windows-0078D6) ![Type](https://img.shields.io/badge/type-single--exe-green)

---

## 为什么需要它？

- **写博客不熟命令行**：`pnpm build`、`scp`、`ssh` 一堆命令，记不住、易出错。
- **本机不想装环境**：Node / pnpm / Python 版本冲突、装半天——工具全内置。
- **发布流程要可重复**：构建、上传、日志、出错提示，一个界面全搞定。
- **改博客设置要快**：头像、昵称、社交链接不用翻源码，界面里改完即写回并发布。

## 功能特性

- **一键构建发布**：内置 Node v22 运行时，自动执行构建并上传，全程日志实时显示。
- **仅上传模式**：对已构建的 dist 目录直接上传（本地预览满意后再上线）。
- **新建文章**：在工程 `src/content/posts/` 生成当日 MD 模板并自动打开（draft: true 草稿机制）。
- **博客设置快捷修改**：界面内直接改标题/副标题/昵称/简介/头像/横幅/社交链接，写回 `src/config.ts` 并自动备份。
- **模板使用说明**：内置 frontmatter 字段、图片/视频/附件写法说明，写文章不查文档。
- **记住密码**：VPS 连接信息保存到同目录 `config.json`，换机重配成本低。
- **自检模式**：`--self-test` 一键验证程序完整性与运行环境。
- **零依赖交付**：单文件 exe，拷到任何 Win10/11 直接双击。

## 快速上手（从零到上线）

### 第 1 步：准备

- 一个完整的 `fuwari` 博客工程目录（含 node_modules 依赖，可从 [fuwari-blog](https://github.com/lovehaichina/fuwari-blog) 克隆后 `pnpm install`）
- 一台 VPS（SSH 可用），目标目录如 `/var/www/html`

### 第 2 步：运行

```bash
# 方式 A：直接运行源码（需 Python 3.8+）
pip install paramiko
python publish_tool.py

# 方式 B：使用打包好的 exe（推荐，无需任何环境）
双击 博客一键发布.exe
```

### 第 3 步：配置并发布

1. 选择模式：**构建并发布**（选 fuwari 工程目录）/ **仅上传**（选已构建的 dist 目录）
2. 填 VPS 设置：IP、端口（默认 22）、用户名、密码；可勾选「记住密码」
3. 点 **开始发布** → 日志区实时显示构建与上传进度
4. 完成后刷新浏览器即可看到

## 界面功能

| 区块 | 作用 |
|---|---|
| ① 发布模式 | 构建并发布（内置 Node 自动构建）/ 仅上传已构建目录 |
| ② 本地目录 | 工程根目录（含 package.json）或 dist 目录 |
| ③ VPS 设置 | 连接信息 + 远程目标路径（默认 /var/www/html），保存到 config.json |
| ④ 运行日志 | 构建输出 + 上传进度实时显示 |
| 仅构建(生成dist) | 只构建生成 dist，不上传（本地预览用） |
| 新建文章 | 生成当日 MD 模板并自动打开 |
| 博客设置 | 图形化修改标题/头像/社交链接等（读写 src/config.ts） |
| 模板使用说明 | 内置 frontmatter 与多媒体写法说明 |

## 发文章标准流程

1. 点「新建文章」→ 生成 `2026-10-15-新文章.md` 并自动打开
2. 填标题、正文；图片放 `public/images/`、视频放 `public/videos/`、附件放 `public/files/`
3. 发布前把 frontmatter 里 `draft: true` 改为 `false`（或删掉该行）
4. 点「开始发布」→ 刷新站点

## 配置说明

运行后自动在同目录生成 `config.json`（参考 `config.json.example`）：

| 字段 | 说明 |
|---|---|
| `host` / `port` / `username` / `password` | VPS 连接信息（SFTP） |
| `remote_path` | 上传目标目录，默认 `/var/www/html` |
| `mode` | `build`=构建并发布；`upload`=仅上传 |
| `project_dir` / `dist_dir` | 工程目录 / dist 目录 |

## 二次开发 / 打包

源码结构（单文件 `publish_tool.py`，约 44KB，无框架依赖）：

```bash
# 直接运行源码（开发调试）
pip install paramiko
python publish_tool.py

# 打包为普通单文件 exe
pyinstaller --onefile --noconsole --name 博客一键发布 publish_tool.py

# 打包为完全自包含版（内置 Node 运行时，体积约 50MB）
pyinstaller --onefile --noconsole --name 博客一键发布 \
  --add-binary "node-v22.23.3-win-x64.zip;." publish_tool.py
```

- 首次运行自包含版会在同目录解压出 `node_runtime/`（约 30MB，可删，会自动重建）
- 界面逻辑、构建调用、SFTP 上传、config.ts 读写均在单文件内，改哪里一目了然，欢迎 Fork 和 PR

## 常见问题

**Q：exe 双击打不开 / 被杀毒拦截？**
单文件 PyInstaller exe 首次启动需解压，稍慢属正常；个别杀软误报可加信任。源码方式 `python publish_tool.py` 不受影响。

**Q：构建失败？**
日志区会显示 astro 构建输出。常见原因：工程目录不完整（缺 node_modules）、`src/config.ts` 存在残留备份（`config.ts.bak`，删掉即可）、路径含特殊字符。

**Q：上传后网站没变化？**
确认上传目标目录与 Caddy/Nginx 的站点根目录一致；上传是覆盖式，若页面有缓存，强制刷新（Ctrl+F5）。

**Q：密码安全吗？**
勾选「记住密码」会以明文存在同目录 `config.json`——请妥善保管该文件；不勾选则每次手输，更安全。

**Q：支持 macOS/Linux 吗？**
工具为 Windows 设计（exe + 内置 Node win 版）。macOS/Linux 用户可直接用源码 + 系统 Node：`python publish_tool.py`（需自行安装 Node/pnpm）。

## License

[GPL-3.0](LICENSE) © 2026 luohai
