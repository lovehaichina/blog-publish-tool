# -*- coding: utf-8 -*-
"""
Fuwari 博客一键发布工具
- 本地构建（pnpm build）→ SFTP 上传 dist 到 VPS /var/www/html/
- 支持仅上传已构建目录
- 新建文章（MD 模板 + 说明）
- 配置保存于本程序同目录 config.json
用法: 双击运行（GUI）；命令行 --self-test 做逻辑自检
"""
import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import traceback
import zipfile
import re
from datetime import date
from pathlib import Path

APP_DIR = Path(sys.argv[0]).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

DEFAULTS = {
    "host": "YOUR_VPS_IP",
    "port": 22,
    "username": "root",
    "password": "",
    "remember_password": False,
    "remote_path": "/var/www/html",
    "mode": "build",          # build=构建并发布, upload=仅上传
    "project_dir": "",
    "dist_dir": "",
}

MD_TEMPLATE = """---
title: {title}
published: {date}
description: 一句话摘要（列表页显示）
tags: [随笔]
category: 随笔
draft: true
---

这里是正文，支持 Markdown 语法。

## 图片
![图片说明](/images/2026/xxx.webp)

## 视频（小文件，大视频建议外链 B 站）
<video controls src="/videos/xxx.mp4" style="width:100%"></video>

## 附件
[下载文件](/files/xxx.pdf)
"""

USAGE_TEXT = """【模板字段说明】
title      文章标题（必填）
published  发布日期 YYYY-MM-DD（必填）
description 摘要，列表页显示
tags       标签，可多个：[技术, 随笔]
category   分类，只能一个：随笔
draft      true=草稿不发布；改成 false 或删掉该行=发布
image      封面图，如 /images/2026/cover.webp（可选）
updated    修改日期（可选）

【多媒体怎么放】
所有图片/视频/附件放到本地工程 public 目录下：
  public\\images\\   → 文章里写 ![说明](/images/xxx.png)
  public\\videos\\   → <video controls src="/videos/xxx.mp4"></video>
  public\\files\\    → [下载](/files/xxx.pdf)
格式不限：png/jpg/jpeg/gif/svg/webp/avif 都支持。
大视频建议传 B 站/YouTube 后嵌入 iframe，别直接放 VPS（上行慢、无 CDN）。

【发布流程】
1. 选择模式：构建并B（选 fuwari 工程目录）/ 仅上传（选已构建的 dist 目录）
2. 填 VPS 信息（首次），保存设置
3. 点「开始发布」→ 自动构建（如有）→ 上传到服务器
4. 完成后刷新浏览器即可看到

【注意】
- 构建使用 exe 内置的 Node 运行时，本机无需安装 Node/pnpm
- 工程目录需完整（含 node_modules 依赖），首次使用把整个 fuwari 工程文件夹一起拷贝
- 上传是覆盖式：新文件写入，服务器上已不存在的旧文件不会自动删除
"""

# ---------- 博客设置（src/config.ts 读写） ----------
COMMON_ICONS = [
    "fa6-brands:twitter", "fa6-brands:github", "fa6-brands:steam",
    "fa6-brands:weibo", "fa6-brands:bilibili", "fa6-brands:zhihu",
    "fa6-brands:weixin", "fa6-brands:qq", "fa6-brands:tiktok",
    "fa6-brands:youtube", "fa6-brands:telegram", "fa6-brands:discord",
    "fa6-brands:reddit", "fa6-brands:facebook", "fa6-brands:instagram",
    "fa6-brands:x-twitter", "material-symbols:smart-display",
    "fa6-solid:envelope", "fa6-solid:rss", "fa6-solid:link",
]
# 内置图标集（缺一不可，用于保存时校验）
ICON_SETS = ["fa6-brands", "fa6-solid", "fa6-regular", "material-symbols"]

_icon_cache = {}


def validate_icons(project_dir, links):
    """校验社交链接的 icon 是否存在于工程已安装图标集。返回 (ok, bad_list)。
    bad_list: [(名称, icon, 原因)]"""
    bad = []
    for l in links:
        icon = (l.get("icon") or "").strip()
        if not icon:
            continue
        if ":" not in icon:
            bad.append((l.get("name", ""), icon, "格式应为 集合:图标名，如 fa6-brands:github"))
            continue
        s, name = icon.split(":", 1)
        if s not in ICON_SETS:
            bad.append((l.get("name", ""), icon,
                        f"图标集 {s} 未安装（仅支持 {'/'.join(ICON_SETS)}）"))
            continue
        icons_json = Path(project_dir) / "node_modules" / "@iconify-json" / s / "icons.json"
        if s not in _icon_cache:
            if not icons_json.exists():
                bad.append((l.get("name", ""), icon, f"图标集 {s} 数据缺失（工程 node_modules 不完整）"))
                continue
            try:
                _icon_cache[s] = json.loads(icons_json.read_text(encoding="utf-8"))
            except Exception as e:
                bad.append((l.get("name", ""), icon, f"读取图标集 {s} 失败：{e}"))
                continue
        data = _icon_cache[s]
        if name not in data.get("icons", {}) and name not in data.get("aliases", {}):
            bad.append((l.get("name", ""), icon, f"图标 {name} 不存在于 {s}（去 icones.js.org 查正确代码）"))
    return (True, []) if not bad else (False, bad)


