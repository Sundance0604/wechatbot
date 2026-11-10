# WeChat local database cleanup & Violet bot

本仓库包含独立的 Windows 微信本地清理应用、离线清理研究脚本与 Violet bot。清理应用不接入邮件或 Hook 机器人；各项目共用本机数据库读取基础，保持独立运行。

| 项目 | 做什么 | 实现路径 | 当前边界 |
| --- | --- | --- | --- |
| [清简 · wechat_cleaner](wechat_cleaner/) | 联系人/群聊关键词查询、全选、离线清理恢复、任务诊断导出 | 独立 React 面板 + 本机 Python 服务 + 加密数据库写回 | 0.3.0 实验版；历史纯文本，存在版本及状态限制 |
| [wechat_message_cleanup](wechat_message_cleanup/) | 关键词扫描、离线副本删除、受限写回实验 | 数据库解密 → SQL 删除 → FTS 索引维护 → 重加密 → 退出微信后替换指定文件 | 写回仍保留原实验的严格样本限制，不是通用清理器 |
| [violet_wechat](violet_wechat/) | 群聊/私聊 AI 对话、管理员微信命令、Codex 工作区任务 | 只读聊天库 + 微信进程内原生文本发送 | 原生发送仅适配指定的 Windows 微信 4.2.0.11 / 4.2.0.13 模块指纹 |

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

打开 <http://127.0.0.1:8766>，填写自己的模型连接、监听会话和管理员身份。开始监听是单独的操作。若原生连接尚未初始化，先在微信手动发一条普通文字，再在面板重新连接发送。没有配置任何真实账号、群或 API Key。

## 清简：独立清理应用 0.3.0

**[下载 Windows 实验版 exe / 发行包](https://github.com/Sundance0604/wechatbot/releases/tag/cleaner-v0.3.0-alpha.1)** · **[使用与构建说明](wechat_cleaner/README.md)**

新增「任务与诊断」：记录执行阶段、报错位置与全文索引结构，一键导出诊断 ZIP；真实路径及任务上下文会包含在内，方便汇总不同电脑的异常。

- 联系人私聊与群聊可混合选择；两类各有全选、清空和搜索结果选择。
- 输入删除词和日期范围，预览发送者、时间、正文，再逐条核对。聊天记录全选仅包含当前预览中可清理的消息。
- 正常退出微信后，先完整备份并重新核验，再单独确认写入。保留失败回滚、重启恢复和新数据保护。
- 不含邮件投递、模型服务或 Hook 机器人；用户无需配置模型或安装 Python 即可运行发行版。

当前写回适配微信 4.2.0.11 / 4.2.0.13 的已验证结构及 32 字节数据库密钥格式。最后一条、未读状态、媒体引用、活动日志或无法唯一核验的索引会停止处理。尚未完成多台电脑及真实写回后的客户端发布验收。

从源码启动：

```powershell
python -m pip install -e ./wechat_cleaner
npm --prefix wechat_cleaner/frontend ci
npm --prefix wechat_cleaner/frontend run build:cleaner
python wechat_cleaner/run.py
```

## 原始离线研究脚本


```powershell
python -m pip install -r wechat_message_cleanup/requirements.txt
python wechat_message_cleanup/test_delete_cy.py --help
python wechat_message_cleanup/test_offline_delete_cy.py --help
python wechat_message_cleanup/prepare_offline_writeback.py --help
python wechat_message_cleanup/offline_writeback.py --help
```

文件名中的 `test_` 是早期实验命名，前两个入口是显式运行的工具；它们不属于自动运行的测试目录。阅读删除项目 README 和完整文档后再使用。扫描可在微信登录时运行，写回要求微信完全退出且缓存中已有可验证密钥。

## 测试

从仓库根目录，在独立 Python 环境安装各项目依赖（包括 `pip install -e "./wechat_cleaner[test]"`）及 `requirements-dev.txt`，运行：

```powershell
python -m pytest -q -p no:cacheprovider
python tools/privacy_audit.py
```

自动测试使用虚构联系人、模拟模型/发送器以及临时数据库。不会给真实微信发消息，也不会启动实际数据库清理。Windows 文件锁测试会启动隔离 Python 子进程。GitHub Actions 使用 Windows 运行这些检查。

## 隐私与发布范围

此版本来自独立整理的源码副本，没有上传原工作区 Git 历史，也没有复制个人配置、账号映射、群记忆、主人资料、聊天记录、密钥缓存、数据库备份、截图、进程转储或运行日志。示例联系人均为虚构占位符。

`wechat_message_cleanup` 保留数据库扫描/副本实验/写回核心，新的 `wechat_cleaner` 提供独立清理面板与通用候选计划；早期临时 Hook 探针和已被 Violet 替代的旧聊天机器人没有发布。Violet 中已保留实际使用的原生发送适配器及其版本指纹。这样可以直接阅读现行实现，不依赖私人实验目录或原父项目。

运行后产生的数据仍可能含有私密内容。`.gitignore` 与隐私扫描提供误提交防护，不会加密文件，也不能替代人工审核。详细说明见 [PRIVACY.md](PRIVACY.md)。仓库保留既有 [GPLv3 许可证](LICENSE)，第三方依赖及文档来源见 [THIRD_PARTY.md](THIRD_PARTY.md)。
