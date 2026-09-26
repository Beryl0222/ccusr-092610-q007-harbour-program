# 领域约定

2026“天涯共此时”中秋活动启动，包含江苏文旅推介和多地协作；相关节庆项目跨境巡演和场地资源需衔接多方安排。

聚合对象包括`program_revision`、`venue_slot`、`resource_claim`、`show_record`。事件类型包括`PROGRAM_CHANGED`、`ATTENDANCE_CONFIRMED`、`RESOURCE_HELD`、`SHOW_FROZEN`、`CHANGE_REVIEWED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `PROGRAM_CHANGED`：载荷还需包含 `supersedes`, `reason`。
- `RESOURCE_HELD`：载荷还需包含 `resource_ref`, `slot`。
- `SHOW_FROZEN`：载荷还需包含 `revision`, `frozen_at`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
