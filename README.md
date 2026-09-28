# 一键上色，拆件skill 1.0版本

面向 AI 助手的三维模型分色、拆件与打印装配工作流。**1.0** 是本项目的公开发布版本；包内工作流的内部版本为 **2.10.3**。

> 本项目提供方法指导、记录模板和 STL 基础回读脚本，**不包含通用自动上色、语义分区或接口生成引擎**。需要执行环境中的 Blender、模型操作工具和人工判断配合。

## 能做什么

- 按部位、颜色和装配需求规划物理拆件。
- 指导工作网格修复、部位归属、共享边界和配对接口设计。
- 记录版本、依赖、证据和未完成的检查，支持任务恢复。
- 在 Blender 后台回读 STL，检查文件完整性、哈希、连通性、基础拓扑和有向体积。

未另行指定时，流程默认 FDM、装配总高 200 mm；明确的用户要求和已确认参数优先。默认不切片、不生成 G-code。

## 使用

1. 下载本仓库或 Release 中的 ZIP，解压后保留整个目录结构。
2. 将目录交给支持 `SKILL.md` 的 AI 助手，按该助手的安装方式添加为 skill；入口为 [SKILL.md](SKILL.md)。
3. 提供有权使用的三维模型，并说明配色、拆件目标及已知打印参数。
4. 根据阶段确认部位归属、接口与检查证据，再导出独立零件。

示例请求：

```text
使用 blender-color-print-split 处理这个模型：按配色拆为独立打印件，
保留原始模型，先检查并展示分区与共享边界，再生成装配接口。
请记录未完成的检查，默认不切片。
```

## STL 回读工具

需要提供 `wm.stl_import` 操作符及所需参数的 Blender 版本。实际兼容性请在本机运行测试确认。脚本只使用 Python 标准库与 Blender 自带的 `bpy` / `bmesh`。

```bash
blender -b --factory-startup --python-exit-code 1 \
  --python scripts/check_stl_exports.py -- \
  /path/to/stl_exports /path/to/report.json \
  --manifest /path/to/manifest.json
```

`--manifest` 可选；未提供时不验证导出清单完整性。模板见 [manifest.example.json](assets/manifest.example.json)。STL 坐标按毫米解释，不自动推断单位。报告必须使用独立的 JSON 路径。

脚本在一次性后台进程中逐文件清空场景。基础检查通过不代表自交、壁厚、语义分区、装配路径或实际打印已经通过。

## 测试

```bash
python3 tests/test_stl_exports.py --blender /path/to/blender
```

现有 14 项集成测试使用临时合成 STL，覆盖正常实体、反向壳、顶点相接、多壳、缺件/多件、坏文件、哈希和输出保护。必须使用真实 Blender，不使用模拟拓扑结果。

## 目录

- `SKILL.md`：工作流入口。
- `references/`：分区、重建、接口、验收和能力范围说明。
- `assets/manifest.example.json`：记录模板。
- `scripts/check_stl_exports.py`：基础 STL 回读工具。
- `tests/test_stl_exports.py`：真实 Blender 集成测试。

## 已知限制

项目内的历史样本说明不等于跨模型效果保证。完整能力边界、失败样本及历史回归记录见 [validation-scope.md](references/validation-scope.md)。本次开源整理未重新执行 Blender 几何集成测试；发布环境未找到 Blender 可执行文件。

本仓库不包含示例角色模型、商业资产或第三方工具源码。引用工具与用户输入模型的许可由各自权利人规定。

## 后续更新

后续版本更新和使用内容，可在小红书关注 **张大山l**（小红书号：**943215445**）。扫描下方原始名片中的二维码也可以找到账号。

<img src="assets/xiaohongshu-qr.jpg" alt="张大山l 的小红书名片和二维码" width="360">

## 参与贡献

欢迎提交 Issue 或 Pull Request。请注明输入类型、Blender 版本、复现步骤、预期结果与实际证据；分享模型前确认有分发权限。新增能力须区分已验证结果与未验证假设。

## 许可证

见 [LICENSE](LICENSE)。
