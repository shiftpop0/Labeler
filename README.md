# Labeler

可复用的多人图片框标注 Web。适合为物体检测模型制作带类别的矩形框标签。
Python 3.11+，服务端只依赖标准库；前端使用原生 HTML / CSS / JavaScript，无 CDN、无 npm 构建。

从已有黄金条块标注平台提取，已将类别、项目名称、账号和图片入口配置化。
不包含原项目图片、人工标注、数据库、密码、API 密钥、模型或公网隧道配置。

## 功能

- 1–100 个可配置对象类别，框有 `class_id` 和 `class_name`，显示名称支持中文。
- 多人登录、管理员全局查看、普通用户只看分配任务，按初始来源状态稳定分配。
- 待标注、已标注、正样本、负样本、不确定、逻辑删除六个同步视图。
- 原图与标注图并排显示；新增、移动、缩放、删除框，切换类别，保存并下一张。
- `Q` 切换新增框模式，`W` 循环切换对象类别，`E` 保存并下一张。
- 新框拖拽完成后再显示类别文字；保存失败保留错误提示；并发修订冲突返回 409。
- SQLite WAL / FULL 事务、完整修订历史和追加式 JSONL 日志。
- 管理员导出版本化 JSONL，命令行转换 YOLO 标签，在线一致性备份。

目前支持**矩形目标检测框**，不包含分割、多边形、关键点、自动预标注或模型训练。

## 快速开始

```bash
git clone https://github.com/shiftpop0/Labeler.git
cd Labeler
python -m labeler.setup
```

这会生成 `project.json`、`wxz/accounts.json` 和空的 `wxz/images/`。
每个账号使用不同的随机密码，命令不会打印密码；请在本机查看 `wxz/accounts.json` 并私下分发。
再次运行不会覆盖已有密码。默认有 `admin` 和 `annotator1`–`annotator5` 六个示例账号。

**如需自定义账号，先复制 `examples/project.json` 为 `project.json` 并编辑，再运行 setup。**
已有工作区的类别定义、类别顺序、项目 ID、schema 版本和账号名单受到锁定保护。
修改这些内容后请使用新的工作区；标题和操作说明可以调整。

将图片放到 `wxz/images/`。图片清单有两种准备方式：

1. 自己创建 `wxz/manifest.jsonl`，格式见下节，不需要安装任何依赖。
2. 安装可选图片工具后扫描本地图片：

```bash
python -m pip install -e ".[images]"
python -m labeler.prepare
```

`prepare` 支持 JPEG、PNG、WebP、BMP，记录分辨率和 SHA-256，不下载、不改写图片，不覆盖已有清单。
遇到带旋转 EXIF 的图片会拒绝导入，请先在自己的数据流程中统一方向，再生成清单。

启动：

```bash
python -m labeler
```

浏览器打开 <http://127.0.0.1:8084>。默认只绑定本机，端口被占用时直接报错。
如果已有其他项目使用 8084：`python -m labeler --port 8085`。

## 换一个检测项目

`examples/project.json` 给出了完整配置。类别 ID 必须从 0 连续编号，名称必须唯一，
使用英文、数字、下划线、点或短横线；`label_zh` 是显示名称，也可以填写其他语言。

```json
{
  "project_id": "parts",
  "title": "零件检测标注",
  "schema_version": "parts-v1",
  "instructions": "分别框选可见螺栓和螺母，框贴合对象边界。",
  "classes": [
    {"id": 0, "name": "bolt", "label_zh": "螺栓", "color": "#1677ff"},
    {"id": 1, "name": "nut", "label_zh": "螺母", "color": "#fa8c16"}
  ],
  "annotators": ["worker1", "worker2"],
  "admin": "admin"
}
```

JSONL 每行一张图片，路径相对于 `--collection-root`：

```json
{"candidate_id":"sample-001","local_path":"batch1/image001.jpg","width":1280,"height":720,"source_status":"uncertain","provenance":{"source":"自有图片","event_group":"batch1"}}
```

- `candidate_id` 必须唯一。实际图片必须在图片目录内，支持子目录，禁止路径越界。
- 默认 `source_status=uncertain`，进入待标注；`accepted` 也进入待标注，初始分类为正样本。
- `rejected` 表示已初审确认负样本，不分配框标注任务；不要用它代表“尚未审核”。
- 可附加 `sha256`，导入时会核对文件内容。`provenance` 原样保留来源、权限和分组等信息。
- 清单只在空工作区初始化时导入。追加数据请用新工作区；当前没有在线追加/再分配功能。
- 一个进程服务一个项目。不同项目使用不同 `--root`、工作区和端口。

