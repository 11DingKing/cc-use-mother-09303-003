# 中外教师互访编排

本项目维护中外教师互访编排的领域约定、角色边界与样例数据，并提供面向互访协调员的完整服务端：邀请资格、课程需求、可用时段、跨时区截止、行程缓冲与接待配额统一排程；申请阶段暂占关联资源，材料齐备后才确认；改期、取消、替代教师与逾期释放均为原子调整；身份材料按角色隔离；冲突返回可选方案；进程恢复后继续清理遗留暂占。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/exchange_service/`：互访编排服务端（排程、暂占/确认、原子调整、角色隔离、恢复清理）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动 HTTP 服务（启动前自动恢复清理）。
- `tools/demo_scenario.py`：日期提前冲突场景的端到端演示。
- `tests/`：契约与服务端回归测试。

## 服务端设计

- **状态机**：邀请 → 准备 → 暂占 → 确认 → 访问（另：完成 / 取消 / 逾期）。
- **统一排程**：`scheduler.plan_window` 一次评估教师行程（含缓冲）、每周课程时段、
  接待宿舍（占用窗口含行程缓冲）、接待配额；冲突时 `find_alternatives` 给出前后平移的可选窗口。
- **暂占与确认**：申请阶段创建带过期时间的暂占（`min(TTL, 材料截止)`）；
  护照、签证、邀请函全部核验通过后才允许确认。
- **原子调整**：改期（释放旧占用 + 建立新占用）、取消、替代教师（换人 + 材料重置 +
  占用回退暂占）、逾期释放均在单个 SQLite 事务中提交，并写审计日志。
- **跨时区截止**：材料截止 = 访问开始前 N 天、接收院校当地 17:00（IANA 时区，含夏令时），统一存 UTC。
- **角色隔离**：材料内容仅协调员 / 派出院校 / 教师本人可读；接收院校只能看到清单状态。
- **逾期与恢复**：`sweep()` 释放逾期暂占并推进访问状态；`recover()` 在进程启动时调用，
  继续清理遗留暂占并修复“暂占但无有效占用”的残留申请。

## HTTP 接口

身份通过请求头携带：`X-Actor-Role`（coordinator / home_institution / host_institution / teacher）、
`X-Actor-Id`、`X-Actor-Institution`（院校角色必填）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/invitations` | 发出邀请（校验邀请资格，计算跨时区材料截止） |
| POST | `/applications/{id}/prepare` | 接受邀请，进入材料准备 |
| POST | `/applications/{id}/materials` | 提交身份材料 `{kind, content}` |
| POST | `/applications/{id}/materials/{kind}` | 核验材料 `{"verify": true}`（仅协调员） |
| GET | `/applications/{id}/materials` | 材料清单（按角色隔离） |
| GET | `/applications/{id}/materials/{kind}` | 读取材料内容（接收院校 403） |
| POST | `/applications/{id}/hold` | 暂占关联资源（冲突返回 409 + 可选方案） |
| POST | `/applications/{id}/confirm` | 材料齐备后确认 |
| POST | `/applications/{id}/reschedule` | 改期（原子替换占用） |
| POST | `/applications/{id}/substitute` | 替代教师（原子重置） |
| POST | `/applications/{id}/cancel` | 取消（原子释放） |
| GET | `/applications/{id}` | 申请详情（含占用与材料清单） |
| POST | `/maintenance/sweep` | 逾期清理与访问状态推进 |
| POST | `/maintenance/recover` | 恢复清理（服务启动时自动执行） |

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

启动服务：`python3 tools/run_server.py --db data/exchange.db --port 8080 --seed`

场景演示：`python3 tools/demo_scenario.py`
