# 结算纹样授权使用费协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

`pattern_license_billing` 包在这些边界上实现“西城纹韵”等纹样授权作品的**计费与结算服务**：管理授权作品与版本、权利人份额、被许可方与同一实际控制集团、地域/渠道/用途范围、有效期、保底金、阶梯费率、税前扣减规则与报送周期；使用事实经幂等导入、去重与范围匹配后按授权与费率版本生成不可覆盖的计费分录，拆分报送按控制集团合并判断阶梯；关账只冻结已确认分录，迟到更正以追补或冲销进入后续期间，争议只托管相关金额而不阻断无争议付款，许可证失效或超范围使用单独形成待追偿项目。运营方、合作方与权利人通过按权限隔离的 API 查看用量、应付与分配明细，审计人员可重算任一期间并解释每笔金额来源；服务重启后关账、争议与付款队列仍按原顺序推进，且总应收、已付、托管与余额始终平衡。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- src/pattern_license_billing/：授权作品与许可、阶梯费率、事实导入去重与匹配、不可覆盖计费分录、关账与保底、争议托管、待追偿、付款队列、分账、重算审计与 HTTP 路由；
- tests/：基础规则、事务边界、接口路由、计费规则、结算生命周期和端到端验收测试。

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
    PYTHONPATH=src python3 -m pattern_license_billing.acceptance

验收命令会在临时 SQLite 数据库中登记项目机构、操作者、业务节点和参考资料，核对幂等回执与审计链；授权计费验收则串起控制集团合并阶梯、幂等去重、保底补足、关账冻结、争议托管与冲销、跨期追补冲销、待追偿、队列顺序付款和重启后的余额恒等，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m pattern_license_billing.api --database licensing.sqlite3 --host 127.0.0.1 --port 8081

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

授权计费服务的主要接口（均通过 X-Actor-Id 鉴权）：

- POST /principals、/holders、/works、/work-versions、/entitlement-versions：登记身份、权利人与作品/版本/份额；
- POST /control-groups、/licensees、/licenses、/license-terminations：登记实际控制集团、被许可方与许可证范围；
- POST /rate-versions、/deduction-rules、/guarantees、/reporting-cycles：阶梯费率、税前扣减、保底金与报送周期；
- POST /usage-batches：幂等导入使用事实（去重、范围匹配、合并阶梯并生成分录）；
- POST /periods/close：关账冻结、保底补足并生成应收/应付付款队列；
- POST /disputes、/disputes/resolve：争议托管与解除/冲销；POST /orders/mark-paid：按队列顺序付款；
- POST /claims/resolve：待追偿项目的达成计费或核销；
- GET /entries、/entries/{id}、/facts、/claims、/disputes、/orders、/queue、/totals、/periods/{key}/recalculate、/audit-events。