例如独立项目目录位于当前仓库内部：

```bash
python -m labeler.setup --root wxz/projects/parts
python -m labeler --root wxz/projects/parts --port 8085
```

工作区、数据、清单、账号和配置路径必须位于所选 `--root` 内。
还支持 `--config`、`--source`、`--collection-root`、`--workspace`、`--password-file`，运行 `--help` 查看。
保留了旧初审 SQLite 清单的只读导入适配器，普通新项目使用 JSONL 即可。

## 标注与导出

选择图片后默认进入选择/调整模式。按 Q 或“新增框”后拖动画框；选择类别按钮也可开始下一框。
按 W 按配置顺序循环切换对象类别，按 E 保存并打开下一张。没有活动框时，W 设置下一框类别
并进入新增模式；明确选中已有框时，W 修改该框类别。点击类别按钮遵循相同规则。Q/W/E 在
输入框、下拉框、可编辑区域、组合键和键盘长按重复期间不会触发。

新框拖拽期间只显示边框，松开鼠标完成后才显示类别文字；移动或缩放已有框时仍显示类别。
正样本必须至少有一个有效类别框；负样本必须无框。
“已标注”视图表示有框正样本，完成的无目标负样本在“负样本”视图中。
删除是可恢复的逻辑删除，保留框和修订历史。

管理员点击“导出快照”，文件保存到运行机器的 `wxz/workspace/exports/`，页面显示路径和 SHA-256。
这是服务器本地导出，不会自动下载到远程标注员的电脑。
默认只导出人工完成的正/负样本；待标注、不确定和逻辑删除记录不进入训练导出。
JSONL 包含原图信息、像素坐标、类别表、分配账号和修订号。

```bash
python -m labeler.yolo wxz/workspace/exports/实际导出文件.jsonl wxz/yolo/export-001
```

输出 `labels/*.txt`、`classes.json` 和 `image-label-map.json`。
正样本使用 `class_id center_x center_y width height` 归一化坐标，负样本为空标签文件。
标签文件名使用 candidate ID 的 SHA-256，**需要按映射表配对/命名图片后再接入训练**。
转换器不复制图片、不划分 train/val/test、不生成训练配置；它不是完整训练数据集打包器。
请在自己的训练数据流程中按事件/来源划分数据，避免泄漏。

## 备份和恢复

```bash
python -m labeler.backup
```

在 `wxz/backups/<时间>/` 创建不可覆盖的 SQLite online backup 快照，从同一快照重建修订
JSONL 和分配清单，检查 SQLite 完整性、外键和修订总数，记录逐文件 SHA-256。
**备份包含账号哈希和登录会话，应按私有数据保管；图片、project.json 和 accounts.json 需另行保管。**

恢复步骤：

1. 停止对应标注服务，核对备份清单中的文件哈希。
2. 将旧工作区整体移动到 `wxz/del/<时间>/`，保留原路径、时间和原因记录。不要直接删除或覆盖。
3. 在新工作区放入备份数据库、修订日志、分配清单；保留原有配置、账号和图片路径。
4. 检查 `PRAGMA integrity_check` 与备份记录的修订数一致，再启动并抽查标注。

不要把活动数据库的 `.sqlite` 单独直接复制作为一致性备份，也不要让旧 `-wal/-shm` 混入恢复目录。
当前数据库中的图片路径是绝对路径，跨机器移动需保持原路径或单独做受控迁移。
同盘备份不能应对整盘故障；工具没有自动计划任务或远端同步。

## 访问边界

默认 `127.0.0.1`。有账号密码、HttpOnly/SameSite 会话 cookie、CSRF 校验、登录失败限流和按账号授权。
内置 HTTP 服务面向本机/受控网络的小团队，不作为公开互联网生产服务器。
需要远程使用时，自行配置受控网络、HTTPS 反向代理和访问控制；仓库不会自动启动公网隧道。
会话最长 7 天。更换账号密码文件并重启会撤销该账号旧会话。

## 测试

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

测试使用合成临时数据，覆盖任务分配、账号隔离、类别校验、正负样本保存、修订冲突、
重启会话、可变类别数量、配置锁定、导入路径检查、备份和 YOLO 转换。

来源与适配记录见 [docs/PROVENANCE.md](docs/PROVENANCE.md)。
本次发布未替权利人选择开源许可证；公开仓库不等于已授予通用再分发许可。
