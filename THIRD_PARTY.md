# 依赖与技术来源

仓库保留原远端已有的 GPLv3 LICENSE。以下依赖通过安装获取，没有把其安装目录或微信二进制文件复制进仓库。第三方组件仍适用各自许可证。

| 组件 | 用途 | 主来源 |
| --- | --- | --- |
| wechatauto-replica 1.2.1 | 本地微信数据库发现、密钥校验、解密与 WAL 缓存 | [项目源码](https://github.com/fanyuantaier/wechatauto-replica) |
| Frida 17.25.2 | 当前进程内观察、原生函数调用与线程调度 | [官方文档](https://frida.re/docs/javascript-api/) |
| SQLite | 明文数据库、副本快照、事务与应用状态 | [数据库文件格式](https://www.sqlite.org/fileformat.html)、[WAL](https://www.sqlite.org/wal.html) |
| APSW | FTS5 自定义 tokenizer 接口与索引实验 | [FTS5 扩展文档](https://rogerbinns.github.io/apsw/textsearch.html) |
| cryptography | AES-CBC 页级加解密 | [原生对称加密 API](https://cryptography.io/en/latest/hazmat/primitives/symmetric-encryption/) |
| FastAPI / Starlette / Uvicorn | 本机控制面板与接口 | 各项目官方文档 |
| HTTPX / Pydantic / psutil / zstandard | 模型请求、输入校验、进程实例识别、压缩正文解析 | 各项目官方文档 |

SQLCipher 的公开设计用于解释页加密的背景，不是腾讯对微信数据库格式的官方保证。原生函数的名称、偏移与对象语义来自限定二进制样本的观察和代码验证，文档中使用的描述性名称不是微信公开 SDK API。

没有分发 `Weixin.dll`、SQLite 私有模块、用户数据库、密钥或进程转储。更新原生适配应重新核对完整模块指纹与行为，而不是从本文复制地址到另一版本。
