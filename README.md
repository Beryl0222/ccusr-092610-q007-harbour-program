# 香港节庆节目资源锁定账

2026“天涯共此时”中秋活动启动，包含江苏文旅推介和多地协作；相关节庆项目跨境巡演和场地资源需衔接多方安排。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/harbour_program/`：基础契约校验、演出资源承诺服务与命令行入口。
- `tests/`：信封、时间、版本、事件载荷与服务层规则测试。
- `docs/domain.md`：领域对象、事件语义与服务层规则。

## 演出资源承诺服务

`harbour_program.service.CommitmentService` 管理节目版本、场地窗口、到场确认、设备清单、合作方权限、替补方案、费用承诺和现场变更：合作城市只确认自己的节目事实，舞台负责人确认设备状态，主办方冻结最终顺序；设备与时段原子锁定，回执幂等重放、漂移重新确认，跨午夜窗口按场地本地日期归天，已过时段重启后不再释放。`session_board` 给出指定场次可执行的节目顺序、仍缺确认与冲突来源，`pending_changes` 供值班人员恢复未办结变更。状态可持久化到 JSON 文件并在重启后恢复。

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
