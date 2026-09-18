# 运动员赞助资金管控台

面向职业运动员赞助合同、赛季预算和多币种流水的 Python 后端服务（仅依赖标准库，Python 3.11+）。

服务把赞助合同、里程碑验收、分期到账、训练与差旅票据、代扣税款和多币种结算串成一条可追溯的预算账本，核心规则：

- **事件溯源**：所有业务事实追加写入 `.runtime/events.jsonl`，当前视图与历史月度快照都由事件流重放得到，重启后仍可复算。
- **合同变更只影响未来分期**：变更从生效日起重写尚未验收的分期；已验收/已到账的分期与生效日之前的分期保持不变。
- **迟到回执不改写已报出的快照**：每个事件同时记录业务时间（`business_date`）与接收时间（`received_at`），并归属一个“确认期间” `period`；业务月份若已报出快照，则顺延到第一个未报出的月份。
- **银行回单幂等**：`receipt_no` 相同的到账重传返回原事件（`duplicate: true`），不重复入账；所有写接口也支持客户端 `event_id` 幂等。
- **退款、冲正、汇率差额相互关联**：三类调整通过 `links_to` 指向原始流水，台账中可沿链路追溯。
- **预警处置任务**：可支配余额跌破合同预警线或转负时生成待确认任务，需经纪人确认（confirm/reject）后闭环。

## 运行

```bash
python3 src/index.py          # 默认监听 8000，数据写入 .runtime/
```

环境变量：`HOST`、`PORT`、`RUNTIME_DIR`（默认 `.runtime`）。测试：

```bash
python3 -m unittest discover -s tests
```

也可以运行 `docker compose up --build` 启动容器。

## 金额口径

按（合同 × 币种）分桶，合同视图汇总四个核心指标：

| 指标 | 含义 |
| --- | --- |
| `committed` | 承诺额：现行合同版本下有效分期总额（合同币种） |
| `used` | 已用额：费用票据 + 代扣税款（扣除退款/冲正） |
| `pending_acceptance` | 待验收款：尚未验收的分期余额 |
| `disposable` | 可支配余额：到账 − 已用 + 汇率差额 |

另提供 `received`（已到账）、`receivable`（已验收未到账）、`fx_difference`（汇率差额）与 `by_currency` 分币种明细。跨币种到账需在请求中携带 `fx_rate`，现金按到账币种入账、分期已付按合同币种折算。

## 接口一览

所有接口返回 JSON；写接口接受可选 `event_id`（幂等键）与 `received_at`（模拟接收时间）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/athletes` | 登记运动员 `{athlete_id, name, base_currency?}` |
| GET | `/athletes` | 运动员列表 |
| POST | `/contracts` | 登记合同 `{contract_id, athlete_id, season, currency, installments[], warning_threshold?, start_date?}` |
| GET | `/contracts/{id}` | 合同详情：分期状态、版本历史、余额视图 |
| POST | `/contracts/{id}/amendments` | 合同变更 `{effective_date, installments[]}`，仅影响生效日及之后的分期 |
| POST | `/events/milestone-approvals` | 里程碑验收 `{contract_id, installment_id, business_date}` |
| POST | `/events/sponsor-payments` | 分期到账 `{receipt_no, contract_id, installment_id, amount, currency, value_date, fx_rate?}` |
| POST | `/events/expenses` | 费用票据 `{contract_id, fund_use, amount, currency, business_date}` |
| POST | `/events/tax-withholdings` | 代扣税款 `{contract_id, amount, currency, business_date}` |
| POST | `/events/refunds` | 退款 `{links_to, amount, business_date, reason?}` |
| POST | `/events/reversals` | 冲正 `{links_to, business_date, reason?}`（全额抵消目标流水） |
| POST | `/events/fx-adjustments` | 汇率差额 `{links_to, amount, currency, business_date}`（金额可正可负） |
| GET | `/balances?athlete_id=&contract_id=&season=` | 承诺额/已用额/待验收款/可支配余额 |
| GET | `/ledger?contract_id=&athlete_id=&season=` | 流水台账，每笔附带过账后可支配余额 |
| GET | `/events?event_type=&period=&contract_id=` | 事件审计查询 |
| POST | `/snapshots/publish` | 报出月度快照 `{period: "YYYY-MM"}` |
| GET | `/snapshots` / `/snapshots/{period}` | 快照列表 / 详情（含计算依据 `basis`） |
| GET | `/snapshots/{period}/recompute` | 按水位线重放复算快照并校验一致性 |
| GET | `/tasks?status=&contract_id=` | 处置任务列表 |
| POST | `/tasks/{id}/confirm` | 经纪人确认 `{decision: confirmed\|rejected, note?, resolved_by?}` |

错误响应统一为 `{"error": {"code": ..., "message": ...}}`。

## 快照与复算

`POST /snapshots/publish` 报出某月快照时，服务记录水位线（`watermark`）、事件数与摘要（`basis`）。快照口径为“确认期间 ≤ 该月且 seq ≤ 水位线”的事件重放结果，因此：

- 迟到的验收/票据会顺延到未报出的月份，不会改写已报出的快照；
- 重启后 `GET /snapshots/{period}/recompute` 仍按同一水位线重放，逐字复算历史快照；
- 月度快照只能按期间递增报出，不能回补更早已报出的区间。
