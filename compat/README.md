# LivingMemory structured request handoff

This compatibility patch pairs Group Chat Plus continuity support with LivingMemory 2.9.1 on AstrBot 4.29.4. The LivingMemory source commit is `f43f6c6`; the patch changes only `core/event_handler_modules/memory_recall.py`. Its contract tests are recorded in the companion source commit. No dependency, configuration or persisted memory changes are required.

Apply the patch to the matching LivingMemory source after checking its base and preserving local edits. Validate and deploy LivingMemory first, then deploy Group Chat Plus. Use the normal scoped plugin reload procedure for each plugin. The patch file and this note are maintenance artifacts, not runtime files. Never apply the patch automatically during plugin startup.

Group Chat Plus requests a handoff only for its explicitly enabled behavior sessions. LivingMemory publishes an ephemeral payload only when `_gcp_lm_handoff_requested` is the current request object. The payload binds that same request and the original temporary memory part to a copied structured memory list and its native formatter. Group Chat Plus checks object identity, temporary marking and a complete native formatting round-trip before selecting whole entries within the character budget. Consumption and request termination clear the owned handoff.

Without this compatible producer, Group Chat Plus preserves unstructured memory text and reports budget observations. Other requests retain normal LivingMemory behavior. The patch neither adds memory retrieval nor modifies persona or learning data. After a future LivingMemory upgrade, revalidate this identity-bound contract before reapplying the patch; do not infer entry boundaries from rendered memory text.

Validation used `astrbot-rhonin:4.29.4-local-20261106-r2`: all Group Chat Plus tests passed (495 tests and 134 subtests), the three LivingMemory handoff contract tests passed, and a native request/TextPart/formatter probe verified structured selection and cleanup. These checks establish local pipeline compatibility; deployment and live acceptance remain separate steps.

## 群成员身份适配

`livingmemory-member-identity-v1.patch`基于上述结构化交接版本f43f6c6，增加请求绑定的身份查询锚点及近邻顺序修复。运行基线6c7625c。部署顺序为Self Learning身份服务、LivingMemory、GCP；不向上游自动推送。完整协议与配置见[实现记录](../docs/maintenance/2026-11-06-member-identity.md)。

`self-learning-member-identity-v1.patch`基于自学习本地基线7ecae7a，目标5139e185，包含独立身份服务、配置页与生命周期适配。它是已有自用改动之上的增量，不可直接套在不同上游提交；需先核对基线。禁止覆盖原有学习数据库。

`self-learning-pilot-repairs-20261106.patch`基于0252d3b，修复合法点号和@会话的LightRAG路径校验、纯媒体采集隔断及匿名采集统计，目标4c8411d。保留原目录名称与知识索引，不迁移身份数据。

## 2026-11-07撤销状态与完整记忆修复

`self-learning-scoped-denials-20261107.patch`基于4c8411d，目标749320b，按当前已选成员携带有效称呼撤销，兼容旧unresolved字段。同名按主体隔离；每成员最多八项，正文提及优先。未扩大原有自然语言解析语法。

`livingmemory-complete-recall-and-alias-denial-20261107.patch`基于6c7625c，目标034f022，移除条目正文800字符截断；普通路径按完整条目执行软预算，精确opt-in交接全部候选及同一个无截断formatter，由GCP选择完整条目。否认别名只从对应成员的检索锚点中排除。无需依赖、配置或数据库迁移。

GCP消费提交6f97dae。两份补丁均为本地增量，应用前核对基线，禁止启动时自动套用或覆盖人格与学习数据库。最终验证使用r3镜像；LM发布Git blob原始字节通过1238后端及43原生召回场景，SL通过1235项及119子测试。自然语法12项既有缺口与工具停滞行为仍需后续开发，不据此扩大试点。

## 2026-11-07自然称呼语法补齐

`self-learning-natural-nicknames-20261107.patch`基于749320b，目标9eefa65，补齐固定标注的12项语法缺口，加入限定整句解析、语气词与引号边界、复合更正的原子事务。冻结114标注全部通过；无数据库迁移、依赖、配置或作用域变更。首次未知且未加引号的尾“吧”按口语语气词解释，真实字面名字使用引号消歧；已知完整名保留，两种解释都已知时不自动更新。不得通过批量重写既有学习记录消除歧义。

## 2026-11-07反思重试耗尽保护

`livingmemory-preserve-exhausted-reflections-20261107.patch`基于034f022，目标cf6775d，包含运行修复及配套测试。反思重试耗尽后保留原pending区间和游标，暂停自动反思，避免将未存储消息视为已处理；默认不带数量的`/lmem summarize`从保留的原始消息重新生成摘要并尝试存储，成功后提交，失败继续保留。显式数量参数维持原有选取语义，不等于恢复全部积压。

生产只部署`core/event_handler_modules/memory_reflection.py`，无依赖、配置、数据库或人格迁移。r4后端1248项通过，未变化的前端产物构建检查因隔离镜像缺少esbuild而单独排除；最终Git blob的33项聚焦测试通过。外部embedding超时与SDK重试行为未修改。升级时需保留既有本地补丁并核对基线，不得在启动时自动套用。本次调查与发布见[维护记录](../docs/maintenance/2026-11-07-r4-finalization-memory-timeout.md)。
