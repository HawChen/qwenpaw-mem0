# 第三方组件与许可声明（Third-Party Notices）

本仓库 `qwenpaw-mem0` 的全部**自有代码以 MIT License 发布**（见 [LICENSE](LICENSE)）。
它是为 QwenPaw 开发的**独立第三方插件**：通过 QwenPaw / AgentScope 的公开扩展接口（类级 Hook、Toolkit）与 mem0ai 的公开 SDK API 实现，**未复制、修改或再分发任何上游项目的源代码**，按 Apache-2.0 对“仅按名绑定 / 链接接口的独立作品”的界定，不属于上游作品的衍生拷贝。

## 直接依赖与扩展对象

| 组件 | 版本 | 在本项目中的角色 | 许可证 | 权利人 / 来源 |
| --- | --- | --- | --- | --- |
| QwenPaw | 宿主（非打包依赖） | 插件运行的桌面 Agent 宿主，提供插件 / Hook 扩展点 | Apache-2.0 | AgentScope Team, Alibaba（[agentscope-ai/qwenpaw](https://github.com/agentscope-ai/qwenpaw)） |
| AgentScope | 1.0.20 | Agent / Hook / Toolkit 底层框架 | Apache-2.0 | Tongyi Lab, Alibaba（[agentscope-ai/agentscope](https://github.com/agentscope-ai/AgentScope)） |
| mem0ai | 0.1.118 | 记忆客户端 SDK，对接兼容 mem0 协议的托管后端 | Apache-2.0 | Mem0 AI, Inc.（[mem0ai/mem0](https://github.com/mem0ai/mem0)） |
| FastAPI | 0.115.6 | 可选独立服务模式的 Web 框架 | MIT | Sebastián Ramírez 等 |
| Uvicorn | 0.34.0 | 可选 ASGI 服务器 | BSD-3-Clause | Encode OSS Ltd. |
| Pydantic | 2.10.4 | 工具入参运行时类型解析 | MIT | Pydantic Services Inc. |
| HTTPX | 0.28.1 | HTTP 客户端 | BSD-3-Clause | Encode OSS Ltd. |
| python-dotenv | 1.0.1 | `.env` 配置加载 | BSD-3-Clause | Saurabh Kumar |

> 各依赖的准确许可证文本以其官方仓库 / 分发包中的 LICENSE 文件为准；上表为开发时所采用版本的许可证归类。上述许可证（Apache-2.0 / MIT / BSD）均为宽松许可，与本项目采用的 MIT 相互兼容，允许商用、修改与再分发。

## Apache-2.0 合规说明

- 本项目**未修改** QwenPaw / AgentScope / mem0ai 的任何源文件，因此不涉及“在被修改文件上标注变更”的义务；这些依赖通过包管理器安装、由使用者自行获取，本仓库不随附其源码副本。
- 若你在本项目基础上再分发并直接打包了上述 Apache-2.0 组件的源码或其 NOTICE 内容，需按 Apache-2.0 §4 保留对应版权、专利、商标与归属声明。

## 商标与非背书声明

- “QwenPaw”“Qwen / 通义”“AgentScope”是阿里巴巴集团 / 通义实验室相关主体的商标或项目名称；
- “火山引擎 / Volcengine”“豆包”是字节跳动有限公司相关主体的商标；
- “Mem0 / mem0ai”是 Mem0 AI, Inc. 的名称或商标。

本项目是**个人开发的非官方、独立项目**，与上述任何主体均不存在投资、隶属、雇佣、赞助、认证或官方背书关系。依据 Apache-2.0 第 6 条（许可证不授予商标权，仅允许为说明作品来源而作的合理、惯常描述性使用）及通行的指示性合理使用原则，文中出现上述名称仅用于**说明兼容对象、技术栈与服务来源**，不主张任何商标权利，也不暗示官方关系。项目未使用任何上述主体的 Logo 或官方视觉标识。

## 云服务与费用

本项目通过 HTTPS 调用**火山引擎托管 Mem0 服务**（或任意兼容 mem0 协议的后端）。使用者需：

1. 自行注册火山引擎账号、开通 Mem0 服务并获取本人 API Key，填入本地 `mem0_service/.env`（该文件已被 `.gitignore` 排除，仓库不含任何可用凭据）；
2. 自行遵守《火山引擎服务条款》等适用协议，并自行承担服务调用产生的费用、配额与账号合规责任；
3. 不得在本项目中内置、分享他人 Key，或绕过服务计费与访问控制。

## 免责声明

本项目按“现状（AS IS）”提供，不附带任何明示或默示担保。作者不对依赖其运行所产生的记忆数据正确性、服务可用性或任何直接 / 间接损失负责；生产环境使用前请自行评估与测试。