def _esc(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _config_block(text, marker):
    """取 export const marker = { ... } 的块文本（截至下一个 export const）。"""
    start = text.find(marker)
    if start < 0:
        return None
    end = text.find("export const", start + 1)
    return text[start:end if end > 0 else len(text)]


def parse_blog_config(project_dir):
    """读取工程 src/config.ts，返回可编辑字段 dict。返回 (data, err)。"""
    cfg_ts = Path(project_dir) / "src" / "config.ts"
    if not cfg_ts.exists():
        return None, f"未找到配置文件：{cfg_ts}"
    text = cfg_ts.read_text(encoding="utf-8")

    def grab(pattern, block=None, default=""):
        src = block if block is not None else text
        m = re.search(pattern, src)
        return m.group(1).strip() if m else default

    site = _config_block(text, "export const siteConfig") or ""
    prof = _config_block(text, "export const profileConfig") or ""
    data = {
        "title": grab(r'title:\s*"([^"]*)"', site),
        "subtitle": grab(r'subtitle:\s*"([^"]*)"', site),
        "avatar": grab(r'avatar:\s*"([^"]*)"', prof),
        "name": grab(r'name:\s*"([^"]*)"', prof),
        "bio": grab(r'bio:\s*"([^"]*)"', prof),
    }
    bm = re.search(r'banner:\s*\{[^}]*?src:\s*"([^"]*)"', site)
    data["banner"] = bm.group(1).strip() if bm else ""

    links = []
    lm = re.search(r"links:\s*\[(.*?)\]\s*,?\s*\};", prof, re.S)
    if lm:
        for item in re.finditer(
                r'\{\s*name:\s*"([^"]*)"\s*,.*?icon:\s*"([^"]*)"\s*,.*?url:\s*"([^"]*)"\s*,?\s*\}',
                lm.group(1), re.S):
            links.append({"name": item.group(1), "icon": item.group(2), "url": item.group(3)})
    data["links"] = links
    return data, None


def _serialize_links(links):
    if not links:
        return " [\n        ],"
    lines = [" ["]
    for l in links:
        lines.append("            {")
        lines.append(f'                name: "{_esc(l["name"])}",')
        lines.append(f'                icon: "{_esc(l["icon"])}",')
        lines.append(f'                url: "{_esc(l["url"])}",')
        lines.append("            },")
    lines.append("        ]")
    return "\n".join(lines)


def write_blog_config(project_dir, data):
    """把字段写回 src/config.ts（先备份 .ts.bak，仅替换目标字段）。返回 (ok, msg)。"""
    cfg_ts = Path(project_dir) / "src" / "config.ts"
    if not cfg_ts.exists():
        return False, f"未找到配置文件：{cfg_ts}"
    text = cfg_ts.read_text(encoding="utf-8")
    try:
        # 备份到工程根（不能在 src 内：ImageWrapper.astro 的 import.meta.glob("../../**") 会把它当模块导入导致构建失败）
        (cfg_ts.parent.parent / "config.ts.bak").write_text(text, encoding="utf-8")
    except Exception:
        pass

    def sub_val(src, pattern, value):
        return re.sub(pattern, lambda m: m.group(1) + _esc(value) + m.group(2), src)

    site = _config_block(text, "export const siteConfig") or ""
    prof = _config_block(text, "export const profileConfig") or ""
    new_site = sub_val(sub_val(sub_val(site,
                                       r'(title:\s*")[^"]*(")', data.get("title", "")),
                               r'(subtitle:\s*")[^"]*(")', data.get("subtitle", "")),
                       r'(banner:\s*\{[^}]*?src:\s*")[^"]*(")', data.get("banner", ""))
    new_prof = sub_val(sub_val(sub_val(prof,
                                       r'(name:\s*")[^"]*(")', data.get("name", "")),
                               r'(bio:\s*")[^"]*(")', data.get("bio", "")),
                       r'(avatar:\s*")[^"]*(")', data.get("avatar", ""))
    new_prof = re.sub(r"links:\s*\[.*?\]\s*,?\s*\};",
                      lambda m: "links:" + _serialize_links(data.get("links", [])) + ",\n};",
                      new_prof, count=1, flags=re.S)

    new_text = text.replace(site, new_site).replace(prof, new_prof)
    if new_text == text:
        return False, "未识别到可修改的字段（config.ts 格式与预期不符）"
    try:
        cfg_ts.write_text(new_text, encoding="utf-8")
        return True, "已写入 src/config.ts（备份：config.ts.bak）"
    except Exception as e:
        return False, f"写入失败：{e}"


def overwrite_blog_image(project_dir, field, src_path):
    """把本地图片复制覆盖到 config 指向的头像/横幅文件。field='avatar'|'banner'。返回 (ok, msg)。"""
    data, err = parse_blog_config(project_dir)
    if err:
        return False, err
    rel = (data.get(field) or "").strip()
    if not rel:
        rel = "assets/images/demo-avatar.png" if field == "avatar" else "assets/images/demo-banner.png"
    if rel.startswith("/"):
        target = Path(project_dir) / "public" / rel.lstrip("/")
    else:
        target = Path(project_dir) / "src" / rel
    if target.parent.name == "images" and target.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return False, f"目标文件需为 png/jpg/jpeg/webp，当前：{target}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, target)
        return True, str(target)
    except Exception as e:
        return False, f"复制图片失败：{e}"



def load_config():
    cfg = dict(DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update({k: data[k] for k in data if k in cfg})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


# ---------- 构建 ----------
NODE_ZIP_NAME = "node-v22.23.3-win-x64.zip"  # 打包时内置的便携 Node 压缩包（保持原名）
NODE_CACHE_NAME = "node_runtime"


def resource_path(name):
    """PyInstaller 打包后资源位于 sys._MEIPASS；源码运行取脚本同目录。"""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent
    return base / name


def ensure_node_runtime(log):
    """解压内置 Node 到缓存目录，返回 node.exe 路径；失败返回 None。"""
    cache_root = APP_DIR / NODE_CACHE_NAME
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        cache_root = Path(tempfile.gettempdir()) / "blog_publish_node"
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
    node_exe = cache_root / "node.exe"
    if node_exe.exists():
        return str(node_exe)
    zip_src = resource_path(NODE_ZIP_NAME)
    if not zip_src.exists():
        return None
    log("首次使用：解压内置 Node 运行时（约 30MB，一次性）...")
    try:
        tmp = cache_root.parent / (cache_root.name + "_tmp")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_src) as z:
            top = z.namelist()[0].split("/")[0]
            z.extractall(str(tmp))
        src = tmp / top
        for item in src.iterdir():
            dst = cache_root / item.name
            if dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst, ignore_errors=True)
                else:
                    dst.unlink()
            shutil.move(str(item), str(dst))
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        log(f"解压 Node 失败：{e}")
        return None
    return str(node_exe) if node_exe.exists() else None


