# 群成员昵称、账号和花名感知调查

日期：2026-11-06。范围：GCP、自学习与LivingMemory。此次仅调查和维护路线图，没有修改运行代码、配置、人格或学习记忆数据，没有重载或发送群测试消息。

## 结论

基础账号与显示名并非完全丢失。主要缺口在于没有贯穿三个插件的“稳定账号—历史显示名—群内花名”事实链：采集保留了部分身份字段，但花名提炼、带证据归并、持久化更新、按身份召回及模型可见内容没有完整接通。增加提示词不能补足这些环节。

显示名变化是观察事实；其他成员的称呼是需要消歧的关系声明。两者不能直接等同，也不能把参与某段聊天当成该段所有事实的主体。

## 线上核对

2026-11-06 12:11–12:16 CST，读取当前配置并逐文件核对哈希：GCP `5e29427` 的4个入口/消息模块、LM `f43f6c6` 的9个相关模块、自学习 `7ecae7a` 的8个相关模块均与线上一致。未仅凭旧版本说明作结论。

| 插件 | 实际配置 | 含义 |
| --- | --- | --- |
| GCP | `include_sender_info=true`，`max_context_messages=20`，`enable_memory_injection=true`，LivingMemory模式 | 发送者信息已开启；有限历史不能替代长期身份注册表。配置中的`livingmemory_top_k=3`不能直接当作原生hook实际召回数，需区分手动与原生路径 |
| 自学习 | 消息采集、自动学习、社交注入、关系注入开启；计划间隔6小时 | 采集和计划学习配置仍开启；实际执行与成功产出需批次证据，实时学习关闭不代表关闭采集 |
| 自学习 | `llm_hook_local_only=true`、`llm_hook_include_v2=false`、`enable_realtime_v2_processing=false`；hook向临时内容注入，允许3个会话，含当前试点 | 为低延迟关闭V2知识召回热路径；知识摄取与回复可见性不能混为一谈 |
| LM | 全群采集开启，图谱开启，手工`identity_aliases`为空，`enable_agentic_recall_beta=false` | 具备身份元数据，但按当前发言人检索的Beta工具未启用，普通召回没有自动获得同等能力 |

只读数据库聚合，不导出任何QQ、姓名或聊天正文：

| 指标 | 数值 | 限制 |
| --- | --- | --- |
| 有效JSON元数据的记忆文档 | 1175 | 全库存量，不是本次发布后新增量 |
| 含非空participant_identities的文档 | 1161 | 表示身份元数据存在，不证明事实归属正确或回复使用了它 |
| 参与者身份记录 | 12631 | 不同记忆可重复包含同一人，不是独立人数 |
| aliases数量大于1的身份记录 | 402 | 来源是显示名观察，不代表自动学会群友花名 |
| user角色消息 | 4610 | 当前会话库保留样本，不代表所有历史 |
| 名称为空或等于sender_id | 771 | 采集名称质量存在缺口；不据此断言全由某一适配器造成 |
| 同会话同账号观察到多个sender_name的组合 | 12 | 说明同账号多显示名现象存在，可能来自改名、群名片或采集路径差异，不表示全部别名已确认 |

## GCP链路

- `utils/message_processor.py:235` 将昵称与完整ID格式化进入消息；`utils/reply_handler.py:387` 提醒模型当前对话对象，`utils/context_manager.py:1885` 保存发送者ID和名称。基础字段采集/注入已存在。
- `main.py:12404` 的普通@识别优先读取`At.qq`、`At.name`；缺失名称时保留空值。`utils/message_processor.py:108` 可显示“解析失败”。普通@路径未提供统一的成员名称补全；入群欢迎代码中的成员查询不等于普通聊天具备同样能力。
- 未发现按群维护的成员别名事实注册表或从第三人称呼自动提炼身份的链路。绘图主体别名配置面向角色设定，不能当作真实群成员身份表。
- 新增的`utils/turn_continuity.py`仅为按会话、发言人及人格隔离的短期任务/话题缓存，TTL600秒；不具备长期花名学习。
- `utils/request_compatibility.py:30`保留其他插件的增量上下文，当前自学习通过`extra_user_content_parts`注入。不能将全部问题归因于GCP重写prompt丢掉自学习内容。

