# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。

## 证据交付

- batch_id：证据原件登记批次，与业务 run_id 相互独立。
- original_text：登记时提交的完整原始材料，逐字节留存，不因交付而裁剪或删除。
- raw_line：物理行号对应的原始行文本，保留原作者字段与行内顺序。
- station_id：材料所属外包扫描站；由材料附加列或独立行映射给出，不写回原始 CSV。
- credential_sha256：站点凭据的 SHA-256 摘要；明文只在签发时返回一次，服务端不存明文。
- station.status：active 合作中，terminated 已解除；解除后旧摘要立即失效。
- request_id：一次取件的幂等标识，查阅账唯一键；重试与恢复复用同一记录。
- access_log.status：pending 已记账待交付，delivered 已交付，denied 已拒绝。
- access_log.purpose：审计读取原件时声明的用途，随账目留存。
- access_log.content_sha256：本次交付内容的摘要，用于事后核验交付范围与正文一致。
- scope：交付范围的机读描述；站点范围为 batch + station_id + 行号，审计范围为整批原件。

