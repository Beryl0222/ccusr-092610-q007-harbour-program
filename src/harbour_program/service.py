"""演出资源承诺服务。

在领域契约（``contracts.py``）之上实现业务规则：

- 角色权限：合作城市只能确认自己城市的节目事实，设备状态由舞台负责人确认，
  只有主办方可以冻结最终节目顺序，值班人员恢复未办结的现场变更。
- 节目版本与替补：每次变更产生新版本并保留被替换内容与原因；
  已演出记录不可再改成新版本。
- 资源锁定：设备与时段由并行节目共享，一次锁定要么全部成交要么全部不成交。
- 到场确认：相同回执可重放；内容漂移作废旧确认并要求重新确认。
- 场地窗口：跨午夜的装台、演出、撤场按场地本地日期归属演出日。
- 重启恢复：状态由事件日志重建；已过时段的锁定只结算一次，
  不会因服务重启再次释放资源。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import validate_event
from .journal import Journal

_DEFAULT_SCHEMA = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"

_ACTIVE_CLAIM_STATES = ("held", "confirmed")
_UNFINISHED_CHANGE_STATES = ("open", "approved", "in_progress")


class Role(str, Enum):
    """操作者角色。"""

    ORGANIZER = "organizer"  # 主办方
    PARTNER_CITY = "partner_city"  # 合作城市
    STAGE_MANAGER = "stage_manager"  # 舞台负责人
    DUTY_STAFF = "duty_staff"  # 值班人员


@dataclass(frozen=True)
class Actor:
    """一次操作的发起人；合作城市必须携带城市名。"""

    role: Role
    city: str | None = None
    name: str = ""


class ServiceError(Exception):
    """服务层业务错误基类。"""


class PermissionDenied(ServiceError):
    """角色或城市权限不足。"""


class NotFound(ServiceError):
    """引用的对象不存在。"""


class InvalidState(ServiceError):
    """当前状态不允许该操作。"""


class ConflictError(ServiceError):
    """资源锁定冲突；conflicts 逐项列出冲突来源。"""

    def __init__(self, message: str, conflicts: Sequence[Mapping[str, Any]]):
        super().__init__(message)
        self.conflicts = [dict(item) for item in conflicts]


class ContentDrift(ServiceError):
    """回执内容与已确认事实不一致，需重新确认。"""


class FreezeNotReady(ServiceError):
    """冻结前提未满足；missing 为仍缺的确认清单。"""

    def __init__(self, missing: Sequence["MissingConfirmation"]):
        super().__init__("场次仍缺确认，不能冻结最终顺序")
        self.missing = list(missing)


@dataclass
class ProgramRevision:
    """节目的一个版本；旧版本永久保留，替补内容可追溯。"""

    program_id: str
    revision: int
    city: str
    title: str
    duration_minutes: int
    equipment: tuple[str, ...]
    content: dict[str, Any]
    content_hash: str
    reason: str
    supersedes: int | None
    recorded_at: datetime


@dataclass
class VenueSlot:
    """场地窗口：装台、演出、撤场四个时刻与并行容量。"""

    slot_id: str
    venue: str
    tz: str
    load_in_start: datetime
    show_start: datetime
    show_end: datetime
    load_out_end: datetime
    capacity: int

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def performance_date(self) -> date:
        """演出日按场地本地日期计算；跨午夜的撤场不改变归属。"""
        return self.show_start.astimezone(self.zone).date()

    def elapsed(self, now: datetime) -> bool:
        return now >= self.load_out_end


@dataclass
class ResourceClaim:
    """一次原子锁定：某节目在某时段对一组设备的占用。"""

    claim_id: str
    show_id: str
    program_id: str
    slot_id: str
    equipment: tuple[str, ...]
    state: str = "held"  # held -> confirmed -> released / settled
    equipment_confirmed_by: str | None = None
    release_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.state in _ACTIVE_CLAIM_STATES


@dataclass
class AttendanceState:
    """节目当前有效的到场确认。"""

    program_id: str
    revision: int
    content_hash: str
    receipt_id: str
    city: str
    confirmed_at: datetime


@dataclass
class CostCommitment:
    """主办方对某节目的一笔费用承诺（金额为最小货币单位）。"""

    show_id: str
    program_id: str
    amount: int
    currency: str
    note: str
    committed_at: datetime


@dataclass
class PerformedEntry:
    """已演出记录，写入后不可修改。"""

    program_id: str
    revision: int
    performed_at: datetime
    note: str


@dataclass
class ChangeRecord:
    """现场变更：open -> approved/rejected -> in_progress -> closed。"""

    change_id: str
    show_id: str
    description: str
    details: dict[str, Any]
    opened_by: str
    opened_at: datetime
    status: str = "open"
    reviewed_by: str | None = None
    decision: str | None = None
    resumed_by: str | None = None
    closed_by: str | None = None
    outcome: str | None = None

    @property
    def unfinished(self) -> bool:
        return self.status in _UNFINISHED_CHANGE_STATES


@dataclass
class ShowRecord:
    """场次：某场地窗口内的一份节目顺序及其冻结、演出状态。"""

    show_id: str
    slot_id: str
    order: list[str]
    order_revision: int = 1
    frozen: bool = False
    frozen_revision: int | None = None
    frozen_at: datetime | None = None
    frozen_order: list[tuple[str, int]] | None = None
    performed: dict[str, PerformedEntry] = field(default_factory=dict)


@dataclass(frozen=True)
class MissingConfirmation:
    """仍缺的确认；conflicts 给出冲突来源。"""

    program_id: str
    kind: str
    detail: str
    conflicts: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ProgramReadiness:
    """顺序中一个节目的可执行状态。"""

    program_id: str
    revision: int
    title: str
    ready: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class ShowOrderView:
    """指定场次目前可执行的节目顺序。"""

    show_id: str
    slot_id: str
    performance_date: date
    frozen: bool
    slot_elapsed: bool
    executable: bool
    entries: tuple[ProgramReadiness, ...]


def _content_hash(content: Mapping[str, Any]) -> str:
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse_time(value: str, field_name: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} 必须携带时区")
    return parsed


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须携带时区")


class CommitmentService:
    """演出资源承诺服务。

    所有状态变更先写入事件日志再应用到内存；重启后重放日志恢复，
    并对已过时段的活跃锁定做一次（且仅一次）结算。
    """

    def __init__(
        self,
        journal_path: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
        schema: Mapping[str, Any] | None = None,
    ):
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._schema = schema if schema is not None else json.loads(_DEFAULT_SCHEMA.read_text(encoding="utf-8"))
        self._journal = Journal(journal_path)
        self._lock = threading.RLock()
        self._programs: dict[str, dict[int, ProgramRevision]] = {}
        self._slots: dict[str, VenueSlot] = {}
        self._shows: dict[str, ShowRecord] = {}
        self._claims: dict[str, ResourceClaim] = {}
        self._attendance: dict[str, AttendanceState] = {}
        self._receipts: dict[str, str] = {}
        self._drifted: set[str] = set()
        self._costs: list[CostCommitment] = []
        self._changes: dict[str, ChangeRecord] = {}
        self._versions: dict[tuple[str, str], int] = {}
        for event in self._journal.read_all():
            self._apply(event)
        self.settle_elapsed()

    # ------------------------------------------------------------------
    # 事件写入与重放
    # ------------------------------------------------------------------

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        *,
        event_id: str | None = None,
    ) -> str:
        """校验契约、写日志、应用状态；相同事件标识重放为无操作。"""
        with self._lock:
            eid = event_id or f"evt-{uuid.uuid4().hex[:12]}"
            if self._journal.contains(eid):
                return eid
            key = (aggregate_type, aggregate_id)
            event = {
                "event_id": eid,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "occurred_at": self._now().isoformat(),
                "version": self._versions.get(key, 0) + 1,
                "payload": dict(payload),
            }
            issues = validate_event(event, self._schema)
            if issues:
                raise ServiceError("事件未通过契约校验: " + "; ".join(f"{i.field}:{i.code}" for i in issues))
            self._journal.append(event)
            self._apply(event)
            return eid

    def _apply(self, event: Mapping[str, Any]) -> None:
        handler = getattr(self, f"_on_{event['event_type'].lower()}", None)
        if handler is not None:
            handler(event["aggregate_id"], event["payload"], _parse_time(event["occurred_at"], "occurred_at"))
        key = (event["aggregate_type"], event["aggregate_id"])
        self._versions[key] = max(self._versions.get(key, 0), int(event["version"]))

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------

    @staticmethod
    def _require(actor: Actor, *roles: Role) -> None:
        if actor.role not in roles:
            names = "、".join(role.value for role in roles)
            raise PermissionDenied(f"角色 {actor.role.value} 无权执行，需要: {names}")

    @staticmethod
    def _require_own_city(actor: Actor, city: str) -> None:
        if actor.role is Role.PARTNER_CITY and actor.city == city:
            return
        raise PermissionDenied("每个合作城市只可确认自己的节目事实")

    @staticmethod
    def _require_program_owner(actor: Actor, city: str) -> None:
        if actor.role is Role.ORGANIZER:
            return
        if actor.role is Role.PARTNER_CITY and actor.city == city:
            return
        raise PermissionDenied("只有主办方或节目所属城市可以变更节目")

    # ------------------------------------------------------------------
    # 查找辅助
    # ------------------------------------------------------------------

    def _slot(self, slot_id: str) -> VenueSlot:
        try:
            return self._slots[slot_id]
        except KeyError:
            raise NotFound(f"场地窗口不存在: {slot_id}") from None

    def _show(self, show_id: str) -> ShowRecord:
        try:
            return self._shows[show_id]
        except KeyError:
            raise NotFound(f"场次不存在: {show_id}") from None

    def _current_revision(self, program_id: str) -> ProgramRevision:
        revisions = self._programs.get(program_id)
        if not revisions:
            raise NotFound(f"节目不存在: {program_id}")
        return revisions[max(revisions)]

    def _claim(self, claim_id: str) -> ResourceClaim:
        try:
            return self._claims[claim_id]
        except KeyError:
            raise NotFound(f"资源锁定不存在: {claim_id}") from None

    def _change(self, change_id: str) -> ChangeRecord:
        try:
            return self._changes[change_id]
        except KeyError:
            raise NotFound(f"现场变更不存在: {change_id}") from None

    # ------------------------------------------------------------------
    # 场地窗口与场次
    # ------------------------------------------------------------------

    def define_slot(
        self,
        actor: Actor,
        slot_id: str,
        *,
        venue: str,
        tz: str,
        load_in_start: datetime,
        show_start: datetime,
        show_end: datetime,
        load_out_end: datetime,
        capacity: int = 1,
        event_id: str | None = None,
    ) -> str:
        """登记场地窗口；装台、演出、撤场可跨午夜，演出日按场地本地日期。"""
        self._require(actor, Role.ORGANIZER)
        for name, value in (
            ("load_in_start", load_in_start),
            ("show_start", show_start),
            ("show_end", show_end),
            ("load_out_end", load_out_end),
        ):
            _require_aware(value, name)
        if not (load_in_start <= show_start < show_end <= load_out_end):
            raise ValueError("时刻顺序必须满足 装台 <= 演出开始 < 演出结束 <= 撤场结束")
        if capacity < 1:
            raise ValueError("并行容量至少为 1")
        ZoneInfo(tz)  # 非法时区在此抛出
        with self._lock:
            if slot_id in self._slots:
                raise InvalidState(f"场地窗口已存在: {slot_id}")
            self._emit(
                "SLOT_DEFINED",
                "venue_slot",
                slot_id,
                {
                    "venue": venue,
                    "tz": tz,
                    "load_in_start": load_in_start.isoformat(),
                    "show_start": show_start.isoformat(),
                    "show_end": show_end.isoformat(),
                    "load_out_end": load_out_end.isoformat(),
                    "capacity": capacity,
                },
                event_id=event_id,
            )
            return slot_id

    def open_show(
        self,
        actor: Actor,
        show_id: str,
        *,
        slot_id: str,
        order: Sequence[str],
        event_id: str | None = None,
    ) -> str:
        """开立场次并给出初始节目顺序。"""
        self._require(actor, Role.ORGANIZER)
        with self._lock:
            self._slot(slot_id)
            if show_id in self._shows:
                raise InvalidState(f"场次已存在: {show_id}")
            for program_id in order:
                self._current_revision(program_id)
            self._emit(
                "SHOW_OPENED",
                "show_record",
                show_id,
                {"slot_id": slot_id, "order": list(order)},
                event_id=event_id,
            )
            return show_id

    # ------------------------------------------------------------------
    # 节目版本与替补
    # ------------------------------------------------------------------

    def register_program(
        self,
        actor: Actor,
        program_id: str,
        *,
        city: str,
        title: str,
        duration_minutes: int,
        equipment: Sequence[str] = (),
        content: Mapping[str, Any] | None = None,
        event_id: str | None = None,
    ) -> str:
        """登记节目首个版本。"""
        self._require(actor, Role.ORGANIZER, Role.PARTNER_CITY)
        self._require_program_owner(actor, city)
        if duration_minutes < 1:
            raise ValueError("时长必须为正整数分钟")
        body = dict(content or {})
        with self._lock:
            if program_id in self._programs:
                raise InvalidState(f"节目已存在: {program_id}，请用 substitute_program 变更")
            self._emit(
                "PROGRAM_REGISTERED",
                "program_revision",
                program_id,
                {
                    "city": city,
                    "title": title,
                    "duration_minutes": duration_minutes,
                    "equipment": sorted(set(equipment)),
                    "content": body,
                    "content_hash": _content_hash(body),
                    "revision": 1,
                },
                event_id=event_id,
            )
            return program_id

    def substitute_program(
        self,
        actor: Actor,
        show_id: str,
        program_id: str,
        *,
        title: str,
        duration_minutes: int,
        equipment: Sequence[str] = (),
        content: Mapping[str, Any] | None = None,
        reason: str,
        change_id: str | None = None,
        event_id: str | None = None,
    ) -> int:
        """以新版本替换节目内容；被替换内容与原因永久保留。

        已冻结场次的替换必须引用一笔已批准的现场变更；
        已演出记录不能改成新版本。
        """
        if not reason or not reason.strip():
            raise ValueError("替补必须给出原因")
        with self._lock:
            show = self._show(show_id)
            if program_id not in show.order:
                raise NotFound(f"节目不在该场次: {program_id}")
            current = self._current_revision(program_id)
            self._require_program_owner(actor, current.city)
            if program_id in show.performed:
                raise InvalidState("已演出记录不能改成新版本")
            if show.frozen:
                change = self._changes.get(change_id or "")
                if change is None or change.show_id != show_id or change.status not in ("approved", "in_progress"):
                    raise PermissionDenied("场次已冻结，替换须引用已批准的现场变更")
            body = dict(content or {})
            new_revision = current.revision + 1
            self._emit(
                "PROGRAM_CHANGED",
                "program_revision",
                program_id,
                {
                    "show_id": show_id,
                    "supersedes": current.revision,
                    "reason": reason,
                    "revision": new_revision,
                    "city": current.city,
                    "title": title,
                    "duration_minutes": duration_minutes,
                    "equipment": sorted(set(equipment)),
                    "content": body,
                    "content_hash": _content_hash(body),
                },
                event_id=event_id,
            )
            for claim in list(self._claims.values()):
                if claim.active and claim.show_id == show_id and claim.program_id == program_id:
                    self._emit(
                        "RESOURCE_RELEASED",
                        "resource_claim",
                        claim.claim_id,
                        {"reason": "program_changed", "resource_ref": claim.claim_id, "slot": claim.slot_id},
                    )
            return new_revision

    # ------------------------------------------------------------------
    # 到场确认
    # ------------------------------------------------------------------

    def confirm_attendance(
        self,
        actor: Actor,
        show_id: str,
        program_id: str,
        *,
        receipt_id: str,
        content: Mapping[str, Any],
        event_id: str | None = None,
    ) -> AttendanceState | None:
        """合作城市确认自己节目的到场回执。

        相同回执（同一 receipt_id 与内容）重放为无操作；
        同一 receipt_id 内容漂移则作废旧确认并要求重新确认。
        """
        with self._lock:
            show = self._show(show_id)
            if program_id not in show.order:
                raise NotFound(f"节目不在该场次: {program_id}")
            current = self._current_revision(program_id)
            self._require_own_city(actor, current.city)
            received_hash = _content_hash(content)
            known_hash = self._receipts.get(receipt_id)
            if known_hash is not None:
                if known_hash == received_hash:
                    return self._attendance.get(program_id)  # 重放：幂等
                self._emit(
                    "ATTENDANCE_DRIFTED",
                    "program_revision",
                    program_id,
                    {
                        "receipt_id": receipt_id,
                        "program_id": program_id,
                        "show_id": show_id,
                        "previous_hash": known_hash,
                        "received_hash": received_hash,
                    },
                    event_id=event_id,
                )
                raise ContentDrift("回执内容漂移，旧确认已作废，需重新确认")
            if received_hash != current.content_hash:
                raise ContentDrift("回执内容与当前节目版本不符，需按最新版本重新确认")
            self._emit(
                "ATTENDANCE_CONFIRMED",
                "program_revision",
                program_id,
                {
                    "receipt_id": receipt_id,
                    "program_id": program_id,
                    "show_id": show_id,
                    "revision": current.revision,
                    "content_hash": received_hash,
                    "city": current.city,
                    "confirmed_by": actor.name or actor.city or "",
                },
                event_id=event_id,
            )
            return self._attendance[program_id]

    # ------------------------------------------------------------------
    # 资源锁定
    # ------------------------------------------------------------------

    def hold_resources(
        self,
        actor: Actor,
        show_id: str,
        program_id: str,
        *,
        slot_id: str | None = None,
        equipment: Sequence[str] | None = None,
        event_id: str | None = None,
    ) -> str:
        """原子锁定一组设备与时段：任一冲突则全部不成交。

        返回锁定标识；冲突时抛出 ConflictError 并逐项给出冲突来源。
        """
        self._require(actor, Role.ORGANIZER, Role.STAGE_MANAGER)
        with self._lock:
            show = self._show(show_id)
            if program_id not in show.order:
                raise NotFound(f"节目不在该场次: {program_id}")
            slot = self._slot(slot_id or show.slot_id)
            wanted = sorted(set(equipment)) if equipment is not None else list(self._current_revision(program_id).equipment)
            if slot.elapsed(self._now()):
                raise InvalidState("场地时段已过，不能再锁定资源")
            own = [
                claim
                for claim in self._claims.values()
                if claim.active and claim.slot_id == slot.slot_id and claim.show_id == show_id and claim.program_id == program_id
            ]
            if own:
                if sorted(own[0].equipment) == wanted:
                    return own[0].claim_id  # 同一节目同一批设备：幂等
                raise InvalidState("该节目在此时段已有其他锁定，请先释放")
            conflicts: list[dict[str, Any]] = []
            occupants: set[tuple[str, str]] = set()
            for claim in self._claims.values():
                if not claim.active or claim.slot_id != slot.slot_id:
                    continue
                occupants.add((claim.show_id, claim.program_id))
                for resource in sorted(set(claim.equipment) & set(wanted)):
                    conflicts.append(
                        {
                            "type": "equipment_conflict",
                            "resource": resource,
                            "held_by_program": claim.program_id,
                            "held_by_show": claim.show_id,
                            "claim_id": claim.claim_id,
                        }
                    )
            if (show_id, program_id) not in occupants and len(occupants) + 1 > slot.capacity:
                conflicts.append(
                    {
                        "type": "slot_capacity",
                        "slot_id": slot.slot_id,
                        "capacity": slot.capacity,
                        "occupants": sorted(f"{s}/{p}" for s, p in occupants),
                    }
                )
            if conflicts:
                raise ConflictError("资源锁定冲突，本次未成交任何设备", conflicts)
            claim_id = event_id or f"claim-{uuid.uuid4().hex[:12]}"
            self._emit(
                "RESOURCE_HELD",
                "resource_claim",
                claim_id,
                {
                    "resource_ref": claim_id,
                    "slot": slot.slot_id,
                    "show_id": show_id,
                    "program_id": program_id,
                    "equipment": wanted,
                },
                event_id=claim_id,
            )
            return claim_id

    def confirm_equipment(self, actor: Actor, claim_id: str, *, event_id: str | None = None) -> str:
        """舞台负责人确认设备状态。"""
        self._require(actor, Role.STAGE_MANAGER)
        with self._lock:
            claim = self._claim(claim_id)
            if not claim.active:
                raise InvalidState(f"锁定已{claim.state}，不能确认设备")
            if claim.state == "confirmed":
                return claim_id  # 幂等
            self._emit(
                "EQUIPMENT_CONFIRMED",
                "resource_claim",
                claim_id,
                {"confirmed_by": actor.name or actor.role.value},
                event_id=event_id,
            )
            return claim_id

    def release_resources(self, actor: Actor, claim_id: str, *, reason: str, event_id: str | None = None) -> str:
        """主动释放锁定；已过时段的结算由 settle_elapsed 负责。"""
        self._require(actor, Role.ORGANIZER, Role.STAGE_MANAGER)
        if not reason or not reason.strip():
            raise ValueError("释放必须给出原因")
        with self._lock:
            claim = self._claim(claim_id)
            if not claim.active:
                return claim_id  # 幂等
            self._emit(
                "RESOURCE_RELEASED",
                "resource_claim",
                claim_id,
                {"reason": reason, "resource_ref": claim.claim_id, "slot": claim.slot_id},
                event_id=event_id,
            )
            return claim_id

    def settle_elapsed(self) -> list[str]:
        """结算已过时段的活跃锁定；结算事件入日志，重启不会再次释放。"""
        settled: list[str] = []
        with self._lock:
            now = self._now()
            for claim in list(self._claims.values()):
                if not claim.active:
                    continue
                slot = self._slots.get(claim.slot_id)
                if slot is None or not slot.elapsed(now):
                    continue
                self._emit(
                    "RESOURCE_RELEASED",
                    "resource_claim",
                    claim.claim_id,
                    {
                        "reason": "slot_elapsed",
                        "resource_ref": claim.claim_id,
                        "slot": claim.slot_id,
                        "settled_at": now.isoformat(),
                        "performance_date": slot.performance_date.isoformat(),
                    },
                    event_id=f"settle-{claim.claim_id}",
                )
                settled.append(claim.claim_id)
        return settled

    # ------------------------------------------------------------------
    # 费用承诺与冻结
    # ------------------------------------------------------------------

    def commit_cost(
        self,
        actor: Actor,
        show_id: str,
        program_id: str,
        *,
        amount: int,
        currency: str,
        note: str = "",
        event_id: str | None = None,
    ) -> str:
        """主办方登记一笔费用承诺（金额为最小货币单位）。"""
        self._require(actor, Role.ORGANIZER)
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("金额必须是非负整数（最小货币单位）")
        if not currency or not currency.strip():
            raise ValueError("币种不能为空")
        with self._lock:
            show = self._show(show_id)
            if program_id not in show.order:
                raise NotFound(f"节目不在该场次: {program_id}")
            return self._emit(
                "COST_COMMITTED",
                "show_record",
                show_id,
                {"program_id": program_id, "amount": amount, "currency": currency, "note": note},
                event_id=event_id,
            )

    def freeze_show(self, actor: Actor, show_id: str, *, event_id: str | None = None) -> str:
        """主办方冻结最终节目顺序；仍缺确认时抛出 FreezeNotReady。"""
        self._require(actor, Role.ORGANIZER)
        with self._lock:
            show = self._show(show_id)
            if show.frozen:
                return show_id  # 幂等
            missing = self.missing_confirmations(show_id)
            if missing:
                raise FreezeNotReady(missing)
            current = self._current_revision
            self._emit(
                "SHOW_FROZEN",
                "show_record",
                show_id,
                {
                    "revision": show.order_revision,
                    "frozen_at": self._now().isoformat(),
                    "frozen_order": [[pid, current(pid).revision] for pid in show.order],
                },
                event_id=event_id,
            )
            return show_id

    def record_performed(
        self,
        actor: Actor,
        show_id: str,
        program_id: str,
        *,
        note: str = "",
        event_id: str | None = None,
    ) -> str:
        """记录节目已演出；记录不可修改，重复记录为幂等。"""
        self._require(actor, Role.ORGANIZER, Role.STAGE_MANAGER)
        with self._lock:
            show = self._show(show_id)
            if program_id not in show.order:
                raise NotFound(f"节目不在该场次: {program_id}")
            if not show.frozen:
                raise InvalidState("场次未冻结，不能记录演出")
            if program_id in show.performed:
                return program_id  # 幂等
            self._emit(
                "PERFORMANCE_RECORDED",
                "show_record",
                show_id,
                {
                    "program_id": program_id,
                    "revision": self._current_revision(program_id).revision,
                    "performed_at": self._now().isoformat(),
                    "note": note,
                },
                event_id=event_id,
            )
            return program_id

    # ------------------------------------------------------------------
    # 现场变更
    # ------------------------------------------------------------------

    def open_change(
        self,
        actor: Actor,
        show_id: str,
        *,
        description: str,
        details: Mapping[str, Any] | None = None,
        event_id: str | None = None,
    ) -> str:
        """登记一笔现场变更。"""
        if not description or not description.strip():
            raise ValueError("变更描述不能为空")
        with self._lock:
            self._show(show_id)
            change_id = event_id or f"chg-{uuid.uuid4().hex[:12]}"
            self._emit(
                "CHANGE_OPENED",
                "show_record",
                show_id,
                {
                    "change_id": change_id,
                    "description": description,
                    "details": dict(details or {}),
                    "opened_by": actor.name or actor.role.value,
                },
                event_id=change_id,
            )
            return change_id

    def review_change(self, actor: Actor, change_id: str, *, decision: str, event_id: str | None = None) -> str:
        """主办方审批现场变更。"""
        self._require(actor, Role.ORGANIZER)
        if decision not in ("approved", "rejected"):
            raise ValueError("审批结论只能是 approved 或 rejected")
        with self._lock:
            change = self._change(change_id)
            if change.status != "open":
                raise InvalidState(f"变更状态为 {change.status}，不能审批")
            self._emit(
                "CHANGE_REVIEWED",
                "show_record",
                change.show_id,
                {"change_id": change_id, "decision": decision, "reviewed_by": actor.name or actor.role.value},
                event_id=event_id,
            )
            return change_id

    def resume_change(self, actor: Actor, change_id: str, *, event_id: str | None = None) -> ChangeRecord:
        """值班人员恢复一笔未办结的现场变更（含服务重启之后）。"""
        self._require(actor, Role.DUTY_STAFF)
        with self._lock:
            change = self._change(change_id)
            if change.status == "in_progress":
                return change  # 幂等
            if change.status not in ("open", "approved"):
                raise InvalidState(f"变更状态为 {change.status}，不能恢复")
            self._emit(
                "CHANGE_RESUMED",
                "show_record",
                change.show_id,
                {"change_id": change_id, "resumed_by": actor.name or actor.role.value},
                event_id=event_id,
            )
            return self._changes[change_id]

    def close_change(self, actor: Actor, change_id: str, *, outcome: str, event_id: str | None = None) -> str:
        """办结现场变更。"""
        self._require(actor, Role.ORGANIZER, Role.DUTY_STAFF)
        if not outcome or not outcome.strip():
            raise ValueError("办结说明不能为空")
        with self._lock:
            change = self._change(change_id)
            if change.status in ("closed", "rejected"):
                raise InvalidState(f"变更状态为 {change.status}，不能办结")
            self._emit(
                "CHANGE_CLOSED",
                "show_record",
                change.show_id,
                {"change_id": change_id, "outcome": outcome, "closed_by": actor.name or actor.role.value},
                event_id=event_id,
            )
            return change_id

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def performance_date(self, show_id: str) -> date:
        """场次演出日，按场地本地日期计算。"""
        show = self._show(show_id)
        return self._slot(show.slot_id).performance_date

    def program_history(self, program_id: str) -> list[ProgramRevision]:
        """节目全部版本，被替换的内容与原因都在其中。"""
        revisions = self._programs.get(program_id)
        if not revisions:
            raise NotFound(f"节目不存在: {program_id}")
        return [revisions[key] for key in sorted(revisions)]

    def slot_claims(self, slot_id: str) -> list[ResourceClaim]:
        """某时段的全部锁定（含已释放、已结算）。"""
        self._slot(slot_id)
        return [claim for claim in self._claims.values() if claim.slot_id == slot_id]

    def cost_commitments(self, show_id: str, program_id: str | None = None) -> list[CostCommitment]:
        self._show(show_id)
        return [
            cost
            for cost in self._costs
            if cost.show_id == show_id and (program_id is None or cost.program_id == program_id)
        ]

    def unfinished_changes(self, show_id: str | None = None) -> list[ChangeRecord]:
        """未办结的现场变更；值班人员据此恢复处理。"""
        return [
            change
            for change in self._changes.values()
            if change.unfinished and (show_id is None or change.show_id == show_id)
        ]

    def _equipment_conflicts(self, show_id: str, program_id: str, slot_id: str, equipment: Iterable[str]) -> list[dict[str, Any]]:
        wanted = set(equipment)
        conflicts = []
        for claim in self._claims.values():
            if not claim.active or claim.slot_id != slot_id:
                continue
            if claim.show_id == show_id and claim.program_id == program_id:
                continue
            for resource in sorted(set(claim.equipment) & wanted):
                conflicts.append(
                    {
                        "type": "equipment_conflict",
                        "resource": resource,
                        "held_by_program": claim.program_id,
                        "held_by_show": claim.show_id,
                        "claim_id": claim.claim_id,
                    }
                )
        return conflicts

    def missing_confirmations(self, show_id: str) -> list[MissingConfirmation]:
        """指定场次仍缺的确认及其冲突来源。"""
        with self._lock:
            show = self._show(show_id)
            slot = self._slot(show.slot_id)
            missing: list[MissingConfirmation] = []
            if slot.elapsed(self._now()):
                missing.append(
                    MissingConfirmation("", "slot", f"场地时段已过（演出日 {slot.performance_date.isoformat()}）")
                )
            for program_id in show.order:
                current = self._current_revision(program_id)
                attendance = self._attendance.get(program_id)
                if attendance is None:
                    detail = "到场回执内容漂移，需重新确认" if program_id in self._drifted else "人员到场未确认"
                    missing.append(MissingConfirmation(program_id, "attendance", detail))
                elif attendance.revision != current.revision:
                    missing.append(
                        MissingConfirmation(program_id, "attendance", "节目已改版，到场确认需按新版本重新确认")
                    )
                claim = next(
                    (
                        item
                        for item in self._claims.values()
                        if item.active and item.show_id == show_id and item.program_id == program_id
                    ),
                    None,
                )
                if claim is None:
                    conflicts = self._equipment_conflicts(show_id, program_id, show.slot_id, current.equipment)
                    missing.append(
                        MissingConfirmation(program_id, "equipment_held", "设备与时段未锁定", tuple(conflicts))
                    )
                elif claim.state != "confirmed":
                    missing.append(
                        MissingConfirmation(program_id, "equipment_confirmed", "设备已锁定但舞台负责人未确认")
                    )
                if not any(cost.show_id == show_id and cost.program_id == program_id for cost in self._costs):
                    missing.append(MissingConfirmation(program_id, "cost", "费用承诺未登记"))
            return missing

    def executable_order(self, show_id: str) -> ShowOrderView:
        """指定场次目前可执行的节目顺序：冻结后按冻结版本，否则按当前版本。"""
        with self._lock:
            show = self._show(show_id)
            slot = self._slot(show.slot_id)
            missing_by_program: dict[str, list[str]] = {}
            slot_missing: list[str] = []
            for item in self.missing_confirmations(show_id):
                if item.kind == "slot":
                    slot_missing.append(item.detail)
                else:
                    missing_by_program.setdefault(item.program_id, []).append(item.detail)
            if show.frozen and show.frozen_order is not None:
                sequence = list(show.frozen_order)
            else:
                sequence = [(pid, self._current_revision(pid).revision) for pid in show.order]
            entries = []
            for program_id, revision in sequence:
                revisions = self._programs[program_id]
                info = revisions.get(revision) or revisions[max(revisions)]
                blockers = tuple(missing_by_program.get(program_id, ()))
                entries.append(
                    ProgramReadiness(
                        program_id=program_id,
                        revision=info.revision,
                        title=info.title,
                        ready=not blockers,
                        blockers=blockers,
                    )
                )
            elapsed = slot.elapsed(self._now())
            executable = not elapsed and all(entry.ready for entry in entries)
            return ShowOrderView(
                show_id=show_id,
                slot_id=show.slot_id,
                performance_date=slot.performance_date,
                frozen=show.frozen,
                slot_elapsed=elapsed,
                executable=executable,
                entries=tuple(entries),
            )

    # ------------------------------------------------------------------
    # 事件重放处理器（只改状态，不做权限判断）
    # ------------------------------------------------------------------

    def _on_program_registered(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        self._programs.setdefault(aggregate_id, {})[int(payload["revision"])] = ProgramRevision(
            program_id=aggregate_id,
            revision=int(payload["revision"]),
            city=payload["city"],
            title=payload["title"],
            duration_minutes=int(payload["duration_minutes"]),
            equipment=tuple(payload.get("equipment", ())),
            content=dict(payload.get("content", {})),
            content_hash=payload["content_hash"],
            reason="",
            supersedes=None,
            recorded_at=occurred_at,
        )

    def _on_program_changed(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        revision = ProgramRevision(
            program_id=aggregate_id,
            revision=int(payload["revision"]),
            city=payload["city"],
            title=payload["title"],
            duration_minutes=int(payload["duration_minutes"]),
            equipment=tuple(payload.get("equipment", ())),
            content=dict(payload.get("content", {})),
            content_hash=payload["content_hash"],
            reason=payload["reason"],
            supersedes=int(payload["supersedes"]),
            recorded_at=occurred_at,
        )
        self._programs.setdefault(aggregate_id, {})[revision.revision] = revision
        self._attendance.pop(aggregate_id, None)  # 内容漂移：需重新确认
        self._drifted.discard(aggregate_id)
        show = self._shows.get(payload.get("show_id", ""))
        if show is not None:
            show.order_revision += 1
            if show.frozen_order is not None:
                show.frozen_order = [
                    (pid, revision.revision if pid == aggregate_id else rev) for pid, rev in show.frozen_order
                ]

    def _on_slot_defined(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        self._slots[aggregate_id] = VenueSlot(
            slot_id=aggregate_id,
            venue=payload["venue"],
            tz=payload["tz"],
            load_in_start=_parse_time(payload["load_in_start"], "load_in_start"),
            show_start=_parse_time(payload["show_start"], "show_start"),
            show_end=_parse_time(payload["show_end"], "show_end"),
            load_out_end=_parse_time(payload["load_out_end"], "load_out_end"),
            capacity=int(payload.get("capacity", 1)),
        )

    def _on_show_opened(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        self._shows[aggregate_id] = ShowRecord(
            show_id=aggregate_id,
            slot_id=payload["slot_id"],
            order=list(payload["order"]),
        )

    def _on_attendance_confirmed(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        program_id = payload["program_id"]
        self._attendance[program_id] = AttendanceState(
            program_id=program_id,
            revision=int(payload["revision"]),
            content_hash=payload["content_hash"],
            receipt_id=payload["receipt_id"],
            city=payload["city"],
            confirmed_at=occurred_at,
        )
        self._receipts[payload["receipt_id"]] = payload["content_hash"]
        self._drifted.discard(program_id)

    def _on_attendance_drifted(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        program_id = payload["program_id"]
        self._attendance.pop(program_id, None)
        self._receipts[payload["receipt_id"]] = payload["received_hash"]
        self._drifted.add(program_id)

    def _on_resource_held(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        self._claims[aggregate_id] = ResourceClaim(
            claim_id=aggregate_id,
            show_id=payload["show_id"],
            program_id=payload["program_id"],
            slot_id=payload["slot"],
            equipment=tuple(payload.get("equipment", ())),
        )

    def _on_equipment_confirmed(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        claim = self._claims[aggregate_id]
        claim.state = "confirmed"
        claim.equipment_confirmed_by = payload["confirmed_by"]

    def _on_resource_released(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        claim = self._claims[aggregate_id]
        claim.state = "settled" if payload["reason"] == "slot_elapsed" else "released"
        claim.release_reason = payload["reason"]

    def _on_cost_committed(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        self._costs.append(
            CostCommitment(
                show_id=aggregate_id,
                program_id=payload["program_id"],
                amount=int(payload["amount"]),
                currency=payload["currency"],
                note=payload.get("note", ""),
                committed_at=occurred_at,
            )
        )

    def _on_show_frozen(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        show = self._shows[aggregate_id]
        show.frozen = True
        show.frozen_revision = int(payload["revision"])
        show.frozen_at = _parse_time(payload["frozen_at"], "frozen_at")
        show.frozen_order = [(pid, int(rev)) for pid, rev in payload["frozen_order"]]

    def _on_performance_recorded(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        show = self._shows[aggregate_id]
        program_id = payload["program_id"]
        if program_id not in show.performed:
            show.performed[program_id] = PerformedEntry(
                program_id=program_id,
                revision=int(payload["revision"]),
                performed_at=_parse_time(payload["performed_at"], "performed_at"),
                note=payload.get("note", ""),
            )

    def _on_change_opened(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        self._changes[payload["change_id"]] = ChangeRecord(
            change_id=payload["change_id"],
            show_id=aggregate_id,
            description=payload["description"],
            details=dict(payload.get("details", {})),
            opened_by=payload.get("opened_by", ""),
            opened_at=occurred_at,
        )

    def _on_change_reviewed(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        change = self._changes[payload["change_id"]]
        change.status = payload["decision"]
        change.decision = payload["decision"]
        change.reviewed_by = payload.get("reviewed_by", "")

    def _on_change_resumed(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        change = self._changes[payload["change_id"]]
        change.status = "in_progress"
        change.resumed_by = payload.get("resumed_by", "")

    def _on_change_closed(self, aggregate_id: str, payload: Mapping[str, Any], occurred_at: datetime) -> None:
        change = self._changes[payload["change_id"]]
        change.status = "closed"
        change.closed_by = payload.get("closed_by", "")
        change.outcome = payload["outcome"]
