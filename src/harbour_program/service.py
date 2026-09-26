"""演出资源承诺服务。

在基础契约之上管理节目版本、场地窗口、到场确认、设备清单、
合作方权限、替补方案、费用承诺和现场变更，并保证：

- 每个合作城市只能确认自己城市的节目事实；
- 设备状态只能由舞台负责人确认，最终顺序只能由主办方冻结；
- 替补保留被替换内容和原因，已演出记录不可改写为新版本；
- 设备和时段被并行节目共享，锁定要么全部成功要么全部失败；
- 相同到场回执可以重放，内容漂移必须重新确认；
- 跨午夜的装台、演出、撤场按场地本地日期计算；
- 已过时段在服务重启后不得再次释放资源。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import validate_event

ROLE_ORGANIZER = "organizer"
ROLE_STAGE_MANAGER = "stage_manager"
ROLE_PARTNER = "partner"
ROLE_DUTY = "duty"
KNOWN_ROLES = (ROLE_ORGANIZER, ROLE_STAGE_MANAGER, ROLE_PARTNER, ROLE_DUTY)

PHASES = ("setup", "performance", "teardown")

WINDOW_OPEN = "open"
WINDOW_RELEASED = "released"

HOLD_HELD = "held"
HOLD_RELEASED = "released"

RECEIPT_CONFIRMED = "confirmed"
RECEIPT_CONTESTED = "contested"

CHANGE_OPEN = "open"
CHANGE_APPLIED = "applied"
CHANGE_CLOSED = "closed"


class ServiceError(Exception):
    """业务规则冲突，携带稳定代码和中文说明。"""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True)
class Actor:
    """调用方身份：角色决定权限，合作方必须携带城市。"""

    role: str
    city: str | None = None
    name: str = ""


def _coerce_instant(value: datetime | str, field: str = "时间") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ServiceError("validation", f"{field}不是合法的 ISO 时间") from None
    else:
        raise ServiceError("validation", f"{field}必须是带时区的时间")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ServiceError("validation", f"{field}必须携带时区")
    return parsed


def _iso(value: datetime | str) -> str:
    return _coerce_instant(value).isoformat()


def _non_empty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError("validation", f"{field}必须是非空字符串")
    return value.strip()


def _fingerprint(parts: Mapping[str, Any]) -> str:
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _overlaps(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return _coerce_instant(a["start"]) < _coerce_instant(b["end"]) and _coerce_instant(b["start"]) < _coerce_instant(a["end"])


def _new_state() -> dict[str, Any]:
    return {
        "venues": {},
        "sessions": {},
        "windows": {},
        "programs": {},
        "attendance": {},
        "equipment": {},
        "holds": {},
        "substitutions": [],
        "costs": [],
        "changes": {},
        "conflict_audit": [],
        "releases": [],
        "events": [],
        "seq": 0,
    }


class CommitmentService:
    """演出资源承诺服务；状态可持久化到 JSON 文件并在重启后恢复。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path is not None else None
        if self._path is not None and self._path.exists():
            stored = json.loads(self._path.read_text(encoding="utf-8"))
            self._state = _new_state()
            self._state.update(stored.get("state", {}))
            self._state["events"] = stored.get("events", self._state.get("events", []))
        else:
            self._state = _new_state()

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def _save(self) -> None:
        if self._path is None:
            return
        payload = json.dumps(
            {"state": {k: v for k, v in self._state.items() if k != "events"}, "events": self._state["events"]},
            ensure_ascii=False,
            indent=1,
            sort_keys=True,
        )
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, self._path)

    def _next_id(self, prefix: str) -> str:
        self._state["seq"] += 1
        return f"{prefix}-{self._state['seq']:06d}"

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str, version: int, at: datetime | str, payload: Mapping[str, Any], schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "event_id": self._next_id("evt"),
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": _iso(at),
            "version": version,
            "payload": dict(payload),
        }
        if schema is not None:
            issues = validate_event(event, schema)
            if issues:
                raise ServiceError("contract_violation", "事件不符合领域契约", {"issues": [vars(i) for i in issues]})
        self._state["events"].append(event)
        return event

    def events(self) -> list[dict[str, Any]]:
        """返回已发生的领域事件（深拷贝，按发生顺序）。"""
        return json.loads(json.dumps(self._state["events"]))

    @staticmethod
    def _require_role(actor: Actor, *roles: str) -> None:
        if actor.role not in KNOWN_ROLES:
            raise ServiceError("permission_denied", f"未知角色 {actor.role!r}")
        if roles and actor.role not in roles:
            raise ServiceError("permission_denied", "当前角色无权执行该操作", {"required": list(roles), "actual": actor.role})

    @staticmethod
    def _require(mapping: Mapping[str, Any], key: str, what: str) -> Any:
        if key not in mapping:
            raise ServiceError("not_found", f"{what}不存在: {key}")
        return mapping[key]

    def _require_program(self, program_id: str) -> dict[str, Any]:
        return self._require(self._state["programs"], program_id, "节目")

    def _require_session(self, session_id: str) -> dict[str, Any]:
        return self._require(self._state["sessions"], session_id, "场次")

    def _require_window(self, window_id: str) -> dict[str, Any]:
        return self._require(self._state["windows"], window_id, "场地窗口")

    def _require_order_member(self, session: Mapping[str, Any], program_id: str) -> None:
        if program_id not in session["order"]:
            raise ServiceError("validation", f"节目 {program_id} 不在场次 {session['order']} 中")

    def _require_not_frozen(self, session: Mapping[str, Any]) -> None:
        if session["frozen"]:
            raise ServiceError("already_frozen", "最终顺序已冻结，不能再调整")

    def _require_partner_city(self, actor: Actor, program: Mapping[str, Any]) -> None:
        self._require_role(actor, ROLE_PARTNER)
        if actor.city != program["city"]:
            raise ServiceError(
                "permission_denied",
                "合作城市只能确认自己城市的节目事实",
                {"program_city": program["city"], "actor_city": actor.city},
            )

    # ------------------------------------------------------------------
    # 建册：场地、节目、场次、窗口
    # ------------------------------------------------------------------

    def register_venue(self, actor: Actor, venue_id: str, timezone: str) -> dict[str, Any]:
        self._require_role(actor, ROLE_ORGANIZER)
        venue_id = _non_empty(venue_id, "场地标识")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ServiceError("invalid_timezone", f"场地时区无法识别: {timezone}") from None
        if venue_id in self._state["venues"]:
            raise ServiceError("state_conflict", f"场地已登记: {venue_id}")
        venue = {"venue_id": venue_id, "timezone": timezone}
        self._state["venues"][venue_id] = venue
        self._save()
        return dict(venue)

    def register_program(self, actor: Actor, program_id: str, city: str, title: str, summary: str, at: datetime | str) -> dict[str, Any]:
        self._require_role(actor, ROLE_ORGANIZER)
        program_id = _non_empty(program_id, "节目标识")
        city = _non_empty(city, "合作城市")
        if program_id in self._state["programs"]:
            raise ServiceError("state_conflict", f"节目已登记: {program_id}")
        program = {
            "program_id": program_id,
            "city": city,
            "current_revision": 1,
            "revisions": {
                "1": {
                    "title": _non_empty(title, "节目名称"),
                    "summary": summary,
                    "reason": "首次登记",
                    "supersedes": None,
                    "registered_at": _iso(at),
                }
            },
            "performed": False,
            "performed_revision": None,
        }
        self._state["programs"][program_id] = program
        self._save()
        return json.loads(json.dumps(program))

    def open_session(self, actor: Actor, session_id: str, venue_id: str, program_order: Iterable[str]) -> dict[str, Any]:
        self._require_role(actor, ROLE_ORGANIZER)
        session_id = _non_empty(session_id, "场次标识")
        self._require(self._state["venues"], venue_id, "场地")
        order = [_non_empty(pid, "节目标识") for pid in program_order]
        if len(set(order)) != len(order):
            raise ServiceError("validation", "场次顺序中节目重复")
        for pid in order:
            self._require_program(pid)
        if session_id in self._state["sessions"]:
            raise ServiceError("state_conflict", f"场次已存在: {session_id}")
        session = {
            "session_id": session_id,
            "venue_id": venue_id,
            "order": order,
            "frozen": False,
            "frozen_order": None,
            "frozen_at": None,
        }
        self._state["sessions"][session_id] = session
        self._save()
        return json.loads(json.dumps(session))

    def add_window(self, actor: Actor, session_id: str, window_id: str, phase: str, start: datetime | str, end: datetime | str) -> dict[str, Any]:
        self._require_role(actor, ROLE_ORGANIZER)
        session = self._require_session(session_id)
        window_id = _non_empty(window_id, "窗口标识")
        if phase not in PHASES:
            raise ServiceError("validation", f"窗口阶段必须是 {list(PHASES)} 之一")
        start_dt, end_dt = _coerce_instant(start, "开始时间"), _coerce_instant(end, "结束时间")
        if not start_dt < end_dt:
            raise ServiceError("validation", "窗口开始必须早于结束")
        if window_id in self._state["windows"]:
            raise ServiceError("state_conflict", f"窗口已存在: {window_id}")
        window = {
            "window_id": window_id,
            "session_id": session["session_id"],
            "venue_id": session["venue_id"],
            "phase": phase,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "status": WINDOW_OPEN,
        }
        self._state["windows"][window_id] = window
        self._save()
        return dict(window)

    def adjust_window(self, actor: Actor, window_id: str, new_end: datetime | str, at: datetime | str) -> dict[str, Any]:
        """时段压缩：只能由主办方缩短尚未释放的窗口。"""
        self._require_role(actor, ROLE_ORGANIZER)
        window = self._require_window(window_id)
        if window["status"] != WINDOW_OPEN:
            raise ServiceError("window_closed", "时段已释放，不能再调整")
        end_dt = _coerce_instant(new_end, "结束时间")
        if not _coerce_instant(window["start"]) < end_dt:
            raise ServiceError("validation", "结束时间必须晚于开始时间")
        if end_dt > _coerce_instant(window["end"]):
            raise ServiceError("validation", "只允许压缩时段，不允许延长")
        window["end"] = end_dt.isoformat()
        window["adjusted_at"] = _iso(at)
        self._save()
        return dict(window)

    def window_local_date(self, window_id: str) -> str:
        """窗口所属日期按场地本地时区计算，跨午夜不拆天。"""
        window = self._require_window(window_id)
        venue = self._require(self._state["venues"], window["venue_id"], "场地")
        return _coerce_instant(window["start"]).astimezone(ZoneInfo(venue["timezone"])).date().isoformat()

    # ------------------------------------------------------------------
    # 节目版本与替补
    # ------------------------------------------------------------------

    def revise_program(self, actor: Actor, program_id: str, title: str, summary: str, reason: str, at: datetime | str) -> dict[str, Any]:
        program = self._require_program(program_id)
        if actor.role == ROLE_PARTNER:
            self._require_partner_city(actor, program)
        else:
            self._require_role(actor, ROLE_ORGANIZER)
        if program["performed"]:
            raise ServiceError("performed_immutable", "已演出记录不可改写为新版本")
        reason = _non_empty(reason, "变更原因")
        previous = program["current_revision"]
        revision = previous + 1
        program["revisions"][str(revision)] = {
            "title": _non_empty(title, "节目名称"),
            "summary": summary,
            "reason": reason,
            "supersedes": previous,
            "registered_at": _iso(at),
        }
        program["current_revision"] = revision
        self._emit(
            "PROGRAM_CHANGED",
            "program_revision",
            program_id,
            revision,
            at,
            {"supersedes": str(previous), "reason": reason, "title": program["revisions"][str(revision)]["title"]},
        )
        self._save()
        return {"program_id": program_id, "revision": revision, "supersedes": previous}

    def substitute(self, actor: Actor, session_id: str, out_program_id: str, in_program_id: str, reason: str, at: datetime | str) -> dict[str, Any]:
        """替补：保留被替换节目的当前内容快照和原因，顺序原位替换。"""
        self._require_role(actor, ROLE_ORGANIZER)
        session = self._require_session(session_id)
        self._require_not_frozen(session)
        out_program = self._require_program(out_program_id)
        in_program = self._require_program(in_program_id)
        self._require_order_member(session, out_program_id)
        if in_program_id in session["order"]:
            raise ServiceError("state_conflict", f"节目 {in_program_id} 已在场次顺序中")
        reason = _non_empty(reason, "替补原因")
        out_revision = str(out_program["current_revision"])
        record = {
            "substitution_id": self._next_id("sub"),
            "session_id": session_id,
            "out_program_id": out_program_id,
            "out_revision": out_program["current_revision"],
            "out_snapshot": dict(out_program["revisions"][out_revision]),
            "in_program_id": in_program_id,
            "in_revision": in_program["current_revision"],
            "reason": reason,
            "at": _iso(at),
        }
        session["order"][session["order"].index(out_program_id)] = in_program_id
        self._state["substitutions"].append(record)
        self._save()
        return json.loads(json.dumps(record))

    def record_performance(self, actor: Actor, program_id: str, at: datetime | str) -> dict[str, Any]:
        """登记已演出记录；此后该节目不可再改版。"""
        self._require_role(actor, ROLE_ORGANIZER, ROLE_STAGE_MANAGER)
        program = self._require_program(program_id)
        if program["performed"]:
            raise ServiceError("state_conflict", "节目已有演出记录")
        program["performed"] = True
        program["performed_revision"] = program["current_revision"]
        program["performed_at"] = _iso(at)
        self._save()
        return {"program_id": program_id, "performed_revision": program["performed_revision"]}

    # ------------------------------------------------------------------
    # 到场确认：幂等重放与内容漂移
    # ------------------------------------------------------------------

    def confirm_attendance(self, actor: Actor, receipt_id: str, program_id: str, revision: int, headcount: int, note: str, at: datetime | str) -> dict[str, Any]:
        program = self._require_program(program_id)
        self._require_partner_city(actor, program)
        receipt_id = _non_empty(receipt_id, "回执标识")
        if isinstance(revision, bool) or not isinstance(revision, int) or str(revision) not in program["revisions"]:
            raise ServiceError("validation", f"节目 {program_id} 不存在版本 {revision}")
        if isinstance(headcount, bool) or not isinstance(headcount, int) or headcount < 1:
            raise ServiceError("validation", "到场人数必须是正整数")
        fingerprint = _fingerprint({"program_id": program_id, "revision": revision, "headcount": headcount, "note": note})
        existing = self._state["attendance"].get(receipt_id)
        if existing is not None:
            if existing["fingerprint"] == fingerprint and existing["status"] == RECEIPT_CONFIRMED:
                return {"replayed": True, "receipt": dict(existing)}
            existing["status"] = RECEIPT_CONTESTED
            self._state["conflict_audit"].append(
                {
                    "conflict_id": self._next_id("con"),
                    "kind": "attendance_drift",
                    "program_id": program_id,
                    "revision": existing["revision"],
                    "source": receipt_id,
                    "detail": "同一回执标识携带了不同内容，需重新确认",
                    "at": _iso(at),
                }
            )
            self._save()
            raise ServiceError("drift_detected", "回执内容漂移，需用新回执重新确认", {"receipt_id": receipt_id})
        receipt = {
            "receipt_id": receipt_id,
            "program_id": program_id,
            "revision": revision,
            "city": program["city"],
            "headcount": headcount,
            "note": note,
            "status": RECEIPT_CONFIRMED,
            "fingerprint": fingerprint,
            "confirmed_by": actor.name or actor.role,
            "occurred_at": _iso(at),
        }
        self._state["attendance"][receipt_id] = receipt
        self._emit(
            "ATTENDANCE_CONFIRMED",
            "program_revision",
            f"{program_id}@{revision}",
            revision,
            at,
            {"receipt_id": receipt_id, "city": program["city"], "headcount": headcount},
        )
        self._save()
        return {"replayed": False, "receipt": dict(receipt)}

    # ------------------------------------------------------------------
    # 设备清单、原子锁定与舞台确认
    # ------------------------------------------------------------------

    def declare_equipment(self, actor: Actor, program_id: str, items: Iterable[str]) -> dict[str, Any]:
        program = self._require_program(program_id)
        if actor.role == ROLE_PARTNER:
            self._require_partner_city(actor, program)
        else:
            self._require_role(actor, ROLE_ORGANIZER)
        seen: list[str] = []
        for item in items:
            item = _non_empty(item, "设备名称")
            if item not in seen:
                seen.append(item)
        record = self._state["equipment"].setdefault(
            program_id,
            {"program_id": program_id, "items": [], "confirmed_revision": None, "confirmed_by": None, "confirmed_at": None},
        )
        if record["items"] != seen:
            # 清单内容漂移：既有舞台确认失效，需重新确认。
            record["items"] = seen
            record["confirmed_revision"] = None
            record["confirmed_by"] = None
            record["confirmed_at"] = None
        self._save()
        return dict(record)

    def confirm_equipment(self, actor: Actor, program_id: str, at: datetime | str) -> dict[str, Any]:
        self._require_role(actor, ROLE_STAGE_MANAGER)
        program = self._require_program(program_id)
        record = self._state["equipment"].get(program_id)
        if record is None:
            raise ServiceError("validation", "尚未申报设备清单")
        record["confirmed_revision"] = program["current_revision"]
        record["confirmed_by"] = actor.name or actor.role
        record["confirmed_at"] = _iso(at)
        self._save()
        return dict(record)

    def lock_resources(self, actor: Actor, session_id: str, window_id: str, program_id: str, resources: Iterable[str], now: datetime | str) -> dict[str, Any]:
        """原子锁定：任一资源冲突则整批失败，不产生部分锁定。"""
        self._require_role(actor, ROLE_ORGANIZER, ROLE_STAGE_MANAGER)
        session = self._require_session(session_id)
        window = self._require_window(window_id)
        self._require_program(program_id)
        self._require_order_member(session, program_id)
        if window["session_id"] != session_id:
            raise ServiceError("validation", "窗口不属于该场次")
        now_dt = _coerce_instant(now, "当前时间")
        if window["status"] != WINDOW_OPEN or _coerce_instant(window["end"]) <= now_dt:
            raise ServiceError("window_closed", "时段已过或已释放，不能再锁定资源")
        wanted: list[str] = []
        for res in resources:
            res = _non_empty(res, "资源标识")
            if res not in wanted:
                wanted.append(res)
        if not wanted:
            raise ServiceError("validation", "锁定清单不能为空")
        held_by_self: set[str] = set()
        conflicts: list[dict[str, Any]] = []
        for hold in self._state["holds"].values():
            if hold["status"] != HOLD_HELD:
                continue
            other_window = self._state["windows"][hold["window_id"]]
            if other_window["venue_id"] != window["venue_id"] or not _overlaps(window, other_window):
                continue
            shared = [res for res in wanted if res in hold["resources"]]
            if not shared:
                continue
            if hold["program_id"] == program_id:
                held_by_self.update(shared)
            else:
                for res in shared:
                    conflicts.append(
                        {
                            "resource": res,
                            "held_by_program": hold["program_id"],
                            "held_window_id": hold["window_id"],
                            "hold_id": hold["hold_id"],
                        }
                    )
        if conflicts:
            self._state["conflict_audit"].append(
                {
                    "conflict_id": self._next_id("con"),
                    "kind": "resource_conflict",
                    "session_id": session_id,
                    "program_id": program_id,
                    "source": [c["held_by_program"] for c in conflicts],
                    "detail": conflicts,
                    "at": now_dt.isoformat(),
                }
            )
            self._save()
            raise ServiceError("resource_conflict", "设备或时段被其他节目占用，整批锁定失败", {"conflicts": conflicts})
        todo = [res for res in wanted if res not in held_by_self]
        if not todo:
            return {"replayed": True, "hold_id": None, "resources": sorted(held_by_self)}
        hold = {
            "hold_id": self._next_id("hold"),
            "session_id": session_id,
            "window_id": window_id,
            "program_id": program_id,
            "resources": todo,
            "status": HOLD_HELD,
        }
        self._state["holds"][hold["hold_id"]] = hold
        for index, res in enumerate(todo, start=1):
            self._emit(
                "RESOURCE_HELD",
                "resource_claim",
                hold["hold_id"],
                index,
                now_dt,
                {"resource_ref": res, "slot": window_id, "session_id": session_id, "program_id": program_id},
            )
        self._save()
        return {"replayed": False, "hold_id": hold["hold_id"], "resources": list(todo)}

    def sweep_expired(self, now: datetime | str) -> list[str]:
        """释放已过时段及其资源；只处理仍处于开放状态的窗口，重启后重放安全。"""
        now_dt = _coerce_instant(now, "当前时间")
        released: list[str] = []
        for window in self._state["windows"].values():
            if window["status"] != WINDOW_OPEN or _coerce_instant(window["end"]) > now_dt:
                continue
            window["status"] = WINDOW_RELEASED
            window["released_at"] = now_dt.isoformat()
            for hold in self._state["holds"].values():
                if hold["window_id"] == window["window_id"] and hold["status"] == HOLD_HELD:
                    hold["status"] = HOLD_RELEASED
            self._state["releases"].append({"window_id": window["window_id"], "at": now_dt.isoformat()})
            released.append(window["window_id"])
        if released:
            self._save()
        return released

    # ------------------------------------------------------------------
    # 费用承诺
    # ------------------------------------------------------------------

    def commit_cost(self, actor: Actor, session_id: str, program_id: str, amount: int | float, currency: str, note: str, at: datetime | str) -> dict[str, Any]:
        session = self._require_session(session_id)
        program = self._require_program(program_id)
        if actor.role == ROLE_PARTNER:
            self._require_partner_city(actor, program)
        else:
            self._require_role(actor, ROLE_ORGANIZER)
        self._require_order_member(session, program_id)
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            raise ServiceError("validation", "费用金额必须是正数")
        record = {
            "cost_id": self._next_id("cost"),
            "session_id": session_id,
            "program_id": program_id,
            "amount": amount,
            "currency": _non_empty(currency, "币种"),
            "note": note,
            "committed_by": actor.name or actor.role,
            "at": _iso(at),
        }
        self._state["costs"].append(record)
        self._save()
        return dict(record)

    # ------------------------------------------------------------------
    # 冻结与现场变更
    # ------------------------------------------------------------------

    def freeze_order(self, actor: Actor, session_id: str, at: datetime | str) -> dict[str, Any]:
        self._require_role(actor, ROLE_ORGANIZER)
        session = self._require_session(session_id)
        self._require_not_frozen(session)
        snapshot = [
            {"program_id": pid, "revision": self._require_program(pid)["current_revision"]} for pid in session["order"]
        ]
        session["frozen"] = True
        session["frozen_order"] = snapshot
        session["frozen_at"] = _iso(at)
        self._emit(
            "SHOW_FROZEN",
            "show_record",
            f"{session_id}-order",
            1,
            at,
            {"revision": 1, "frozen_at": _iso(at), "order": snapshot},
        )
        self._save()
        return json.loads(json.dumps(snapshot))

    def open_change(self, actor: Actor, session_id: str, kind: str, detail: str, at: datetime | str) -> dict[str, Any]:
        self._require_role(actor)
        self._require_session(session_id)
        record = {
            "change_id": self._next_id("chg"),
            "session_id": session_id,
            "kind": _non_empty(kind, "变更类型"),
            "detail": _non_empty(detail, "变更说明"),
            "status": CHANGE_OPEN,
            "opened_by": actor.name or actor.role,
            "opened_at": _iso(at),
            "resolved_by": None,
            "resolved_at": None,
        }
        self._state["changes"][record["change_id"]] = record
        self._save()
        return dict(record)

    def _resolve_change(self, actor: Actor, change_id: str, status: str, at: datetime | str) -> dict[str, Any]:
        self._require_role(actor, ROLE_ORGANIZER, ROLE_DUTY)
        record = self._require(self._state["changes"], change_id, "现场变更")
        if record["status"] != CHANGE_OPEN:
            raise ServiceError("state_conflict", "变更已办结，不能重复处理")
        record["status"] = status
        record["resolved_by"] = actor.name or actor.role
        record["resolved_at"] = _iso(at)
        self._emit(
            "CHANGE_REVIEWED",
            "show_record",
            change_id,
            1,
            at,
            {"decision": status, "kind": record["kind"], "session_id": record["session_id"]},
        )
        self._save()
        return dict(record)

    def apply_change(self, actor: Actor, change_id: str, at: datetime | str) -> dict[str, Any]:
        return self._resolve_change(actor, change_id, CHANGE_APPLIED, at)

    def close_change(self, actor: Actor, change_id: str, at: datetime | str) -> dict[str, Any]:
        return self._resolve_change(actor, change_id, CHANGE_CLOSED, at)

    def pending_changes(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """未办结的现场变更，值班人员重启后可以继续处理。"""
        records = [
            dict(c)
            for c in self._state["changes"].values()
            if c["status"] == CHANGE_OPEN and (session_id is None or c["session_id"] == session_id)
        ]
        return sorted(records, key=lambda c: c["change_id"])

    # ------------------------------------------------------------------
    # 场次看板
    # ------------------------------------------------------------------

    def session_board(self, session_id: str, now: datetime | str) -> dict[str, Any]:
        """回答：目前可执行的节目顺序、仍缺的确认及其冲突来源。"""
        session = self._require_session(session_id)
        now_dt = _coerce_instant(now, "当前时间")
        programs = self._state["programs"]
        if session["frozen"]:
            entries_source = list(session["frozen_order"])
        else:
            entries_source = [
                {"program_id": pid, "revision": programs[pid]["current_revision"]} for pid in session["order"]
            ]
        windows = [w for w in self._state["windows"].values() if w["session_id"] == session_id]
        performance_windows = [w for w in windows if w["phase"] == "performance"]
        live_windows = [
            w for w in performance_windows if w["status"] == WINDOW_OPEN and _coerce_instant(w["end"]) > now_dt
        ]
        entries: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        executable: list[dict[str, Any]] = []
        for position, source in enumerate(entries_source):
            pid, revision = source["program_id"], source["revision"]
            program = programs[pid]
            blockers: list[dict[str, Any]] = []
            receipts = [
                r
                for r in self._state["attendance"].values()
                if r["program_id"] == pid and r["revision"] == revision
            ]
            if not any(r["status"] == RECEIPT_CONFIRMED for r in receipts):
                contested = [r for r in receipts if r["status"] == RECEIPT_CONTESTED]
                detail = "到场回执内容漂移，需重新确认" if contested else f"等待{program['city']}确认到场"
                blockers.append({"kind": "attendance", "detail": detail})
                missing.append({"program_id": pid, "kind": "attendance", "revision": revision, "detail": detail})
                for r in contested:
                    conflicts.append(
                        {
                            "kind": "attendance_drift",
                            "program_id": pid,
                            "revision": revision,
                            "source": r["receipt_id"],
                            "detail": "同一回执标识携带了不同内容",
                        }
                    )
            equipment = self._state["equipment"].get(pid)
            if equipment is not None and equipment["confirmed_revision"] != revision:
                detail = "设备状态未经舞台负责人确认" if equipment["confirmed_revision"] is None else "节目改版后设备状态未重新确认"
                blockers.append({"kind": "equipment", "detail": detail})
                missing.append({"program_id": pid, "kind": "equipment", "revision": revision, "detail": detail})
            if equipment is not None:
                held = {
                    res
                    for hold in self._state["holds"].values()
                    if hold["status"] == HOLD_HELD
                    and hold["program_id"] == pid
                    and self._state["windows"][hold["window_id"]]["session_id"] == session_id
                    and self._state["windows"][hold["window_id"]]["status"] == WINDOW_OPEN
                    and _coerce_instant(self._state["windows"][hold["window_id"]]["end"]) > now_dt
                    for res in hold["resources"]
                }
                for res in equipment["items"]:
                    if res in held:
                        continue
                    holders = [
                        hold
                        for hold in self._state["holds"].values()
                        if hold["status"] == HOLD_HELD
                        and res in hold["resources"]
                        and hold["program_id"] != pid
                        and any(_overlaps(w, self._state["windows"][hold["window_id"]]) for w in live_windows)
                    ]
                    detail = f"设备 {res} 尚未锁定"
                    blockers.append({"kind": "resource", "detail": detail})
                    if holders:
                        conflicts.append(
                            {
                                "kind": "resource",
                                "program_id": pid,
                                "resource": res,
                                "source": holders[0]["program_id"],
                                "detail": f"设备 {res} 被节目 {holders[0]['program_id']} 占用",
                            }
                        )
            if not performance_windows:
                blockers.append({"kind": "window", "detail": "未排定演出时段"})
            elif not live_windows:
                blockers.append({"kind": "window", "detail": "演出时段已过"})
            entry = {
                "position": position,
                "program_id": pid,
                "revision": revision,
                "city": program["city"],
                "executable": not blockers,
                "blockers": blockers,
            }
            entries.append(entry)
            if not blockers:
                executable.append({"position": position, "program_id": pid, "revision": revision})
        local_date = None
        if performance_windows:
            venue = self._state["venues"][session["venue_id"]]
            local_date = min(
                _coerce_instant(w["start"]).astimezone(ZoneInfo(venue["timezone"])).date() for w in performance_windows
            ).isoformat()
        return {
            "session_id": session_id,
            "venue_id": session["venue_id"],
            "local_date": local_date,
            "frozen": session["frozen"],
            "entries": entries,
            "executable_order": executable,
            "missing_confirmations": missing,
            "conflicts": conflicts,
            "pending_changes": self.pending_changes(session_id),
            "substitutions": [dict(s) for s in self._state["substitutions"] if s["session_id"] == session_id],
            "costs": [dict(c) for c in self._state["costs"] if c["session_id"] == session_id],
        }

    def conflict_log(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """冲突审计：漂移与锁定失败的历史记录。"""
        records = []
        for record in self._state["conflict_audit"]:
            if session_id is None:
                records.append(dict(record))
                continue
            if record.get("session_id") == session_id:
                records.append(dict(record))
                continue
            program_id = record.get("program_id")
            if program_id and any(
                program_id in s["order"] or any(e["program_id"] == program_id for e in s["frozen_order"] or [])
                for s in self._state["sessions"].values()
                if s["session_id"] == session_id
            ):
                records.append(dict(record))
        return records
