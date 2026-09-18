# 运动员赞助资金管控台

面向职业运动员赞助合同、赛季预算和多币种流水的 Python 后端服务（仅依赖标准库）。

把赞助合同、里程碑验收、分期到账、训练与差旅票据、代扣税款和多币种结算串成
可追溯的预算账本，支持按运动员、合同、赛季查看承诺额、已用额、待验收款与
可支配余额，并在资金越过预警线时生成需要经纪人确认的处置任务。

## 运行

需要 Python 3.11 或更高版本：

```bash
python3 src/index.py
```

服务默认监听 `8000` 端口（`HOST`/`PORT` 环境变量可覆盖），持久化文件写入
`.runtime/`（`RUNTIME_DIR` 可覆盖）。访问 `GET /health` 确认进程状态。

```bash
python3 -m unittest discover -s tests   # 运行测试
docker compose up --build               # 容器启动
```

## 设计要点

- **事件溯源**：所有变更追加为 `.runtime/events.jsonl` 中的不可变事件，信封含
  业务时间 `business_date`、接收时间 `received_at`、接收序号 `seq` 与版本；
  重启后按序重放即可完整恢复账本、任务与幂等索引。
- **月度快照**：`POST /api/snapshots` 发布后该月份冻结，快照文件
  （`.runtime/snapshots/*.json`）不可改写。业务日期落在已冻结月份的迟到事件
  保留真实业务日期，但计入最近一个未冻结月份，不会改写已报出的快照。
  `GET /api/snapshots/{id}/verify` 可随时从事件日志复算快照并逐字段比对。
- **计算依据**：每个余额字段都带 `basis`（组成该数字的流水 ID / 分期版本），
  承诺额可定位到具体计划版本，流水之间通过 `links`/`linked_by` 双向追溯。
- **合同变更**：以 `effective_date` 为界，只允许修改到期日不早于生效日的分期，
  历史分期与已到账款项不受影响，每次变更产生新的计划版本。
- **幂等**：银行回单按 `(contract_id, bank_ref)` 去重，票据按 `receipt_ref`
  去重，重传返回原流水并标记 `duplicate: true`；所有写接口支持
  `Idempotency-Key` 请求头（或 `request_id` 字段）做请求级重放。
- **关联流水**：退款、冲正、汇率差额都是独立流水并通过 `links` 指向原始流水。
  外币票据按业务日汇率折算入账，银行实结金额与入账金额之差自动生成
  `fx_difference` 流水。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/athletes` | 登记运动员 |
| POST | `/api/contracts` | 创建合同（分期计划、预警线） |
| POST | `/api/contracts/{id}/amendments` | 合同变更（仅影响生效日起的分期） |
| POST | `/api/milestones/submit` / `approve` | 里程碑报验 / 验收 |
| POST | `/api/payments` | 赞助分期到账（`bank_ref` 幂等） |
| POST | `/api/expenses` | 训练/差旅/装备/税费票据（可多币种结算） |
| POST | `/api/tax-withholdings` | 代扣税款 |
| POST | `/api/refunds` / `/api/reversals` | 退款 / 冲正（关联原始流水） |
| POST | `/api/fx-rates` | 登记汇率 |
| GET | `/api/balances?athlete_id=&contract_id=&season=` | 四本账视图 |
| GET | `/api/ledger?...` | 流水明细（含关联关系） |
| POST | `/api/snapshots` | 发布月度快照 |
| GET | `/api/snapshots/{id}/verify` | 复算并校验快照 |
| GET | `/api/tasks` · POST `/api/tasks/{id}/resolve` | 预警处置任务与确认 |

金额一律为定点 6 位小数字符串（对齐 `reference/domain.json` 的
`quantity_precision`），错误响应统一为
`{"error": {"code": ..., "message": ...}}`。
