# LivingMemory structured request handoff

This compatibility patch pairs Group Chat Plus continuity support with LivingMemory 2.9.1 on AstrBot 4.29.4. The LivingMemory source commit is `f43f6c6`; the patch changes only `core/event_handler_modules/memory_recall.py`. Its contract tests are recorded in the companion source commit. No dependency, configuration or persisted memory changes are required.

Apply the patch to the matching LivingMemory source after checking its base and preserving local edits. Validate and deploy LivingMemory first, then deploy Group Chat Plus. Use the normal scoped plugin reload procedure for each plugin. The patch file and this note are maintenance artifacts, not runtime files. Never apply the patch automatically during plugin startup.

Group Chat Plus requests a handoff only for its explicitly enabled behavior sessions. LivingMemory publishes an ephemeral payload only when `_gcp_lm_handoff_requested` is the current request object. The payload binds that same request and the original temporary memory part to a copied structured memory list and its native formatter. Group Chat Plus checks object identity, temporary marking and a complete native formatting round-trip before selecting whole entries within the character budget. Consumption and request termination clear the owned handoff.

Without this compatible producer, Group Chat Plus preserves unstructured memory text and reports budget observations. Other requests retain normal LivingMemory behavior. The patch neither adds memory retrieval nor modifies persona or learning data. After a future LivingMemory upgrade, revalidate this identity-bound contract before reapplying the patch; do not infer entry boundaries from rendered memory text.

Validation used `astrbot-rhonin:4.29.4-local-20261106-r2`: all Group Chat Plus tests passed (495 tests and 134 subtests), the three LivingMemory handoff contract tests passed, and a native request/TextPart/formatter probe verified structured selection and cleanup. These checks establish local pipeline compatibility; deployment and live acceptance remain separate steps.

## 群成员身份适配

`livingmemory-member-identity-v1.patch`基于上述结构化交接版本f43f6c6，增加请求绑定的身份查询锚点及近邻顺序修复。运行基线6c7625c。部署顺序为Self Learning身份服务、LivingMemory、GCP；不向上游自动推送。完整协议与配置见[实现记录](../docs/maintenance/2026-11-06-member-identity.md)。

`self-learning-member-identity-v1.patch`基于自学习本地基线7ecae7a，目标5139e185，包含独立身份服务、配置页与生命周期适配。它是已有自用改动之上的增量，不可直接套在不同上游提交；需先核对基线。禁止覆盖原有学习数据库。