def run_proc(cmd, cwd, log):
    """运行子进程并实时输出日志。返回 True/False。"""
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in proc.stdout:
            if line.strip():
                log("  " + line.rstrip())
        rc = proc.wait()
        return rc == 0
    except Exception as e:
        log(f"  子进程异常：{e}")
        return False


def run_build(project_dir, log):
    """构建工程：优先内置 Node，其次系统 pnpm。返回 (ok, msg)。"""
    proj = Path(project_dir)
    if not (proj / "package.json").exists():
        return False, f"所选目录没有 package.json，不是有效的工程目录：{project_dir}"
    astro_js = proj / "node_modules" / "astro" / "astro.js"
    if not astro_js.exists():
        return False, ("工程缺少依赖（未找到 node_modules\\astro）。\n"
                       "请先在该工程目录执行一次依赖安装：npm install 或 pnpm install\n"
                       "（或直接把带 node_modules 的完整工程文件夹拷贝到本机）。")

    node_exe = ensure_node_runtime(log)
    if node_exe:
        log("开始构建（内置 Node，无需系统安装）：node node_modules/astro/astro.js build ...")
        if not run_proc([node_exe, "node_modules/astro/astro.js", "build"], proj, log):
            return False, "构建失败（astro build），请查看上方日志"
        pagefind_js = proj / "node_modules" / "pagefind" / "lib" / "runner" / "bin.cjs"
        if pagefind_js.exists():
            log("生成搜索索引：pagefind --site dist ...")
            if not run_proc([node_exe, str(pagefind_js), "--site", "dist"], proj, log):
                return False, "搜索索引生成失败（pagefind），请查看上方日志"
        return True, "构建完成（内置 Node）"

    pnpm = shutil.which("pnpm")
    if pnpm:
        log("未找到内置 Node，使用系统 pnpm 构建：pnpm build ...")
        return run_pnpm_build(pnpm, proj, log)
    return False, "构建不可用：内置 Node 缺失且系统未安装 pnpm。"


