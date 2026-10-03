# 安宁礼仪与公墓运营服务

这是一个供殡仪馆、公墓和合作医疗机构使用的 Python 后端服务，统一管理逝者业务档案、遗体保管交接、送别厅与火化设备预约、服务订单、墓位权属、账单收款和审计时间线。系统把容易产生争议的交接、排程与收费动作保存在本地 SQLite 中，支持在单个 Linux 应用容器内离线运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

依次执行 python -m venv .venv、source .venv/bin/activate、python -m pip install -e ".[dev]"。可通过 PEACEFUL_CARE_DATABASE_PATH 指定数据库文件，默认写入项目的 data 目录。

## 初始化与启动

先执行 python -m app.cli init-db 和 python -m app.cli check-db，再用 uvicorn app.main:app --host 0.0.0.0 --port 8432 启动。健康检查为 GET /api/system/health。殡葬业务接口位于 /api/mortuary，涵盖档案、交接、资源、预约、服务订单、墓位权属、账单和时间线。生前契约接口位于 /api/preneed，覆盖签约冻结、分期收款、版本变更、逾期暂停解除转让，以及受益人死亡后向业务档案的转换。

## 测试与编译检查

测试命令：python -m pytest

编译命令：python -m compileall -q app tests

API 与 CLI 冒烟命令：python -m app.cli smoke、python -m app.cli mortuary-demo、python -m app.cli preneed-demo

## 目录结构

- app/mortuary：档案、保管交接、资源排程、权属和账单领域
- app/preneed：生前契约签约、版本、分期资金、状态机与身后转换领域
- app/api：登录、角色、审计及系统管理接口
- app/core：时钟、安全、异常、隐私与分页能力
- app/repositories：通用身份和审计数据访问
- app/services：会话、权限、后台任务及维护服务
- tests：领域、接口、异常路径和身份回归测试

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时事务。业务档案采用外部编号去重，保管交接与预约保留幂等键，服务订单开票后不可再次开票，支付流水不能重复分配。关键状态变化同时写入领域时间线；会话令牌仅保存摘要，审计记录不会保存明文密码或令牌。

生前契约在签约时冻结服务清单、数量、单价与价目表版本；每次变更生成新版本行，经客户确认后才生效，旧版本标记为 superseded 但永久保留，拒绝的版本标记为 rejected。分期按版本保存，收款流水与退款流水均以外部单号唯一去重；逾期由巡检任务在超过宽限期后标记并生成备忘会计事件，补缴后自动恢复。暂停、解除（仅财务经理并按退款规则计算可退金额）、受益人转让（留存前后受益人与客户确认）各有独立权限矩阵。所有会计事件借贷平衡，并以 (契约,事件,发生键) 防重复记账。只有受益人死亡后，处于有效状态的契约才能凭幂等键转换为业务档案、按冻结价格生成服务订单与账单；转换防重复，已收资金抵扣账单、未收余额保留为应收。GET /contracts/{id}/as-of 可回放任一时点的状态、生效版本、合同责任、资金差额与下一步动作。
