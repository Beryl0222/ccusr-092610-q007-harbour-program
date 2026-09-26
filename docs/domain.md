# 领域约定

2026“天涯共此时”中秋活动启动，包含江苏文旅推介和多地协作；相关节庆项目跨境巡演和场地资源需衔接多方安排。

聚合对象包括`program_revision`、`venue_slot`、`resource_claim`、`show_record`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `PROGRAM_REGISTERED`：载荷还需包含 `city`, `title`, `revision`, `content_hash`。
- `PROGRAM_CHANGED`：载荷还需包含 `supersedes`, `reason`。
- `SLOT_DEFINED`：载荷还需包含 `venue`, `tz`, `load_in_start`, `show_start`, `show_end`, `load_out_end`。
- `SHOW_OPENED`：载荷还需包含 `slot_id`, `order`。
- `ATTENDANCE_CONFIRMED`：载荷还需包含 `receipt_id`, `program_id`, `revision`, `content_hash`, `city`。
- `ATTENDANCE_DRIFTED`：载荷还需包含 `receipt_id`, `program_id`。
- `RESOURCE_HELD`：载荷还需包含 `resource_ref`, `slot`。
- `EQUIPMENT_CONFIRMED`：载荷还需包含 `confirmed_by`。
- `RESOURCE_RELEASED`：载荷还需包含 `reason`（`slot_elapsed` 表示时段已过自动结算）。
- `COST_COMMITTED`：载荷还需包含 `program_id`, `amount`, `currency`。
- `SHOW_FROZEN`：载荷还需包含 `revision`, `frozen_at`。
- `PERFORMANCE_RECORDED`：载荷还需包含 `program_id`, `revision`。
- `CHANGE_OPENED`：载荷还需包含 `change_id`, `description`。
- `CHANGE_REVIEWED`：载荷还需包含 `change_id`, `decision`。
- `CHANGE_RESUMED`：载荷还需包含 `change_id`。
- `CHANGE_CLOSED`：载荷还需包含 `change_id`, `outcome`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；契约层只定义可稳定交换的基础事实。

## 承诺服务（`harbour_program.service`）

`CommitmentService` 是契约之上的上层服务，状态完全由事件日志（`harbour_program.journal`）重建。

### 角色与权限

- 主办方（`organizer`）：登记场地窗口、开立场次、登记费用承诺、冻结最终节目顺序、审批现场变更。
- 合作城市（`partner_city`，携带城市名）：只可登记与确认自己城市的节目事实（节目版本、到场回执）。
- 舞台负责人（`stage_manager`）：锁定与释放设备、确认设备状态、记录演出。
- 值班人员（`duty_staff`）：登记并恢复未办结的现场变更。

### 业务规则

- 节目版本与替补：每次替换产生新版本，`supersedes` 与 `reason` 必填；被替换内容永久保留在旧版本中，可经 `program_history` 追溯。已演出记录（`PERFORMANCE_RECORDED`）不能改成新版本。已冻结场次的替换必须引用一笔已批准的现场变更。
- 资源锁定：设备与时段由并行节目共享（时段有并行容量）。一次锁定要么全部成交要么全部不成交；冲突时 `ConflictError.conflicts` 逐项给出冲突来源（设备被哪个节目占用、容量超限）。同一节目对同一批设备的重复锁定是幂等的。
- 到场确认：相同回执（同一 `receipt_id` 与内容）可重放；同一回执内容漂移则作废旧确认并要求重新确认；回执内容必须与当前节目版本一致。节目改版后旧确认自动作废。
- 场地窗口：装台、演出、撤场可跨午夜，演出日按场地本地日期（`show_start` 在场地时区下的日期）计算。
- 冻结：只有主办方可冻结，且场次内所有节目须完成到场确认、设备锁定并经舞台负责人确认、费用承诺登记；否则 `FreezeNotReady.missing` 给出仍缺清单。
- 重启恢复：服务重启重放事件日志；已过时段的活跃锁定在恢复时结算一次（事件标识 `settle-<claim_id>`），不会因反复重启再次释放资源。未办结的现场变更（open / approved / in_progress）在重启后仍可被值班人员恢复并办结。

### 查询接口

- `executable_order(show_id)`：指定场次目前可执行的节目顺序（冻结后按冻结版本），逐项给出就绪状态与阻塞原因，并标识时段是否已过。
- `missing_confirmations(show_id)`：仍缺的确认（到场、设备锁定、设备确认、费用、时段）及其冲突来源。
- `unfinished_changes(show_id=None)`：未办结的现场变更。
- `program_history(program_id)` / `slot_claims(slot_id)` / `cost_commitments(show_id)` / `performance_date(show_id)`。
