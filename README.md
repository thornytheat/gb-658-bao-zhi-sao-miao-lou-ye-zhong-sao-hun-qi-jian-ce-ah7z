# 扫描期版质检台

面向真实数据复核的中型数据工程工具，包含七个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18217。测试：docker compose exec -T app pytest -q

## 模块

- M1 清单采信：页面 /intake，接口 /api/scans/import
- M2 期版契约：页面 /issues，接口 /api/issues/normalize
- M3 页码序列：页面 /pages，接口 /api/pages/check
- M4 重扫判定：页面 /rescans，接口 /api/rescans/evaluate
- M5 混期归因：页面 /mixed，接口 /api/mixed/evaluate
- M6 批次对账：页面 /batches，接口 /api/reconcile
- M7 证据交付：页面 /evidence，接口 /api/evidence/*

## 证据交付

外包站点只需拿到本站错页证据去补扫；总包审计需要核验含其他站原始行的整批原件。
因此交付分为两条受控通道，原件整批完整保留，裁剪只发生在交付视图：

- 站点取件 `POST /api/evidence/stations/{station_id}/deliver`，凭本站凭据，只返回本站行；
  越权行号、他人站点、他站批次一律拒绝。
- 审计取件 `POST /api/evidence/audit/read`，凭审计令牌并声明用途；先写查阅账并提交，
  记账成功后才返回整批原件；记账失败不返回任何原文。
- 原下载入口 `GET /api/export/evidence.csv` 同样受控，必须指明批次并出示站点凭据。
- 解除合作 `POST /api/evidence/stations/{station_id}/terminate`：旧凭据立即失效；
  解除与取件并发时以解除为准。
- 查阅账 `GET /api/evidence/access`、交付范围 `GET /api/evidence/stations` 可回读。
- 审计幂等：`Idempotency-Key`（或请求体 `request_id`）唯一标识一次请求；重试复用原账目，
  中断后可用同一标识恢复原请求，不重复登记。

管理员登记站点与材料：`POST /api/evidence/stations`、`POST /api/evidence/materials`。
凭据通过 `X-Admin-Token`、`X-Audit-Token`、`X-Station-Credential` 请求头（或 Bearer）传递。

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

