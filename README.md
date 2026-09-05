# AI 漫剧生产工作流（脱敏发布包）

本仓库提供本地优先的漫剧前期生产辅助：剧本解析、资产索引、镜头规划、提示词检查、任务队列、字幕和成片归档。

## 安全边界

- 默认只做本地准备与离线模拟，不自动提交豆包任务、不消耗账号额度。
- 不保存密码、验证码、Cookie、令牌或真实账号信息。
- 真实桌面自动化、下载和计划任务必须在本机单独配置，并经过人工验收。
- 请复制 `configs/template.json` 为本地配置；不要提交真实路径、数据库、日志、截图或媒体。

## 快速开始

```powershell
python -m pip install -r requirements.txt
python -m src.cli prepare --config configs\simulation.json
python -m src.cli report --config configs\simulation.json
python -m unittest discover -s tests -p 'test_*.py'
```

`configs/simulation.json` 仅用于本地模拟，路径需要按你的环境调整。真实提交相关闸门默认为关闭；任何开启都应先完成单任务、账号、下载和画面比例验收。

## 目录

- `src/`：核心代码
- `tests/`：离线回归测试
- `configs/template.json`：脱敏配置模板
- `configs/simulation.json`：模拟运行示例
- `docs/architecture.md`：设计约束

发布版本：0.1.0（脱敏、仅本地准备基线）
