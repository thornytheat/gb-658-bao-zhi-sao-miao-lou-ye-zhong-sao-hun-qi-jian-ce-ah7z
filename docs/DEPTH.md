# 深度登记 扫描期版质检台

- 规模：中型
- 核心规则：期号、版次、页码构成业务键，图像相似只生成候选，页眉证据决定混期。
- 使用者与工作流：质检员导入扫描清单，核对期版与页序，裁决重扫和混期，归档证据。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 清单采信 | 导入扫描清单并定位坏行 | POST /api/scans/import | `app/modules/m1_intake/parser.py` `app/modules/m1_intake/integrity.py` | `tests/test_m1_intake.py` |
| M2 期版契约 | 规范期号、版次和页码 | POST /api/issues/normalize | `app/modules/m2_issue/rules.py` `app/modules/m2_issue/catalog.py` | `tests/test_m2_issue.py` |
| M3 页码序列 | 查缺页、重页与越界页 | POST /api/pages/check | `app/modules/m3_pages/sequence.py` `app/modules/m3_pages/reconcile.py` | `tests/test_m3_pages.py` |
| M4 重扫判定 | 比较扫描候选并保留清晰版本 | POST /api/rescans/evaluate | `app/modules/m4_rescans/similarity.py` `app/modules/m4_rescans/decision.py` | `tests/test_m4_rescans.py` |
| M5 混期归因 | 用页眉和页码证据裁决混期 | POST /api/mixed/evaluate | `app/modules/m5_mixed/evidence.py` `app/modules/m5_mixed/conflicts.py` | `tests/test_m5_mixed.py` |
| M6 批次对账 | 检查四账并导出证据 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |
| M7 证据交付 | 登记原件、按站点裁剪交付、审计查阅记账 | POST /api/evidence/* | `app/modules/m7_evidence/service.py` `app/modules/m7_evidence/materials.py` `app/modules/m7_evidence/access_log.py` | `tests/test_m7_evidence.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 首列带 BOM | 期号字段取不到 | 读取表头时移除 BOM | `app/modules/m1_intake/parser.py` | `tests/test_m1_intake.py::test_strip_bom_header` |
| TRAP-05 | 行列数不齐 | 错位行进入页序 | 隔离并保留物理行号 | `app/modules/m1_intake/parser.py` | `tests/test_m1_intake.py::test_column_count_goes_quarantine` |
| TRAP-07 | 页眉为空 | 空串被当作一致 | 返回未知并转人工复核 | `app/modules/m5_mixed/evidence.py` | `tests/test_m5_mixed.py::test_missing_header_is_unknown` |
| TRAP-08 | 前导零页码 | 转整数后无法复核原编号 | 规范为三位字符串 | `app/modules/m2_issue/rules.py` | `tests/test_m2_issue.py::test_page_zero_pads_without_numeric_roundtrip` |
| TRAP-09 | 大摘要或相似哈希 | 数字往返或只凭相似度误判 | 摘要保形，相似仅生成候选 | `app/modules/m5_mixed/conflicts.py` | `tests/test_m5_mixed.py::test_hash_candidate_without_header_needs_review` |
| TRAP-14 | 同版多扫描值冲突 | 后写覆盖清晰版本 | 版本、清晰度、时间稳定择优 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_duplicate_page_detected` |
| TRAP-16 | 清单尾行截断 | 半行被当成扫描件 | 完整尾行检查并隔离 | `app/modules/m1_intake/integrity.py` | `tests/test_m1_intake.py::test_truncated_tail_detected` |
| TRAP-19 | 候选顺序改变 | 先到者决定保留项 | 显式排序键保持顺序无关 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_selection_order_independent` |
| TRAP-22 | 同内容多别名 | 合并顺序改变代表扫描 | 清晰度后以时间和文件名择一 | `app/modules/m4_rescans/decision.py` | `tests/test_m4_rescans.py::test_selection_is_deterministic_on_tie` |
| TRAP-26 | 陈旧重扫晚提交 | 旧版本覆盖新版 | 版本优先于输入顺序 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_higher_version_wins_not_later_input` |
| TRAP-27 | 整包交付含他站原始行 | 一字不动整包下发会泄露别站材料 | 原件完整保留，交付按站点凭据裁剪 | `app/modules/m7_evidence/materials.py` | `tests/test_m7_evidence.py::test_station_delivery_is_clipped_to_own_rows` |
| TRAP-28 | 删去他站行再交付 | 断掉总包追责依据 | 原始行号与原始文本原样留存，只裁剪视图 | `app/modules/m7_evidence/materials.py` | `tests/test_m7_evidence.py::test_materials_parse_preserves_raw_rows` |
| TRAP-29 | 审计先出原文后记账 | 记账失败仍泄露整批原件 | 先记账提交，成功后才返回原文 | `app/modules/m7_evidence/service.py` | `tests/test_m7_evidence.py::test_audit_log_failure_returns_no_content` |
| TRAP-30 | 解除与取件并发 | 解除后才发出的正文无法追回 | 终止标记 + 交付闸门，解除为准 | `app/modules/m7_evidence/auth.py` | `tests/test_m7_evidence.py::test_threaded_termination_beats_concurrent_delivery` |
| TRAP-31 | 记账中断后重试 | 重复登记或恢复出别的请求 | request_id 唯一，重试复用原账目 | `app/modules/m7_evidence/access_log.py` | `tests/test_m7_evidence.py::test_audit_interruption_resumes_same_request` |

## 对账规则

- 应有页数 = 唯一采纳页 + 缺页数。
- 扫描件数 = 唯一采纳 + 被替代重扫 + 混期 + 隔离。
- 同一输入与规则版本重跑，运行标识和结果行数不变。

## 证据交付规则

- 原件登记只读不删：批次原始文本、原始物理行号、原始字段整体留存，站点交付是过滤视图。
- 站点交付范围 = 该站凭据对应行的并集；请求中出现不属于本站的行号按越权拒绝。
- 审计读取 = 整批原件 + 声明用途；每次读取先写查阅账并提交，记账失败不返回原文。
- 解除合作后旧凭据立即失效，重新合作签发全新凭据，旧摘要永久不可用。
- 解除与取件并发以解除为准：取件在交付闸门内做最终校验，终止标记可见即拒绝。
- 审计请求按 request_id 幂等：重试复用原账目，中断后恢复原请求，不重复登记。
- 采集栏位约定与补扫派工不在本模块改动范围。

## 已知不覆盖

- 不读取图像二进制，感知哈希与 OCR 页眉由上游扫描软件提供。

