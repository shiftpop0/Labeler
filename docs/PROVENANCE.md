# 来源与适配记录

提取日期：2026-09-16。

直接来源为用户的独立 `gold_bullion_detection` 项目当前多人标注实现：

- `src/gold_bullion_detection/multiuser_annotation_web.py`
- `apps/annotation_review/index.html`
- `apps/annotation_review/app.js`
- `tests/test_multiuser_annotation_web.py`

提取时的 SHA-256 见 [source-hashes.json](source-hashes.json)。
来源项目文档记录，其通用交互和持久化方式于 2026-09-04 参考并适配了
`cash_bundle_detection` 中的标注工具。本次仅从黄金项目的当前实现复制，未读取、修改或
运行现金项目。Labeler 运行时不会导入任何其他项目。

本次适配包括：

- 去除固定黄金类别、业务提示、数据总数、原账号名、原项目路径和公网域名。
- JSON 配置对象类别、项目标题、操作说明、账号，服务端和前端使用同一类别表。
- 新增独立 JSONL 清单导入、可选本地图片扫描、随机独立密码初始化。
- 增加工作区契约检查，拒绝在已有标注库上直接更换类别和账号结构。
- 保留六类视图、框编辑、逻辑删除与恢复、分配权限、版本冲突和修订审计。
- 删除提取副本中的不可执行旧单用户脚本，原项目文件保持不变。
- 默认绑定本机；端口冲突直接失败，避免误认服务地址。
- 增加非有限坐标检查、在线备份和 YOLO 标签转换。

源项目当前业务数据及备份均留在源项目私有 `wxz/` 下，未随代码发布。
本仓库未包含许可证授权文件，后续应由权利人决定是否采用 MIT、Apache-2.0 或其他许可。
