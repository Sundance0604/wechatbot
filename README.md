# WeChat local database cleanup & Violet bot

本仓库包含两个 Windows 微信研究项目。二者共用“读取本机已登录账号的加密聊天数据”这一基础，但写入路径完全不同：

| 项目 | 做什么 | 实现路径 | 当前边界 |
| --- | --- | --- | --- |
| [wechat_message_cleanup](wechat_message_cleanup/) | 关键词扫描、离线副本删除、受限写回实验 | 数据库解密 → SQL 删除 → FTS 索引维护 → 重加密 → 退出微信后替换指定文件 | 写回仍保留原实验的严格样本限制，不是通用清理器 |
| [violet_wechat](violet_wechat/) | 多群 AI 聊天、管理员微信命令、Codex 工作区任务 | 只读聊天库 + 微信进程内原生文本发送 | 原生发送仅适配指定的 Windows 微信 4.2.0.11 模块指纹 |

## 两份技术文档

文档按源码解释机制，包含文件导航、数据布局、状态机、失败路径、实验边界与复现步骤。没有真实聊天截图或私人实验日志。

1. **[微信消息删除：数据库结构、页加密、FTS 与离线写回](docs/01-offline-cleanup-internals.md)**
2. **[Violet 消息 bot：只读监听、原生 Hook 发送与 Agent 权限](docs/02-violet-bot-internals.md)**

推荐先读每份文档的“能力边界”，再沿源码阅读。微信升级后不能只改版本号继续使用旧函数地址。

## Violet 快速启动

Windows、Python 3.11+。安装并登录支持版本的微信后：

```powershell
cd violet_wechat
copy config.example.json config.local.json
.\setup.bat
.\start.bat
```

打开 <http://127.0.0.1:8766>，填写自己的模型连接、监听群和管理员身份。开始监听是单独的操作。若原生连接尚未初始化，先在微信手动发一条普通文字，再在面板重新连接发送。没有配置任何真实账号、群或 API Key。

## 删除项目的入口

```powershell
python -m pip install -r wechat_message_cleanup/requirements.txt
python wechat_message_cleanup/test_delete_cy.py --help
python wechat_message_cleanup/test_offline_delete_cy.py --help
python wechat_message_cleanup/prepare_offline_writeback.py --help
python wechat_message_cleanup/offline_writeback.py --help
```

文件名中的 `test_` 是早期实验命名，前两个入口是显式运行的工具；它们不属于自动运行的测试目录。阅读删除项目 README 和完整文档后再使用。扫描可在微信登录时运行，写回要求微信完全退出且缓存中已有可验证密钥。

## 测试

从仓库根目录，在独立 Python 环境安装两个项目依赖及 `requirements-dev.txt`，运行：

```powershell
python -m pytest -q -p no:cacheprovider
python tools/privacy_audit.py
```

自动测试使用虚构联系人、模拟模型/发送器以及临时数据库。不会给真实微信发消息，也不会启动实际数据库清理。Windows 文件锁测试会启动隔离 Python 子进程。GitHub Actions 使用 Windows 运行这些检查。

## 隐私与发布范围

此版本来自独立整理的源码副本，没有上传原工作区 Git 历史，也没有复制个人配置、账号映射、群记忆、主人资料、聊天记录、密钥缓存、数据库备份、截图、进程转储或运行日志。示例联系人均为虚构占位符。

删除项目只保留数据库扫描/副本实验/写回核心；早期临时 Hook 探针和已被 Violet 替代的旧聊天机器人没有发布。Violet 中已保留实际使用的原生发送适配器及其版本指纹。这样可以直接阅读现行实现，不依赖私人实验目录或原父项目。

运行后产生的数据仍可能含有私密内容。`.gitignore` 与隐私扫描提供误提交防护，不会加密文件，也不能替代人工审核。详细说明见 [PRIVACY.md](PRIVACY.md)。仓库保留既有 [GPLv3 许可证](LICENSE)，第三方依赖及文档来源见 [THIRD_PARTY.md](THIRD_PARTY.md)。
