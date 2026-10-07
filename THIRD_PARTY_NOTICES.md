# 第三方组件与许可声明（Third-Party Notices）

本仓库 `qwenpaw-mem0` 的全部**自有代码以 MIT License 发布**（见 [LICENSE](LICENSE)）。
它是为 QwenPaw 开发的**独立第三方插件**：v2 通过 QwenPaw / AgentScope **官方开放的记忆后端扩展接口**（`BaseMemoryManager`、`register_memory_backend`、启动 / 关闭钩子）实现，并以 HTTPS 调用火山引擎 Mem0 的 REST API，**未复制、修改或再分发任何上游项目的源代码**，按 Apache-2.0 对「仅按名绑定 / 链接公共接口的独立作品」的界定，不属于上游作品的衍生拷贝。

## 运行时直接依赖

| 组件 | 版本 | 在本项目中的角色 | 许可证 | 权利人 / 来源 |
| --- | --- | --- | --- | --- |
| HTTPX | ≥ 0.27 | 唯一的第三方运行时依赖，用于直连 Mem0 REST（同步 Client + 限流 / 重试） | BSD-3-Clause | Encode OSS Ltd.（[encode/httpx](https://github.com/encode/httpx)） |

> v2 运行时**不再依赖** mem0ai、FastAPI、Uvicorn、Pydantic、python-dotenv；`.env` 解析与数据建模均使用 Python 标准库自实现。

## 扩展对象（不打包、不分发源码）

| 组件 | 版本 | 在本项目中的角色 | 许可证 | 权利人 / 来源 |
| --- | --- | --- | --- | --- |
| QwenPaw | 2.x | 插件运行的桌面 Agent 宿主，提供记忆后端 / 插件 / 钩子扩展点 | Apache-2.0 | AgentScope Team, Alibaba（[agentscope-ai/qwenpaw](https://github.com/agentscope-ai/qwenpaw)） |
| AgentScope | 2.0.x | Agent / 记忆 / 工具底层框架，提供 `BaseMemoryManager`、`ToolChunk` 等公共抽象 | Apache-2.0 | Tongyi Lab, Alibaba（[agentscope-ai/agentscope](https://github.com/agentscope-ai/AgentScope)） |

## 接口契约参考（不含其源码）

- `mem0_service/http_client.py` 的 REST 请求路径、请求头、请求体与响应字段，依据**火山引擎 Mem0 官方 API 文档**以及 mem0ai（[mem0ai/mem0](https://github.com/mem0ai/mem0)，Apache-2.0）公开实现所体现的接口约定独立编写，目的是与兼容 mem0 协议的托管后端互通；本仓库**不包含、不打包 mem0ai 的任何源代码或二进制文件**。
- 火山引擎 Mem0 是字节跳动 / 火山引擎提供的**商业云服务**，本项目仅为其调用客户端，通过使用者本人配置的 HTTPS 接入点与 API Key 访问，不内含任何火山引擎专有 SDK 或服务端代码。

> 各依赖的准确许可证文本以其官方仓库 / 分发包中的 LICENSE 文件为准。BSD-3-Clause 与 Apache-2.0 均为宽松许可，与本项目采用的 MIT 相互兼容，允许商用、修改与再分发。

## Apache-2.0 合规说明

- 本项目**未修改** QwenPaw / AgentScope 的任何源文件，因此不涉及「在被修改文件上标注变更」的义务；这些组件由使用者通过官方渠道自行获取，本仓库不随附其源码副本。
- 若你在本项目基础上再分发并直接打包了上述 Apache-2.0 组件的源码或其 NOTICE 内容，需按 Apache-2.0 §4 保留对应版权、专利、商标与归属声明。

## 商标与非背书声明

- “QwenPaw”“Qwen / 通义”“AgentScope”是阿里巴巴集团 / 通义实验室相关主体的商标或项目名称；
- “火山引擎 / Volcengine”“豆包”是字节跳动有限公司相关主体的商标；
- “Mem0 / mem0ai”是 Mem0 AI, Inc. 的名称或商标。

本项目是**个人开发的非官方、独立项目**，与上述任何主体均不存在投资、隶属、雇佣、赞助、认证或官方背书关系。依据 Apache-2.0 第 6 条（许可证不授予商标权，仅允许为说明作品来源而作合理、惯常的描述性使用）及通行的指示性合理使用原则，文中出现上述名称仅用于**说明兼容对象、技术栈与服务来源**，不主张任何商标权利，也不暗示官方关系。项目未使用任何上述主体的 Logo 或官方视觉标识。

## 云服务与费用

本项目通过 HTTPS 调用**火山引擎托管 Mem0 服务**（或任意兼容 mem0 协议的后端）。使用者需：

1. 自行注册火山引擎账号、开通 Mem0 服务并获取本人接入点与 API Key，填入本地 `mem0_service/.env`（该文件已被 `.gitignore` 排除，仓库不含任何可用凭据或专属实例地址）；
2. 自行遵守《火山引擎服务条款》等适用协议，并自行承担服务调用产生的费用、配额与账号合规责任（火山引擎 Mem0 自 2026-11-02 起计费）；
3. 不得在本项目中内置、分享他人 Key，或绕过服务计费与访问控制。

## 免责声明

本项目按“现状（AS IS）”提供，不附带任何明示或默示担保。作者不对依赖其运行所产生的记忆数据正确性、费用估算偏差、服务可用性或任何直接 / 间接损失负责；生产环境使用前请自行评估与测试，并以火山引擎控制台的真实账单为准。
