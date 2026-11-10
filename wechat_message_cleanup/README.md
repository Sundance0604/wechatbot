# 微信消息清理板块：独立应用与底层研究

面向操作的新版入口：**[清简 0.2.1：联系人、群聊和聊天记录全选](../wechat_cleaner/README.md)**。

下载 **[Windows 实验版](https://github.com/Sundance0604/wechatbot/releases/tag/cleaner-v0.2.1-alpha.1)**，双击 exe 后按面板提示查询、预览、退出微信、备份并确认删除。独立应用不依赖邮件或 Hook 机器人。

以下内容保留原始受限研究脚本的说明；其中固定关键词、两条消息和固定分片的限制仅属于旧脚本，不是新版面板的输入限制。新版仍限制消息类型、状态、版本和加密格式，并在不能核验时停止。

## 原始受限离线实验

完整说明：**[数据库、加密、FTS 与写回技术文档](../docs/01-offline-cleanup-internals.md)**。

本项目针对本机当前账号的本地历史。它不撤回别人设备上的消息，不修改微信服务器历史，也没有实现 Hook 删除接口。工作方式是修改独立解密副本中的记录，再生成可验证的加密文件；只有显式运行安装步骤时才替换选定原库。不是删除整个消息数据库。

## 文件

| 文件 | 职责 |
| --- | --- |
| `test_delete_cy.py` | 读取所有消息分片，解析正文并生成关键词候选；不删除原件 |
| `test_offline_delete_cy.py` | 独立副本删除、保留行核对、重新加密与认证回读；不安装 |
| `prepare_offline_writeback.py` | 微信退出后的完整目录备份、哈希验证及解密准备 |
| `offline_writeback.py` | `build` / `install` / `restore` 的受限写回实验 |
| `message_parser.py` / `contacts.py` | 随项目提供的正文解析与显示名映射，不依赖原父项目 |
| `tests/` | 虚构页、临时文件、失败回滚与路径边界测试 |

## 环境与顺序

Windows、Python 3.11+，依赖固定兼容 `wechatauto-replica==1.2.1` 的内部接口。先安装依赖：

```powershell
python -m pip install -r requirements.txt
```

账号参数是 `xwechat_files` 下的**本地文件夹名**，不是联系人昵称。示例中的 `example_account` 与 `D:/WeChatData/xwechat_files` 必须换成你自己的本机路径；仓库没有任何预设账号。

1. 登录自己的微信，用只读扫描预热和验证上游密钥缓存。
2. 查看候选。先做独立副本实验，确认解析范围和匹配规则。
3. 完全退出微信后，才准备备份及受限写回。
4. 查看 `manifest.json`、`plan.json` 的目标、哈希和数量后，单独执行安装。

```powershell
# 只读扫描，返回码 2 表示扫描结束但没有公开删除 API，不表示已删除
python test_delete_cy.py --account example_account --db-dir D:/WeChatData/xwechat_files --keyword cy

# 仅改副本；此命令没有安装阶段
python test_offline_delete_cy.py --account example_account --db-dir D:/WeChatData/xwechat_files --keyword cy

# 以下步骤均要求微信已退出；依赖先前已验证的本机密钥缓存
python prepare_offline_writeback.py --account example_account --db-dir D:/WeChatData/xwechat_files

# RUN 为上一步输出的 writeback_runs/<时间戳> 目录
python offline_writeback.py build RUN --account example_account --db-dir D:/WeChatData/xwechat_files
python offline_writeback.py install RUN --account example_account --db-dir D:/WeChatData/xwechat_files
python offline_writeback.py restore RUN --account example_account --db-dir D:/WeChatData/xwechat_files
```

## 写回边界必须先读

公开版保留原研究的限制：写回阶段关键词固定为区分大小写的 `cy`，要求恰好两条目标，最终变更文件集合必须恰好为 `message/message_0.db` 与 `message/message_fts.db`。目标若被会话摘要、未读列表或媒体资源引用，构建拒绝继续。其他情况不是“删除失败后可以强行安装”，而是本实验尚未适配。

`--keyword` 只作用于扫描与副本实验，不能据此推断 `offline_writeback.py` 支持任意关键词。两条消息的限制不是微信数据库的普遍性质，而是故意保留的实验保护条件。

工具检查哈希、SQLite 结构、保留记录和 FTS 词元，但**不把这些检查等同于微信客户端加载与搜索显示已经通过**。本次公开发布只进行了虚构数据回归，没有操作任何真实微信原件。退出要求、恢复限制、多文件非原子性和断电情形见完整文档。

所有输出可能包含明文聊天、账号与密钥相关路径，均只应留在本机。请勿将 `offline_runs`、`writeback_runs` 或读取库缓存加入 Git。
