# 文化创意业务协作与纹样授权计费结算服务

本仓库包含两个仅依赖 Python 标准库与 SQLite 的服务：

- **creative_program_foundation**：文化创意赛事与成果转化业务共享的基础能力（机构、节点、操作者、资料登记、角色权限、请求幂等与哈希串联审计）。
- **pattern_license_settlement**：「西城纹韵」等获奖纹样的授权计费与结算服务。

## 纹样授权计费与结算服务（pattern_license_settlement）

管理授权作品与版本、权利人份额、被许可方（含实际控制方）、地域/渠道/用途范围、
有效期、保底金、阶梯费率、税前扣减规则与报送周期。

### 业务规则

- **幂等导入与去重**：用量报送带 `request_id` 与业务去重键；同键不同内容冲突，重复报送回放原回执。
- **范围匹配**：按作品、地域、渠道、用途、使用日期匹配有效许可证；超范围与许可证失效分别归类为
  `out_of_scope` / `license_expired`，不形成应收而进入待追偿项目。
- **不可覆盖的计费分录**：分录只追加，记录费率快照、逐单位阶梯分段与合并数量游标；关账后分录冻结。
- **实际控制方合并阶梯**：阶梯数量游标按 `(作品版本, 实际控制方, 用途)` 持久化，同一控制方旗下
  多个被许可方主体在临界点前拆分报送仍沿同一游标落档。
- **迟到更正**：关账后补交的退货/更正只能进入后续开放期间，正数为 `supplement` 追补、
  负数为 `reversal` 冲销（按原始毛额/扣减比例红冲，不占用阶梯游标）。
- **保底金**：关账时对本期净额低于保底的许可证自动补足。
- **争议托管**：争议只隔离相关金额到托管账户；无争议部分照常回款与支付。裁定 `release`
  按份额放行给权利人，`reject` 退还给合作方，按争议登记顺序处理。
- **复式账与平衡**：金额一律整数分；应收/追偿/现金/清算/托管五个账户借贷必相等，
  全部账户余额之和恒为零。
- **持久队列与重启**：关账、争议裁定、付款各有 FIFO 持久队列；重启时处理中的项目自动回到
  待处理状态并保持原顺序，付款严格按入队顺序执行、不跳付。
- **权限视图**：运营方（admin/operator）、合作方（partner，仅限本方实际控制的被许可方）、
  权利人（rights_holder，仅限自己的份额与作品）、审计（auditor 只读）。
- **审计重算**：审计人员可重算任一期间，逐分录按快照分段复算毛额、核对净额、分配合计与
  全局试算平衡，并可经 `/entries/{id}/explain` 解释每笔金额来源。

### 主要 HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/actors` | 登记操作者（角色：admin/operator/partner/rights_holder/auditor） |
| POST | `/works` `/rate-cards` `/work-versions` | 作品、阶梯费率卡（含税前扣减）、作品版本与权利人份额（基点合计 10000） |
| POST | `/licensees` `/licenses` `/license-status` | 被许可方（含 controlling_party_id）、许可证范围/有效期/保底/周期、状态变更 |
| POST | `/usage-imports` | 幂等导入用量（支持 `correction_of` 迟到更正） |
| POST | `/billing` | 生成不可覆盖的计费分录 |
| POST | `/disputes` `/disputes/resolve` | 登记争议 / 请求裁定（release/reject） |
| POST | `/periods/close` `/queues/close` | 关账入队 / 按序处理 |
| POST | `/cash-receipts` | 合作方回款 |
| POST | `/payouts/enqueue` `/queues/payments` | 生成付款指令 / 按序付款 |
| POST | `/claims/assess` `/claims/recover` `/claims/write-off` | 追偿核定、回款、核销 |
| GET | `/usage` `/entries` `/distributions` `/claims` `/payments` | 权限范围内查询 |
| GET | `/entries/{id}/explain` | 逐笔解释金额来源（费率快照、分段、分配、复式账） |
| GET | `/recompute?period_key=` | 审计重算期间并校验平衡 |
| GET | `/settlements/{period}` | 期间汇总（应收/已付/托管/余额/核销/追偿） |

## 目录

- src/creative_program_foundation/：基础服务的模型、存储、权限、审计链、HTTP 路由和离线验收；
- src/pattern_license_settlement/：纹样授权计费结算的模型、纯计价逻辑、复式账、存储、领域服务、
  HTTP 路由和离线验收；
- tests/：纯计价、领域规则、队列保序、事务平衡、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m creative_program_foundation.acceptance
    PYTHONPATH=src python3 -m pattern_license_settlement.acceptance

纹样服务的验收场景覆盖：同一实际控制方两家被许可方各报 60 件试图压在 100 件阶梯临界点下
（合并后按 99 件@100 分 + 21 件@80 分计价）、幂等回放、超范围追偿、争议 1000 分托管不阻断
其他付款、关账冻结、Q4 迟到退货按原始比例红冲、服务重启后付款队列按原顺序推进，
以及审计重算与全部账户归零。

## HTTP 服务

    PYTHONPATH=src python3 -m pattern_license_settlement.api --database settlement.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health（返回审计链与试算平衡状态）。写入接口通过 X-Actor-Id 标识操作者，
服务重启后 SQLite 中的分录、队列、审计历史与账户余额继续保留。