def run_pnpm_build(pnpm, proj, log):
    try:
        proc = subprocess.Popen(
            [pnpm, "build"], cwd=str(proj),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in proc.stdout:
            if line.strip():
                log("  " + line.rstrip())
        rc = proc.wait()
        if rc == 0:
            return True, "构建完成（系统 pnpm）"
        return False, f"构建失败（退出码 {rc}），请查看上方日志"
    except Exception as e:
        return False, f"构建异常：{e}"


# ---------- 上传 ----------
def sftp_upload(host, port, username, password, local_dir, remote_dir, log):
    """递归上传 local_dir 内容到 remote_dir。返回 (ok, msg)。"""
    import paramiko

    log(f"连接 {host}:{port} ...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, port=port, username=username, password=password, timeout=20,
                       banner_timeout=20, auth_timeout=20)
    except Exception as e:
        return False, f"连接失败：{e}"

    sftp = client.open_sftp()
    local_root = Path(local_dir)
    total_files = sum(1 for f in local_root.rglob("*") if f.is_file())
    done = 0
    errors = []

    def ensure_dir(remote_path):
        """逐级创建远程目录（幂等）"""
        parts = remote_path.split("/")
        cur = ""
        for p in parts:
            if not p:
                continue
            cur += "/" + p
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except IOError:
                    pass

    try:
        for f in sorted(local_root.rglob("*")):
            if f.is_file():
                rel = f.relative_to(local_root).as_posix()
                remote_file = remote_dir.rstrip("/") + "/" + rel
                try:
                    ensure_dir(remote_file.rsplit("/", 1)[0])
                    sftp.put(str(f), remote_file)
                    done += 1
                    if done % 10 == 0 or done == total_files:
                        log(f"  上传进度 {done}/{total_files}: {rel}")
                except Exception as e:
                    errors.append(f"{rel}: {e}")
        if errors:
            return False, "部分文件上传失败：\n" + "\n".join(errors[:10])
        return True, f"上传完成，共 {done} 个文件"
    finally:
        sftp.close()
        client.close()


# ---------- 新建文章 ----------
def new_post(project_dir, log):
    posts_dir = Path(project_dir) / "src" / "content" / "posts"
    if not posts_dir.is_dir():
        return None, f"未找到文章目录：{posts_dir}\n请选择 fuwari 工程目录（含 src/content/posts）。"
    today = date.today().isoformat()
    base = f"{today}-新文章"
    path = posts_dir / f"{base}.md"
    n = 1
    while path.exists():
        path = posts_dir / f"{today}-新文章-{n}.md"
        n += 1
    path.write_text(MD_TEMPLATE.format(title="新文章", date=today), encoding="utf-8")
    return path, f"已创建：{path.name}\n（draft: true 为草稿，B时改为 false）"


# ---------- 工程探测 ----------
def is_fuwari_project(p):
    """判断目录是否为 fuwari 工程（含 src/content/posts，且 package.json 有 fuwari 特征）。"""
    try:
        pkg = p / "package.json"
        if not pkg.exists():
            return False
        if not (p / "src" / "content" / "posts").is_dir():
            return False
        data = json.loads(pkg.read_text(encoding="utf-8"))
        scripts = data.get("scripts", {}) or {}
        if "new-post" in scripts or str(data.get("name", "")).lower() == "fuwari":
            return True
        return (p / "src" / "content" / "posts").is_dir()
    except Exception:
        return False


def find_fuwari_projects(max_results=5):
    """在常见位置（当前目录、exe 目录、主目录/桌面/文档/下载）有限深度探测 fuwari 工程。"""
    cand = [Path.cwd(), APP_DIR, APP_DIR.parent, Path.home(),
            Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads",
            Path.home() / "桌面", Path.home() / "文档", Path.home() / "下载",
            Path.home() / "OneDrive" / "Desktop", Path.home() / "OneDrive" / "Documents"]
    roots = []
    for p in cand:
        try:
            if p.is_dir():
                roots.append(p.resolve())
        except Exception:
            pass
    found, seen = [], set()
    skip = {"node_modules", ".git", "__pycache__", "打包临时", "打包输出",
            "node_runtime", "venv", ".venv", "site-packages"}
    for root in roots:
        if len(found) >= max_results:
            break
        try:
            for dirpath, dirnames, filenames in os.walk(str(root)):
                dirnames[:] = [d for d in dirnames if d not in skip]
                rel = Path(dirpath)
                try:
                    depth = len(rel.relative_to(root).parts)
                except ValueError:
                    continue
                if depth > 3:
                    dirnames[:] = []
                    continue
                if is_fuwari_project(rel):
                    key = str(rel).lower()
                    if key not in seen:
                        seen.add(key)
                        found.append(rel)
                        if len(found) >= max_results:
                            break
        except Exception:
            continue
    return found


# ---------- GUI ----------
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk, scrolledtext

    cfg = load_config()
    log_queue = queue.Queue()

    root = tk.Tk()
    root.title("Fuwari 博客To发布")
    root.geometry("780x640")
    root.minsize(680, 560)

    style = ttk.Style()
    try:
        style.theme_use("vista")
    except Exception:
        pass

    def log(msg):
        log_queue.put(str(msg))

    def pump_log():
        try:
            while True:
                msg = log_queue.get_nowait()
                txt_log.configure(state="normal")
                txt_log.insert("end", msg + "\n")
                txt_log.see("end")
                txt_log.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(100, pump_log)

    # ---------- 布局 ----------
    pad = {"padx": 10, "pady": 4}
    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)

    # 模式
    ttk.Label(frm, text="① 发布模式", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", **pad)
    mode_var = tk.StringVar(value=cfg.get("mode", "build"))
    row_mode = ttk.Frame(frm)
    row_mode.pack(fill="x", **pad)
    ttk.Radiobutton(row_mode, text="构建并发布（选 fuwari 工程目录，自动 pnpm build）", variable=mode_var, value="build").pack(side="left")
    ttk.Radiobutton(row_mode, text="仅上传（选已构建的 dist 目录）", variable=mode_var, value="upload").pack(side="left", padx=10)

    # 目录
    ttk.Label(frm, text="② 本地目录（工程目录 = 含 src\\content\\posts 的 fuwari 文件夹）",
              font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", **pad)
    row_dir = ttk.Frame(frm)
    row_dir.pack(fill="x", **pad)
    dir_var = tk.StringVar(value=cfg.get("project_dir") or cfg.get("dist_dir") or "")
    ent_dir = ttk.Entry(row_dir, textvariable=dir_var)
    ent_dir.pack(side="left", fill="x", expand=True)

    def browse_dir():
        d = filedialog.askdirectory(initialdir=dir_var.get() or str(Path.home()))
        if d:
            dir_var.set(d)
            log(f"已选择目录：{d}")

    def copy_dir():
        t = dir_var.get().strip()
        if not t:
            messagebox.showinfo("复制路径", "目录为空，请先选择或自动查找。")
            return
        root.clipboard_clear()
        root.clipboard_append(t)
        log(f"路径已复制到剪贴板：{t}")

    def auto_find():
        hits = find_fuwari_projects()
        if not hits:
            messagebox.showinfo("未找到",
                                "未在本机常见位置找到 fuwari 工程。\n\n"
                                "工程目录特征：包含 package.json 和 src\\content\\posts 的文件夹\n"
                                "（例如 …\\new-chat\\fuwari）。\n请点「浏览」手动选择。")
            return
        dir_var.set(str(hits[0]))
        log(f"已找到工程目录：{hits[0]}")
        if len(hits) > 1:
            log("其他候选：")
            for h in hits[1:5]:
                log("  " + str(h))

    ttk.Button(row_dir, text="自动查找", command=auto_find).pack(side="left", padx=(0, 6))
    ttk.Button(row_dir, text="浏览...", command=browse_dir).pack(side="left")
    ttk.Button(row_dir, text="复制路径", command=copy_dir).pack(side="left", padx=6)

    # VPS
    ttk.Label(frm, text="③ VPS 设置（保存在本程序同目录 config.json）", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", **pad)
    grid = ttk.Frame(frm)
    grid.pack(fill="x", **pad)
    host_var = tk.StringVar(value=cfg.get("host", ""))
    port_var = tk.StringVar(value=str(cfg.get("port", 22)))
    user_var = tk.StringVar(value=cfg.get("username", ""))
    pass_var = tk.StringVar(value=cfg.get("password", "") if cfg.get("remember_password") else "")
    remote_var = tk.StringVar(value=cfg.get("remote_path", "/var/www/html"))
    remember_var = tk.BooleanVar(value=cfg.get("remember_password", False))

    ttk.Label(grid, text="IP/域名:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
    ttk.Entry(grid, textvariable=host_var, width=22).grid(row=0, column=1, sticky="w", pady=2)
    ttk.Label(grid, text="端口:").grid(row=0, column=2, sticky="w", padx=(12, 6), pady=2)
    ttk.Entry(grid, textvariable=port_var, width=8).grid(row=0, column=3, sticky="w", pady=2)
    ttk.Label(grid, text="用户名:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=2)
    ttk.Entry(grid, textvariable=user_var, width=22).grid(row=1, column=1, sticky="w", pady=2)
    ttk.Label(grid, text="密码:").grid(row=1, column=2, sticky="w", padx=(12, 6), pady=2)
    ttk.Entry(grid, textvariable=pass_var, width=8, show="*").grid(row=1, column=3, sticky="w", pady=2)
    ttk.Checkbutton(grid, text="记住密码(明文保存)", variable=remember_var, command=lambda: save_ui()).grid(row=1, column=4, sticky="w", padx=12, pady=2)
    ttk.Label(grid, text="远程目标路径:").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=2)
    ttk.Entry(grid, textvariable=remote_var, width=30).grid(row=2, column=1, columnspan=3, sticky="w", pady=2)

    # 按钮行
    row_btn = ttk.Frame(frm)
    row_btn.pack(fill="x", **pad)

    def save_ui(quiet=False):
        """把当前界面所有字段写入 config.json（勾选记住密码、发布、保存按钮共用）。"""
        cfg.update({
            "host": host_var.get().strip(), "port": int(port_var.get() or 22),
            "username": user_var.get().strip(), "remote_path": remote_var.get().strip() or "/var/www/html",
            "mode": mode_var.get(), "remember_password": remember_var.get(),
        })
        if mode_var.get() == "build":
            cfg["project_dir"] = dir_var.get().strip()
        else:
            cfg["dist_dir"] = dir_var.get().strip()
        if remember_var.get():
            cfg["password"] = pass_var.get()
        else:
            cfg["password"] = ""
        return save_config(cfg)

    def do_save():
        ok = save_ui()
        log("设置已保存" if ok else "设置保存失败（目录可能无写权限）")
        if remember_var.get():
            log("提示：密码以明文保存在 config.json，请注意文件保管")

    def do_publish():
        host = host_var.get().strip()
        port = int(port_var.get() or 22)
        user = user_var.get().strip()
        pwd = pass_var.get()
        if not remember_var.get() and not pwd:
            messagebox.showwarning("缺少密码", "请填写 VPS 密码，或勾选「记住密码」。")
            return
        mode = mode_var.get()
        src = dir_var.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showwarning("目录无效", "请选择有效的本地目录。")
            return
        remote = remote_var.get().strip() or "/var/www/html"
        btn_publish.configure(state="disabled")

        def worker():
            try:
                dist_dir = None
                if mode == "build":
                    ok, msg = run_build(src, log)
                    if not ok:
                        log("【失败】" + msg)
                        return
                    dist_dir = str(Path(src) / "dist")
                    if not Path(dist_dir).is_dir():
                        log(f"【失败】构建完成但未找到 dist 目录：{dist_dir}")
                        return
                else:
                    dist_dir = src
                    if not (Path(dist_dir) / "index.html").exists():
                        log("【警告】所选目录未发现 index.html，确认是构建产物目录？")
                ok, msg = sftp_upload(host, port, user, pwd, dist_dir, remote, log)
                log("【成功】" + msg if ok else "【失败】" + msg)
            except Exception as e:
                log("【异常】" + traceback.format_exc())
            finally:
                save_ui()
                root.after(0, lambda: btn_publish.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def do_new_post():
        src = dir_var.get().strip()
        if not src or not Path(src).is_dir():
            # 未选目录：自动弹出选择框，选完直接生成
            d = filedialog.askdirectory(
                title="请选择 fuwari 工程目录（含 src\\content\\posts）",
                initialdir=str(Path.home()),
            )
            if not d:
                return
            dir_var.set(d)
            src = d
        path, msg = new_post(src, log)
        log(msg)
        if path:
            try:
                os.startfile(str(path))
            except Exception:
                pass

    def do_usage():
        win = tk.Toplevel(root)
        win.title("MD 模板使用说明")
        win.geometry("680x560")
        t = scrolledtext.ScrolledText(win, wrap="word", font=("Microsoft YaHei UI", 10))
        t.pack(fill="both", expand=True, padx=10, pady=10)
        t.insert("1.0", USAGE_TEXT)
        t.configure(state="disabled")

    def do_blog_settings():
        """打开 博客设置窗口：标题/副标题/昵称/简介/头像/横幅/社交链接。"""
        src = dir_var.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showwarning("目录无效", "请先选择 fuwari 工程目录（含 src\\config.ts）。")
            return
        data, err = parse_blog_config(src)
        if err:
            messagebox.showerror("读取失败", err)
            return

        win = tk.Toplevel(root)
        win.title("博客设置")
        win.geometry("760x660")
        win.minsize(680, 560)

        wrap = ttk.Frame(win, padding=12)
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text="① 站点（写回 src/config.ts）", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        g1 = ttk.Frame(wrap)
        g1.pack(fill="x", pady=(4, 2))
        title_var = tk.StringVar(value=data.get("title", ""))
        subtitle_var = tk.StringVar(value=data.get("subtitle", ""))
        name_var = tk.StringVar(value=data.get("name", ""))
        bio_var = tk.StringVar(value=data.get("bio", ""))
        avatar_var = tk.StringVar(value=data.get("avatar", ""))
        banner_var = tk.StringVar(value=data.get("banner", ""))

        def row(g, r, label, var, width=52):
            ttk.Label(g, text=label).grid(row=r, column=0, sticky="w", padx=(0, 6), pady=2)
            ttk.Entry(g, textvariable=var, width=width).grid(row=r, column=1, sticky="we", pady=2)
        g1.columnconfigure(1, weight=1)
        row(g1, 0, "站点标题:", title_var)
        row(g1, 1, "副标题:", subtitle_var)
        row(g1, 2, "昵称:", name_var)
        row(g1, 3, "简介:", bio_var)

        ttk.Label(wrap, text="② 头像 / 横幅（选图直接覆盖 config 指向的文件）",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(10, 0))
        g2 = ttk.Frame(wrap)
        g2.pack(fill="x", pady=(4, 2))
        g2.columnconfigure(1, weight=1)
        ttk.Label(g2, text="头像路径:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        ttk.Entry(g2, textvariable=avatar_var).grid(row=0, column=1, sticky="we", pady=2)

        def pick_image(field, var, label):
            fp = filedialog.askopenfilename(
                title=label, filetypes=[("图片", "*.png *.jpg *.jpeg *.webp")])
            if not fp:
                return
            ok, msg = overwrite_blog_image(src, field, fp)
            if ok:
                d2, _ = parse_blog_config(src)
                var.set(d2.get(field, ""))
                log(f"{label}已覆盖：{msg}")
                messagebox.showinfo("完成", f"图片已覆盖到：\n{msg}\n\n保存设置后构建/B 生效。")
            else:
                messagebox.showerror("失败", msg)

        ttk.Button(g2, text="选择头像图片...", width=16,
                   command=lambda: pick_image("avatar", avatar_var, "头像")).grid(row=0, column=2, padx=8, pady=2)
        ttk.Label(g2, text="横幅路径:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=2)
        ttk.Entry(g2, textvariable=banner_var).grid(row=1, column=1, sticky="we", pady=2)
        ttk.Button(g2, text="选择横幅图片...", width=16,
                   command=lambda: pick_image("banner", banner_var, "横幅")).grid(row=1, column=2, padx=8, pady=2)
        ttk.Label(g2, text="（选图即复制覆盖到该路径指向的文件，无需手动找文件）",
                  foreground="#666666").grid(row=2, column=1, sticky="w", pady=(0, 4))

        ttk.Label(wrap, text="③ 社交图标与链接（图标代码见 icones.js.org，工程内置 fa6-brands / fa6-solid / fa6-regular / material-symbols）",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(10, 0))

        link_area_wrap = ttk.Frame(wrap)
        link_area_wrap.pack(fill="both", expand=True, pady=(4, 2))
        canvas = tk.Canvas(link_area_wrap, height=150, highlightthickness=0)
        sb = ttk.Scrollbar(link_area_wrap, orient="vertical", command=canvas.yview)
        link_area = ttk.Frame(canvas)
        link_area.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=link_area, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        link_rows = []

        def add_link_row(link=None):
            row = ttk.Frame(link_area)
            row.pack(fill="x", pady=2)
            name_var = tk.StringVar(value=(link or {}).get("name", ""))
            icon_var = tk.StringVar(value=(link or {}).get("icon", ""))
            url_var = tk.StringVar(value=(link or {}).get("url", ""))
            ttk.Entry(row, textvariable=name_var, width=14).pack(side="left", padx=(0, 4))
            ttk.Combobox(row, textvariable=icon_var, values=COMMON_ICONS, width=30).pack(side="left", padx=(0, 4))
            ttk.Entry(row, textvariable=url_var, width=38).pack(side="left", padx=(0, 4))
            ttk.Button(row, text="✕", width=3, command=lambda: remove_link_row(row)).pack(side="left")
            link_rows.append((row, name_var, icon_var, url_var))

        def remove_link_row(row):
            for i, (r, n, ic, u) in enumerate(link_rows):
                if r is row:
                    row.destroy()
                    link_rows.pop(i)
                    break

        for lnk in data.get("links", []):
            add_link_row(lnk)
        if not link_rows:
            add_link_row()

        ttk.Button(wrap, text="＋ 添加链接", command=add_link_row).pack(anchor="w", pady=(4, 2))

        def save_blog():
            links = [{"name": n.get().strip(), "icon": i.get().strip(), "url": u.get().strip()}
                     for (_, n, i, u) in link_rows if n.get().strip()]
            okv, bad = validate_icons(src, links)
            if not okv:
                msg = "以下社交图标的代码无效，已阻止保存（构建会因此失败）：\n\n" + \
                    "\n".join(f"· {nm}（{ic}）：{why}" for nm, ic, why in bad) + \
                    "\n\n请改为内置图标集内的正确代码（icones.js.org 查询）。"
                messagebox.showwarning("图标无效", msg)
                return
            payload = {
                "title": title_var.get().strip(), "subtitle": subtitle_var.get().strip(),
                "name": name_var.get().strip(), "bio": bio_var.get().strip(),
                "avatar": avatar_var.get().strip(), "banner": banner_var.get().strip(),
                "links": links,
            }
            ok, msg = write_blog_config(src, payload)
            if ok:
                log("博客设置已写入 config.ts（备份 config.ts.bak，位于工程根目录）")
                messagebox.showinfo("已保存",
                                    "设置已写入 src/config.ts。\n\n接下来：点「仅构建(生成dist)」→「开始发布」线上生效。")
            else:
                messagebox.showerror("保存失败", msg)

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="保存设置", command=save_blog).pack(side="left")
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="left", padx=8)
        ttk.Label(btns, text="提示：头像/横幅选图后已立即覆盖文件，其余字段需点「保存设置」。",
                  foreground="#666666").pack(side="left", padx=12)


    def do_build_only():
        """仅构建工程（生成 dist），不上传。"""
        src = dir_var.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showwarning("目录无效", "请选择有效的工程目录。")
            return
        btn_build.configure(state="disabled")

        def worker():
            try:
                ok, msg = run_build(src, log)
                if ok:
                    dist_dir = Path(src) / "dist"
                    if dist_dir.is_dir():
                        n = sum(1 for f in dist_dir.rglob("*") if f.is_file())
                        log(f"【成功】{msg}。dist 目录已生成：{dist_dir}（{n} 个文件）")
                    else:
                        log(f"【成功】{msg}，但未找到 dist 目录。")
                else:
                    log("【失败】" + msg)
            except Exception:
                log("【异常】" + traceback.format_exc())
            finally:
                root.after(0, lambda: btn_build.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    ttk.Button(row_btn, text="保存设置", command=do_save).pack(side="left")
    ttk.Button(row_btn, text="新建文章", command=do_new_post).pack(side="left", padx=8)
    ttk.Button(row_btn, text="模板使用说明", command=do_usage).pack(side="left")
    ttk.Button(row_btn, text="博客设置", command=do_blog_settings).pack(side="left", padx=(8, 0))
    btn_build = ttk.Button(row_btn, text="仅构建(生成dist)", command=do_build_only)
    btn_build.pack(side="left", padx=(8, 0))
    btn_publish = ttk.Button(row_btn, text="开始发布", command=do_publish)
    btn_publish.pack(side="right")

    # 日志
    ttk.Label(frm, text="④ 运行日志", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", **pad)
    txt_log = scrolledtext.ScrolledText(frm, height=14, state="disabled", font=("Consolas", 9))
    txt_log.pack(fill="both", expand=True, **pad)

    def on_close():
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    def startup_auto_find():
        # 已有上次路径则直接显示；否则自动探测本机工程
        if dir_var.get().strip() and Path(dir_var.get().strip()).is_dir():
            log(f"已载入上次目录：{dir_var.get().strip()}")
            return
        hits = find_fuwari_projects()
        if hits:
            dir_var.set(str(hits[0]))
            log(f"已自动找到工程目录：{hits[0]}")
            if len(hits) > 1:
                log("其他候选：" + "；".join(str(h) for h in hits[1:4]))
        else:
            log("未自动找到 fuwari 工程目录。可点「自动查找」或「浏览」手动选择：")
            log("工程目录 = 含 src\\content\\posts 的文件夹（如 …\\new-chat\\fuwari）")

    root.after(100, pump_log)
    root.after(300, startup_auto_find)
    log("就绪。选择模式与目录 → 填 VPS 信息（首次）→ 保存设置 → 构建/B。")
    log("构建使用内置 Node 运行时，任何 Windows 直接可用，无需安装 Node/pnpm。")
    root.mainloop()


# ---------- 自检 ----------
def self_test():
    global CONFIG_PATH
    import tempfile

    results = []
    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(("[PASS] " if ok else "[FAIL] ") + name + (" | " + detail if detail else ""))

    # 1. 依赖
    try:
        import paramiko
        check("paramiko 可用", True, paramiko.__version__)
    except Exception as e:
        check("paramiko 可用", False, str(e))

    # 2. 配置读写
    tmp = Path(tempfile.mkdtemp())
    old_cfg = CONFIG_PATH
    try:
        # 用临时配置路径验证
        CONFIG_PATH = tmp / "config.json"
        cfg = load_config()
        cfg["host"] = "test.example"
        save_config(cfg)
        cfg2 = load_config()
        check("配置读写", cfg2.get("host") == "test.example", str(cfg2.get("host")))
    finally:
        CONFIG_PATH = old_cfg

    # 3. 模板生成
    posts = tmp / "src" / "content" / "posts"
    posts.mkdir(parents=True)
    path, msg = new_post(str(tmp), lambda s: None)
    check("文章模板生成", path is not None and path.exists(), msg.splitlines()[0] if msg else "")
    if path:
        content = path.read_text(encoding="utf-8")
        check("模板含 frontmatter", "title:" in content and "draft: true" in content and "tags:" in content)

    # 3b. 博客设置 config.ts 读写
    sample_cfg = """import type { ProfileConfig, SiteConfig } from "./types/config";

export const siteConfig: SiteConfig = {
    title: "罗海的博客",
    subtitle: "记录与思考",
    banner: {
        enable: true,
        src: "assets/images/demo-banner.png",
        position: "center",
    },
};

export const profileConfig: ProfileConfig = {
    avatar: "assets/images/demo-avatar.png",
    name: "罗海",
    bio: "流程与信息管理",
    links: [
        {
            name: "Twitter",
            icon: "fa6-brands:twitter",
            url: "https://twitter.com",
        },
        {
            name: "GitHub",
            icon: "fa6-brands:github",
            url: "https://github.com/saicaca/fuwari",
        },
    ],
};
"""
    (tmp / "src").mkdir(parents=True, exist_ok=True)
    (tmp / "src" / "config.ts").write_text(sample_cfg, encoding="utf-8")
    d, derr = parse_blog_config(str(tmp))
    check("博客设置解析", derr is None and d["name"] == "罗海" and len(d["links"]) == 2,
          str(derr) or f'name={d["name"]} links={len(d.get("links", []))}')
    d["name"] = "测试账号"
    d["links"] = [{"name": "微博", "icon": "fa6-brands:weibo", "url": "https://weibo.com/x"}]
    ok, wmsg = write_blog_config(str(tmp), d)
    d2, _ = parse_blog_config(str(tmp))
    check("博客设置写回", ok and d2["name"] == "测试账号" and len(d2["links"]) == 1
          and d2["links"][0]["icon"] == "fa6-brands:weibo" and d2["subtitle"] == "记录与思考",
          wmsg if not ok else f'name={d2["name"]} links={d2["links"]}')

    # 4. 工程检测
    check("工程识别", run_build(str(tmp), lambda s: None)[0] is False, "无 pnpm 时给出明确错误")

    # 5. 内置 Node 运行时
    try:
        node = ensure_node_runtime(lambda s: None)
        check("内置 Node 解压", node is not None, node or "未找到")
        if node:
            v = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
            check("内置 Node 可执行", "v22" in v, v)
    except Exception as e:
        check("内置 Node 解压", False, str(e))

    shutil.rmtree(tmp, ignore_errors=True)
    failed = [r for r in results if not r[1]]
    print(f"\n自检完成：{len(results)-len(failed)}/{len(results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(self_test())
    run_gui()
