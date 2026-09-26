# 香港节庆节目资源锁定账

2026“天涯共此时”中秋活动启动，包含江苏文旅推介和多地协作；相关节庆项目跨境巡演和场地资源需衔接多方安排。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/harbour_program/contracts.py`：基础契约校验。
- `src/harbour_program/journal.py`：JSONL 事件日志，服务状态的唯一事实来源。
- `src/harbour_program/service.py`：演出资源承诺服务（节目版本、场地窗口、到场确认、设备锁定、费用承诺、冻结与现场变更）。
- `src/harbour_program/cli.py`：命令行校验入口。
- `tests/`：信封、时间、版本、事件载荷与服务业务规则测试。
- `docs/domain.md`：领域对象、事件语义与服务业务规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m harbour_program.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 承诺服务用法

```python
from datetime import datetime
from zoneinfo import ZoneInfo
from harbour_program import Actor, CommitmentService, Role

HK = ZoneInfo("Asia/Hong_Kong")
svc = CommitmentService("data/journal.jsonl")  # 重启后重放日志恢复状态

organizer = Actor(Role.ORGANIZER, name="主办方")
stage = Actor(Role.STAGE_MANAGER, name="舞台负责人")
nanjing = Actor(Role.PARTNER_CITY, city="南京")

svc.define_slot(organizer, "slot-1", venue="香港文化中心露天广场", tz="Asia/Hong_Kong",
                load_in_start=datetime(2026, 9, 30, 21, 0, tzinfo=HK),
                show_start=datetime(2026, 9, 30, 23, 0, tzinfo=HK),
                show_end=datetime(2026, 10, 1, 0, 30, tzinfo=HK),
                load_out_end=datetime(2026, 10, 1, 2, 0, tzinfo=HK),
                capacity=2)
svc.register_program(nanjing, "prog-nj", city="南京", title="昆曲《牡丹亭》",
                     duration_minutes=40, equipment=["灯A", "音响X"],
                     content={"program": "昆曲《牡丹亭》", "headcount": 12})
svc.open_show(organizer, "show-1", slot_id="slot-1", order=["prog-nj"])

svc.confirm_attendance(nanjing, "show-1", "prog-nj",
                       receipt_id="r-1", content={"program": "昆曲《牡丹亭》", "headcount": 12})
claim = svc.hold_resources(stage, "show-1", "prog-nj")   # 原子锁定，冲突则全部不成交
svc.confirm_equipment(stage, claim)                       # 舞台负责人确认设备
svc.commit_cost(organizer, "show-1", "prog-nj", amount=120000, currency="HKD")
svc.freeze_show(organizer, "show-1")                      # 只有主办方可冻结

svc.executable_order("show-1")        # 目前可执行的节目顺序
svc.missing_confirmations("show-1")   # 仍缺的确认及冲突来源
svc.unfinished_changes()              # 未办结的现场变更（值班人员恢复用）
```