## 自学习链路

源码基线：`7ecae7a`。

- `services/learning/message_pipeline.py:116`保存sender_id、sender_name及引用信息，采集不依赖实时学习开关。
- `services/analysis/multidimensional_analyzer.py:131`创建`nickname_mapping`，`:1120`读取以解析文本@昵称，但业务源码未发现填充入口；普通非@花名也不走此解析。
- 用户画像模型和存储已有`qq_name`、`nicknames`字段；画像分析入口、部分关系分析/实体抽取方法在业务源码中仅见定义，不能把服务实例化当作链路接通。
- `services/integration/lightrag_knowledge_manager.py:206`按`[昵称]: 正文`摄取，没有稳定sender_id锚点；可以提取文本知识，但不能保证“这个昵称属于哪个QQ”。
- `services/hooks/llm_hook_handler.py:316`在当前local-only配置下跳过V2知识召回。LightRAG摄取成功不代表它的知识会经当前SL hook进入回复；可能的其他显式工具调用应另行验收。
- `services/social/social_context_injector.py:618`主要显示已有关系及ID，不能补上未学习的花名。`services/jargon/jargon_miner.py:241`明确排除人名、昵称和ID，黑话学习不是昵称学习的后备路径。
- 潜伏接口问题：分析器`:836`传`load_user_profile(group_id, qq_id)`，数据库接口`services/database/sqlalchemy_database_manager.py:1188`仅收qq_id；保存路径还将group_id用作用户键。目前未找到实际调用链，不能据此宣称已经发生线上异常，接入前必须回归修正。

## LivingMemory链路

源码基线：`f43f6c6`。

- `core/managers/conversation_manager.py:129`、`:422`取得稳定ID和单一sender_name，未分别保存账号昵称、群名片及偏好称呼。
- `core/processors/memory_processor.py:516`、`:602`构建`participant_identities`；aliases来自同一总结窗口内的实际发送者显示名，不提炼“甲把乙称作丙”的可信身份关系。
- `storage/graph_store_write.py:132`更新节点时替换metadata，不累计各窗口别名；旧别名可能仍在旧记忆中，不能说已从所有数据删除。
- `core/event_handler_modules/memory_recall.py:214`、`:244`、`:267`用当前原文和近邻content查询，保留会话与人格过滤，但不按当前发言人稳定身份过滤，也不拼入近邻消息的sender旁列。原文中已有姓名或At标签仍可能参与查询。
- `core/utils/formatting.py:43`、`:76`将参与者显示为`display_name#账号后四位`，不展示aliases；这与GCP当前消息提供完整ID不同。不能把尾号作为无碰撞身份键。
- Beta `current_speaker`工具支持稳定身份检索，但当前关闭；即使开启，也不提供通用可信的“第三人花名→账号”解析。`core/managers/memory_engine_participant.py:216`保留名称兼容回退，不能把其弱匹配当作身份确认。
- 图谱`mentioned_in`表达参与/关联，不足以确认事实主体。手工`access_control.identity_aliases`没有群维度，不宜直接作为自动群花名仓库。
- 另有独立顺序疑点：`storage/conversation_store_messages.py:173`返回升序，`memory_recall.py:218`按降序处理并跳过首项、反转剩余。需用三条合成消息验证漏旧、重复当前和顺序问题，与花名缺口分开修复。

## 推进顺序

先补统一身份契约及真实调用链测试，再补有证据的花名候选提炼和持久化，最后按确认身份召回并向模型提供精简、可信的身份上下文。别名应有群范围、来源、置信度、纠正和撤销；默认保留歧义。避免一次性开启所有实时学习与图谱召回，重新引入延迟。

开发项、此前已部署实验、样本门槛、负例和扩大范围条件均列入[路线图](../ROADMAP.md)。本轮没有将未验证能力标记为完成。
