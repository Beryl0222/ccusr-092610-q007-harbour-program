# 领域约定

2026“天涯共此时”中秋活动启动，包含江苏文旅推介和多地协作；相关节庆项目跨境巡演和场地资源需衔接多方安排。

聚合对象包括`program_revision`、`venue_slot`、`resource_claim`、`show_record`。事件类型包括`PROGRAM_CHANGED`、`ATTENDANCE_CONFIRMED`、`RESOURCE_HELD`、`SHOW_FROZEN`、`CHANGE_REVIEWED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `PROGRAM_CHANGED`：载荷还需包含 `supersedes`, `reason`。
- `RESOURCE_HELD`：载荷还需包含 `resource_ref`, `slot`。
- `SHOW_FROZEN`：载荷还需包含 `revision`, `frozen_at`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。

## 服务层语义（`harbour_program.service`）

`CommitmentService` 在契约之上兑现资源承诺，状态可持久化到 JSON 文件，重启后原样恢复。

- **权限**：合作城市（`partner`）只能确认自己城市的节目事实（到场、设备申报、费用）；设备状态只能由舞台负责人（`stage_manager`）确认；最终顺序只能由主办方（`organizer`）冻结；现场变更由值班人员（`duty`）或主办方办结。
- **版本与替补**：节目改版从 1 递增并记录 `supersedes` 与原因；替补保留被替换节目的内容快照和原因，原位替换顺序；已演出记录不可改写为新版本。
- **原子锁定**：设备和时段被并行节目共享，`lock_resources` 要么整批成功要么整批失败，冲突时给出占用方；同一节目重复锁定同一批资源按重放处理。
- **幂等与漂移**：相同到场回执重放不产生新事件；同一回执标识携带不同内容即内容漂移，原回执转为争议并记入冲突审计，必须用新回执重新确认。设备清单变更同样使既有舞台确认失效。
- **时间**：跨午夜的装台、演出、撤场按场地本地日期归天；`sweep_expired` 只释放仍处于开放状态的窗口，已过时段在服务重启后不会再次释放资源。
- **看板**：`session_board` 回答指定场次目前可执行的节目顺序、仍缺的确认及其冲突来源（占用方、漂移回执）；`pending_changes` 列出未办结的现场变更供值班人员恢复处理。
