# 清简 · 微信记录清理（0.2 实验版）

独立的 Windows 本机清理应用。使用独立 React 面板与基础组件，后端与招聘、邮件投递、Hook 机器人完全分开。微信数据库读取器单独加载 `wechatauto-replica 1.2.1` 的数据库模块，不加载其 GUI 或发送入口。

## 普通用户使用

双击发行版 `WeChatCleaner.exe`，程序会自动打开浏览器中的本地面板。不需要安装 Python、Node 或配置模型。

1. 登录微信，在清简中连接本地账号，并核对昵称。
2. 勾选联系人或群聊，输入删除词，选择“包含这个词”或“整条正文相同”，可限定日期。
3. 查询并核对命中消息，取消勾选需要保留的记录。
4. 在微信菜单中正常退出微信。仅关闭窗口通常仍在后台运行。
5. 点击“准备清理”，先创建备份、重新核验目标、处理副本并校验加密文件。
6. 核对最终数量与会话，勾选确认后执行删除。完成后打开微信核对显示和搜索。

“备份与恢复”页可以继续尚未执行的计划、恢复清理或恢复中断操作。微信重新写入数据后，程序会拒绝直接用旧备份覆盖新数据。关闭浏览器后本地服务仍在运行；使用左下角“退出程序”关闭服务，再次双击程序可重新打开已有服务。

## 联系人与全选

联系人和群聊分两个标签显示，可以混合选择。每类都有全选、清空本类；搜索会话名称后还可单独选择当前搜索结果。切换分类保留另一类选择。同名会话通过微信标识区分。

消息预览的全选只勾选当前预览中可清理的记录，暂不支持的记录保持禁用。最多展示 1000 条；超过时请缩小日期范围，不会把未展示的消息加入删除计划。私聊正文保留原始内容，不应用群聊发送者前缀剥离规则。

## 当前支持范围

- Windows x64；写回限定微信文件版本 4.2.0.11 / 4.2.0.13，并继续校验实际结构和加密页格式。首次连接需在微信运行时确认版本；仅有旧密钥缓存时可以查询，但不能据此执行写回。
- 当前账号内选中联系人私聊和群聊的普通历史纯文本。暂不支持最后一条消息、涉及未读状态、媒体引用、缺失或无法唯一识别的全文搜索文档。
- 写回支持已验证的 32 字节数据库密钥、4096 字节页和 80 字节保留区。其他格式停止处理。
- 离线写回要求数据已 checkpoint：若备份中仍有当前 WAL 世代的有效帧或待恢复的回滚日志，停止准备并保留备份；不会用实验读取器的日志合并结果直接写回。
- 查询可在微信运行时进行，实际写入和恢复必须退出微信。准备完成后数据变化会使计划失效。
- 这是本地逻辑删除，不是撤回；不会清除其他参与者、其他设备或已有备份中的记录。

既往实验已验证两条历史文本的客户端删除与离线副本的主表、搜索词元及辅助索引结果一致。本版另有合成加密库测试和浏览器流程测试。尚未完成多台电脑、多种微信数据集的发布验收，也没有在本轮开发中对真实账号执行删除。

## 数据与恢复

默认数据目录为 `%LOCALAPPDATA%\WeChatCleaner`，可用 `--data-dir` 指定。每个清理计划的 `runs/<编号>/backup` 保存账号完整原始数据库目录（包括 WAL、SHM、KVDB 和其他伴随文件），并逐文件记录 SHA-256。备份不会自动删除。

密钥只保存在进程内存中；不新建明文密钥缓存，不写入日志。连接可读取原读取库已有的本机密钥缓存。解密查询缓存退出程序后清理；准备阶段的明文工作副本在准备结束后清理，中断残留在下次启动时清理。原始备份和待安装文件保持加密状态，计划元数据包含账号、群名和删除词。

多文件替换不是一次跨文件原子提交。程序在写入前持久化操作状态，在普通失败时回滚；突然中断后，启动时提示恢复。只有原文件仍属于已记录的修改前后版本时才允许恢复；额外变化会使恢复停止。

## 开发与构建

```powershell
# 在已安装本项目依赖的 Python 环境中
python -m pip install -e ./wechat_cleaner
npm --prefix wechat_cleaner/frontend ci
npm --prefix wechat_cleaner/frontend run build:cleaner
python wechat_cleaner/run.py

# 虚构数据演示：不访问真实微信
python wechat_cleaner/run.py --demo

# 后端测试均使用合成加密数据库
python -m pytest wechat_cleaner/tests -q

# 浏览器流程测试：先单独启动演示服务
python wechat_cleaner/run.py --demo --no-browser --port 8878 --data-dir wechat_cleaner/.build/ui-runtime
npm --prefix wechat_cleaner/frontend exec -- playwright install chromium
npm --prefix wechat_cleaner/frontend run qa:cleaner

# 安装构建工具后，在 PowerShell 中打包
python -m pip install pyinstaller
./wechat_cleaner/packaging/build.ps1 -Python python
./wechat_cleaner/packaging/build.ps1 -Python python -OneFile
```

目录版位于 `wechat_cleaner/dist/WeChatCleaner/`，必须保留整个目录。单文件版位于 `wechat_cleaner/dist/WeChatCleaner.exe`。后者启动时会自动解压运行组件，业务备份仍保存在持久化的数据目录中。

`WeChatCleaner.exe --self-test --data-dir <诊断目录>` 使用虚构数据验证 SQLite/FTS、AES/HMAC、压缩和独立读取器加载，结果写入该目录的 `selftest.json`，不读取微信。

第三方数据库读取模块保持原样分发，随包附带其 Apache-2.0 许可证。完整依赖声明见 `pyproject.toml`；UI 独立入口是 `wechat_cleaner/frontend/cleaner.html`。
