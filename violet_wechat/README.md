# Violet · 微信 AI 工作台

Violet 在 Windows 本机独立运行，提供微信群聊/私聊 AI 对话、管理员微信命令，以及 Codex 或 DeepSeek Harness 工作区 Agent。接收使用只读聊天库，发送使用已验证微信模块的原生函数。

完整技术文档：[只读监听、原生 Hook 发送与 Agent 权限](../docs/02-violet-bot-internals.md)。

## Windows EXE（0.8.1）

在 [GitHub Releases](https://github.com/Sundance0604/wechatbot/releases/tag/violet-v0.8.1) 下载 Windows x64 压缩包，解压后双击 `Violet.exe`。无需安装 Python、Conda 或 Node.js。更新前先用“退出程序.cmd”退出旧版，再打开新版。

新用户的机器人称呼为 `VioletBot`，默认人设、主人资料、微信账号、监听会话、管理员及工作区列表均为空。使用者需要登录自己的微信、填写自己的 DeepSeek API Key，再选择监听会话。连接本机微信后显示的账号来自使用者自己的登录会话。

0.8.1 移除了开发者称呼及聊天人设等默认值；公开源码、配置样例与 EXE 均接受隐私扫描。既有本机设置继续保留，不随更新删除，也不会随 EXE 发送给其他人。发布时只分享 EXE/发布压缩包，不要分享本机数据目录。

默认数据目录为 `%LOCALAPPDATA%/Violet`。关闭浏览器不会退出服务：面板“停止全部”停止监听与 Agent；发布包的“退出程序.cmd”关闭后台。重复启动同一数据目录会打开已运行的面板，所以更新时应先退出旧版。

## 源码启动

Windows，Python 3.11+，已登录兼容版本的电脑版微信：

```powershell
cd violet_wechat
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python run.py --open
```

也可以双击 `setup.bat` 安装，再用 `start.bat` 启动。主配置为 `config.local.json`，空白模板见 `config.example.json`。源码目录已有该配置时保留原位置；否则使用默认数据目录。

```powershell
python run.py --data-dir D:/VioletData --port 8767 --open
python run.py --data-dir D:/VioletData --stop
python run.py --data-dir D:/VioletData --quit
```

## 配置与使用

1. 在“设置”填写自己的 API Key；保存后不会把已存密钥回传到浏览器。
2. 连接微信，选择监听的群聊或联系人，按需填写机器人称呼及人设。
3. 管理员按真实微信号或 wxid 核验，不按昵称升权。次级管理员仅可使用 `/command` 微信操作。
4. 点击“开始监听”才会处理新增消息；程序不会自动开启全部私聊，也不回放加入监听前的历史。

普通聊天支持写作、解释、整理、翻译和联网查询；默认对话模型 `deepseek-flash`、联网模型 `deepseek-v4-pro`。各会话的上下文、已确认记忆及任务来源独立。“AI 对话”页不会向微信发信。

管理员可使用 `/command send ["示例联系人"] | 正文`、`/command forward 3 -> 示例联系人` 等命令，查看进度或取消自己的命令批次。权限与目标在执行前核验；同名目标不擅自选择。

主人资料默认留空，由使用者自行维护。资料与会话记忆不能授予管理员权限或证明任务已经完成。

## 项目 Agent

普通聊天不需要安装 Agent。使用项目任务时另外安装对应程序：

- `/agent 工作区标识 任务内容`：使用本机安装并登录的 Codex CLI/App Server。
- `/deepseek 工作区标识 任务内容`：使用原生 DeepSeek Harness Agent，复用本机配置的 DeepSeek API Key。

在“工作区 → 全局 Agent 设置”填写程序路径、模型并启用。全局设置供全部工作区共用，新增项目无需重新填写程序路径。保存采用配置版本校验，旧页面无法覆盖较新的启用状态或程序路径；局部 Agent 设置只合并提交的字段。

自行添加已存在的项目目录，设置读写、命令及联网权限，再绑定项目群。项目经理权限默认对该工作区绑定群的所有成员开放，也可改为指定成员。该权限只允许在绑定工作区内发起、查询和取消本人的项目任务，不授予全局管理员权限；新项目不会自动绑定任何真实群。

DeepSeek 使用 Harness 原生沙箱。终端进程可能读取本机可见文件或访问网络，需要更强隔离时应使用独立系统账号或虚拟机。工作区 Markdown 不能扩大程序核验的权限。

## 微信版本

发送只接受已适配的 Windows 微信 `4.2.0.11`、`4.2.0.13`、`4.2.0.52`、`4.2.0.53` 指定 SHA-256 模块。不能仅凭版本号或复制另一进程的内存地址保证兼容；未知模块会停止调用发送函数。

4.2.0.53 的只读连接、发送管理器和工作线程定位、三个文字对象构造及完整释放已在本机验证。本次发布没有自动发送真实微信消息，也未在另一台实体电脑验收。

微信重启后点击“重新连接发送”。若工作线程尚未出现，可以在微信手动发一条文字后重试。微信更新后需要重新验证模块指纹和原生适配。

## 数据与停止

配置、API Key、微信标识、聊天、主人资料、会话记忆、任务日志与本机原生运行配置均不在发布包中。使用后生成的数据属于该电脑使用者，应按敏感资料管理。

普通聊天会把所选会话的近期文字、已确认记忆与主人资料发送给配置的模型服务。Agent 任务会携带其来源会话的快照，读写与工具执行取决于工作区授权。详见 [PRIVACY.md](../PRIVACY.md)。

停止会取消尚未发送的回复和 Agent 任务；已经进入微信发送函数的单条消息无法撤回。不确定是否发送成功时不会自动重发，应查看发送记录再核对。服务重新启动默认保持停止。

## 检查与构建

```powershell
python -m pip install pytest
New-Item -ItemType Directory -Force .build | Out-Null
python -m pytest tests -q -p no:cacheprovider --basetemp .build/test-run
node --test tests/test_harness_policy.mjs
python -m pip install -r packaging/build-requirements.txt
./packaging/build.ps1 -Python python
python packaging/audit.py dist/Violet.exe
python packaging/smoke.py dist/Violet.exe --output .build/release-smoke --port 8878
python packaging/release.py dist/Violet.exe --smoke-report .build/release-smoke/smoke.json
```

`packaging/audit.py --deny-file` 可额外检查本机禁发词，禁发词文件放在仓库外且不可发布。审计扫描 EXE 的资源与 Python 字符串常量，仅输出问题类别和文件路径。

`Violet.exe --self-test` 只检查离线组件，不调用模型、不发信。`--no-open` 禁止自动打开浏览器。程序未作商业代码签名，Windows 可能显示未知发布者提示。

本项目遵循仓库的 GPL-3.0 许可证；第三方许可证随程序提供。
