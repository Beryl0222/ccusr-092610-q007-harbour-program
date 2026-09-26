import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harbour_program.service import (
    Actor,
    CommitmentService,
    ConflictError,
    ContentDrift,
    FreezeNotReady,
    InvalidState,
    PermissionDenied,
    Role,
)

HK = ZoneInfo("Asia/Hong_Kong")

NJ_CONTENT = {"program": "昆曲《牡丹亭》", "troupe": "江苏省昆剧院", "headcount": 12}
SZ_CONTENT = {"program": "评弹《春江花月夜》", "troupe": "苏州评弹团", "headcount": 6}


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal_path = Path(self.tmp.name) / "journal.jsonl"
        self.clock = [datetime(2026, 9, 25, 10, 0, tzinfo=HK)]
        self.svc = self._open_service()
        self.organizer = Actor(Role.ORGANIZER, name="主办方")
        self.stage = Actor(Role.STAGE_MANAGER, name="舞台负责人")
        self.duty = Actor(Role.DUTY_STAFF, name="值班人员")
        self.nanjing = Actor(Role.PARTNER_CITY, city="南京", name="南京联络员")
        self.suzhou = Actor(Role.PARTNER_CITY, city="苏州", name="苏州联络员")

    def _open_service(self) -> CommitmentService:
        return CommitmentService(self.journal_path, now=lambda: self.clock[0])

    def _define_slot(self, slot_id="slot-1", capacity=2):
        return self.svc.define_slot(
            self.organizer,
            slot_id,
            venue="香港文化中心露天广场",
            tz="Asia/Hong_Kong",
            load_in_start=datetime(2026, 9, 30, 21, 0, tzinfo=HK),
            show_start=datetime(2026, 9, 30, 23, 0, tzinfo=HK),
            show_end=datetime(2026, 10, 1, 0, 30, tzinfo=HK),
            load_out_end=datetime(2026, 10, 1, 2, 0, tzinfo=HK),
            capacity=capacity,
        )

    def _register_programs(self):
        self.svc.register_program(
            self.nanjing,
            "prog-nj",
            city="南京",
            title="昆曲《牡丹亭》",
            duration_minutes=40,
            equipment=["灯A", "音响X"],
            content=NJ_CONTENT,
        )
        self.svc.register_program(
            self.suzhou,
            "prog-sz",
            city="苏州",
            title="评弹《春江花月夜》",
            duration_minutes=30,
            equipment=["话筒", "返送"],
            content=SZ_CONTENT,
        )

    def _open_show(self, show_id="show-1", slot_id="slot-1"):
        return self.svc.open_show(self.organizer, show_id, slot_id=slot_id, order=["prog-nj", "prog-sz"])

    def _make_show(self):
        self._define_slot()
        self._register_programs()
        self._open_show()

    def _confirm_attendance(self, program_id, content):
        actor = self.nanjing if program_id == "prog-nj" else self.suzhou
        return self.svc.confirm_attendance(
            actor, "show-1", program_id, receipt_id=f"receipt-{program_id}", content=content
        )

    def _fully_confirm(self, program_id, content):
        self._confirm_attendance(program_id, content)
        claim_id = self.svc.hold_resources(self.stage, "show-1", program_id)
        self.svc.confirm_equipment(self.stage, claim_id)
        self.svc.commit_cost(self.organizer, "show-1", program_id, amount=120000, currency="HKD")

    def _journal_events(self):
        return [json.loads(line) for line in self.journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # --------------------------------------------------------------
    # 场地窗口与跨午夜
    # --------------------------------------------------------------

    def test_performance_date_uses_venue_local_date(self):
        self._make_show()
        self.assertEqual(date(2026, 9, 30), self.svc.performance_date("show-1"))
        # 跨午夜：演出结束与撤场已在本地次日，但演出日仍归 9 月 30 日
        slot = self.svc._slots["slot-1"]
        self.assertEqual(date(2026, 10, 1), slot.show_end.astimezone(HK).date())
        self.assertEqual(date(2026, 9, 30), slot.performance_date)

    def test_local_midnight_boundary_not_utc(self):
        # 本地 10 月 1 日 00:30 开演（UTC 仍是 9 月 30 日 16:30），演出日按场地本地日期
        self.svc.define_slot(
            self.organizer,
            "slot-late",
            venue="西九文化区",
            tz="Asia/Hong_Kong",
            load_in_start=datetime(2026, 9, 30, 22, 0, tzinfo=HK),
            show_start=datetime(2026, 10, 1, 0, 30, tzinfo=HK),
            show_end=datetime(2026, 10, 1, 2, 0, tzinfo=HK),
            load_out_end=datetime(2026, 10, 1, 3, 30, tzinfo=HK),
        )
        slot = self.svc._slots["slot-late"]
        self.assertEqual(date(2026, 9, 30), slot.show_start.astimezone(ZoneInfo("UTC")).date())
        self.assertEqual(date(2026, 10, 1), slot.performance_date)

    def test_slot_requires_timezone_and_order(self):
        with self.assertRaises(ValueError):
            self.svc.define_slot(
                self.organizer,
                "slot-bad",
                venue="v",
                tz="Asia/Hong_Kong",
                load_in_start=datetime(2026, 9, 30, 21, 0),  # 无时区
                show_start=datetime(2026, 9, 30, 23, 0, tzinfo=HK),
                show_end=datetime(2026, 10, 1, 0, 30, tzinfo=HK),
                load_out_end=datetime(2026, 10, 1, 2, 0, tzinfo=HK),
            )
        with self.assertRaises(PermissionDenied):
            self.svc.define_slot(
                self.stage,
                "slot-bad-2",
                venue="v",
                tz="Asia/Hong_Kong",
                load_in_start=datetime(2026, 9, 30, 21, 0, tzinfo=HK),
                show_start=datetime(2026, 9, 30, 23, 0, tzinfo=HK),
                show_end=datetime(2026, 10, 1, 0, 30, tzinfo=HK),
                load_out_end=datetime(2026, 10, 1, 2, 0, tzinfo=HK),
            )

    # --------------------------------------------------------------
    # 到场确认：权限、重放、漂移
    # --------------------------------------------------------------

    def test_partner_city_confirms_only_own_program(self):
        self._make_show()
        self._confirm_attendance("prog-nj", NJ_CONTENT)
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_attendance(self.suzhou, "show-1", "prog-nj", receipt_id="r-x", content=NJ_CONTENT)
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_attendance(self.stage, "show-1", "prog-nj", receipt_id="r-y", content=NJ_CONTENT)
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_attendance(self.organizer, "show-1", "prog-nj", receipt_id="r-z", content=NJ_CONTENT)

    def test_attendance_replay_is_idempotent(self):
        self._make_show()
        self._confirm_attendance("prog-nj", NJ_CONTENT)
        count = len(self._journal_events())
        again = self._confirm_attendance("prog-nj", NJ_CONTENT)
        self.assertIsNotNone(again)
        self.assertEqual(count, len(self._journal_events()))  # 重放不产生新事件

    def test_attendance_drift_requires_reconfirmation(self):
        self._make_show()
        self._confirm_attendance("prog-nj", NJ_CONTENT)
        drifted = dict(NJ_CONTENT, headcount=14)
        with self.assertRaises(ContentDrift):
            self.svc.confirm_attendance(self.nanjing, "show-1", "prog-nj", receipt_id="receipt-prog-nj", content=drifted)
        missing = {item.kind for item in self.svc.missing_confirmations("show-1") if item.program_id == "prog-nj"}
        self.assertIn("attendance", missing)
        details = [item.detail for item in self.svc.missing_confirmations("show-1") if item.program_id == "prog-nj"]
        self.assertTrue(any("漂移" in detail for detail in details))
        # 用新回执按当前版本内容重新确认后恢复
        self.svc.confirm_attendance(self.nanjing, "show-1", "prog-nj", receipt_id="receipt-prog-nj-2", content=NJ_CONTENT)
        missing = {item.kind for item in self.svc.missing_confirmations("show-1") if item.program_id == "prog-nj"}
        self.assertNotIn("attendance", missing)

    def test_attendance_receipt_must_match_current_revision(self):
        self._make_show()
        with self.assertRaises(ContentDrift):
            self.svc.confirm_attendance(
                self.nanjing, "show-1", "prog-nj", receipt_id="r-other", content={"program": "别的内容"}
            )

    # --------------------------------------------------------------
    # 资源锁定：原子性、冲突来源、容量、角色
    # --------------------------------------------------------------

    def test_hold_is_atomic_and_reports_conflict_source(self):
        self._define_slot()
        self.svc.register_program(
            self.nanjing, "prog-nj", city="南京", title="昆曲《牡丹亭》",
            duration_minutes=40, equipment=["灯A", "音响X"], content=NJ_CONTENT,
        )
        # 苏州的节目同样需要音响X：并行节目共享设备，冲突在此发生
        self.svc.register_program(
            self.suzhou, "prog-sz", city="苏州", title="评弹《春江花月夜》",
            duration_minutes=30, equipment=["音响X", "话筒"], content=SZ_CONTENT,
        )
        self._open_show()
        self.svc.hold_resources(self.stage, "show-1", "prog-nj")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.hold_resources(self.stage, "show-1", "prog-sz")
        self.assertEqual("equipment_conflict", ctx.exception.conflicts[0]["type"])
        self.assertEqual("音响X", ctx.exception.conflicts[0]["resource"])
        self.assertEqual("prog-nj", ctx.exception.conflicts[0]["held_by_program"])
        # 原子性：冲突后苏州未成交任何设备
        claims = [c for c in self.svc.slot_claims("slot-1") if c.program_id == "prog-sz" and c.active]
        self.assertEqual([], claims)
        missing = [item for item in self.svc.missing_confirmations("show-1") if item.program_id == "prog-sz"]
        equipment_missing = {item.kind: item for item in missing}["equipment_held"]
        self.assertEqual("prog-nj", equipment_missing.conflicts[0]["held_by_program"])

    def test_slot_capacity_limits_parallel_programs(self):
        self._define_slot(capacity=1)
        self._register_programs()
        self._open_show()
        self.svc.hold_resources(self.stage, "show-1", "prog-nj")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.hold_resources(self.stage, "show-1", "prog-sz")
        self.assertEqual("slot_capacity", ctx.exception.conflicts[0]["type"])

    def test_same_program_same_equipment_hold_is_idempotent(self):
        self._make_show()
        first = self.svc.hold_resources(self.stage, "show-1", "prog-nj")
        second = self.svc.hold_resources(self.stage, "show-1", "prog-nj")
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.svc.slot_claims("slot-1")))

    def test_equipment_confirmed_only_by_stage_manager(self):
        self._make_show()
        claim_id = self.svc.hold_resources(self.stage, "show-1", "prog-nj")
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_equipment(self.organizer, claim_id)
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_equipment(self.nanjing, claim_id)
        self.svc.confirm_equipment(self.stage, claim_id)
        self.svc.confirm_equipment(self.stage, claim_id)  # 幂等
        claim = [c for c in self.svc.slot_claims("slot-1") if c.claim_id == claim_id][0]
        self.assertEqual("confirmed", claim.state)

    def test_hold_rejected_after_slot_elapsed(self):
        self._make_show()
        self.clock[0] = datetime(2026, 10, 1, 3, 0, tzinfo=HK)
        with self.assertRaises(InvalidState):
            self.svc.hold_resources(self.stage, "show-1", "prog-nj")

    # --------------------------------------------------------------
    # 费用承诺与冻结
    # --------------------------------------------------------------

    def test_cost_commitment_retry_is_idempotent(self):
        self._make_show()
        self.svc.commit_cost(self.organizer, "show-1", "prog-nj", amount=100, currency="HKD", event_id="cost-1")
        self.svc.commit_cost(self.organizer, "show-1", "prog-nj", amount=100, currency="HKD", event_id="cost-1")
        self.assertEqual(1, len(self.svc.cost_commitments("show-1", "prog-nj")))
        with self.assertRaises(PermissionDenied):
            self.svc.commit_cost(self.stage, "show-1", "prog-nj", amount=100, currency="HKD")

    def test_freeze_requires_organizer_and_full_confirmation(self):
        self._make_show()
        with self.assertRaises(FreezeNotReady) as ctx:
            self.svc.freeze_show(self.organizer, "show-1")
        kinds = {(item.program_id, item.kind) for item in ctx.exception.missing}
        self.assertIn(("prog-nj", "attendance"), kinds)
        self.assertIn(("prog-nj", "equipment_held"), kinds)
        self.assertIn(("prog-nj", "cost"), kinds)
        self.assertIn(("prog-sz", "equipment_held"), kinds)

        self._fully_confirm("prog-nj", NJ_CONTENT)
        self._fully_confirm("prog-sz", SZ_CONTENT)
        with self.assertRaises(PermissionDenied):
            self.svc.freeze_show(self.stage, "show-1")
        self.svc.freeze_show(self.organizer, "show-1")
        self.svc.freeze_show(self.organizer, "show-1")  # 幂等
        view = self.svc.executable_order("show-1")
        self.assertTrue(view.frozen)
        self.assertTrue(view.executable)

    def test_freeze_blocked_when_equipment_not_confirmed_by_stage(self):
        self._make_show()
        self._confirm_attendance("prog-nj", NJ_CONTENT)
        self._confirm_attendance("prog-sz", SZ_CONTENT)
        self.svc.hold_resources(self.stage, "show-1", "prog-nj")
        self.svc.hold_resources(self.stage, "show-1", "prog-sz")
        self.svc.commit_cost(self.organizer, "show-1", "prog-nj", amount=1, currency="HKD")
        self.svc.commit_cost(self.organizer, "show-1", "prog-sz", amount=1, currency="HKD")
        with self.assertRaises(FreezeNotReady) as ctx:
            self.svc.freeze_show(self.organizer, "show-1")
        kinds = {item.kind for item in ctx.exception.missing}
        self.assertEqual({"equipment_confirmed"}, kinds)

    # --------------------------------------------------------------
    # 替补与已演出记录
    # --------------------------------------------------------------

    def test_substitute_keeps_replaced_content_and_reason(self):
        self._make_show()
        self._fully_confirm("prog-nj", NJ_CONTENT)
        new_content = {"program": "昆曲折子戏集锦", "troupe": "江苏省昆剧院", "headcount": 8}
        revision = self.svc.substitute_program(
            self.organizer,
            "show-1",
            "prog-nj",
            title="昆曲折子戏集锦",
            duration_minutes=25,
            equipment=["灯A"],
            content=new_content,
            reason="嘉宾航班延误，原阵容无法到场",
        )
        self.assertEqual(2, revision)
        history = self.svc.program_history("prog-nj")
        self.assertEqual(2, len(history))
        self.assertEqual(NJ_CONTENT, history[0].content)  # 被替换内容保留
        self.assertEqual(1, history[1].supersedes)
        self.assertEqual("嘉宾航班延误，原阵容无法到场", history[1].reason)
        # 内容漂移：旧到场确认作废，需重新确认
        missing = {item.kind for item in self.svc.missing_confirmations("show-1") if item.program_id == "prog-nj"}
        self.assertIn("attendance", missing)
        # 设备需求变化：旧锁定被释放
        claims = self.svc.slot_claims("slot-1")
        self.assertEqual("released", claims[0].state)
        self.assertEqual("program_changed", claims[0].release_reason)

    def test_substitute_only_by_owner_or_organizer(self):
        self._make_show()
        with self.assertRaises(PermissionDenied):
            self.svc.substitute_program(
                self.suzhou,
                "show-1",
                "prog-nj",
                title="t",
                duration_minutes=10,
                content={},
                reason="越权操作",
            )

    def test_performed_record_cannot_be_revised(self):
        self._make_show()
        self._fully_confirm("prog-nj", NJ_CONTENT)
        self._fully_confirm("prog-sz", SZ_CONTENT)
        with self.assertRaises(InvalidState):
            self.svc.record_performed(self.stage, "show-1", "prog-nj")  # 未冻结
        self.svc.freeze_show(self.organizer, "show-1")
        self.svc.record_performed(self.stage, "show-1", "prog-nj")
        self.svc.record_performed(self.stage, "show-1", "prog-nj")  # 幂等
        with self.assertRaises(InvalidState):
            self.svc.substitute_program(
                self.organizer,
                "show-1",
                "prog-nj",
                title="改演",
                duration_minutes=10,
                content={},
                reason="试图改写已演出记录",
            )

    def test_frozen_show_substitution_needs_approved_change(self):
        self._make_show()
        self._fully_confirm("prog-nj", NJ_CONTENT)
        self._fully_confirm("prog-sz", SZ_CONTENT)
        self.svc.freeze_show(self.organizer, "show-1")
        with self.assertRaises(PermissionDenied):
            self.svc.substitute_program(
                self.organizer, "show-1", "prog-nj", title="t", duration_minutes=5, content={}, reason="r"
            )
        change_id = self.svc.open_change(self.duty, "show-1", description="合作城市临时换演出内容")
        self.svc.review_change(self.organizer, change_id, decision="approved")
        revision = self.svc.substitute_program(
            self.organizer,
            "show-1",
            "prog-nj",
            title="备选节目",
            duration_minutes=20,
            equipment=["灯A"],
            content={"program": "备选节目"},
            reason="合作城市临时换演出内容",
            change_id=change_id,
        )
        self.assertEqual(2, revision)
        view = self.svc.executable_order("show-1")
        entry = {e.program_id: e for e in view.entries}["prog-nj"]
        self.assertEqual(2, entry.revision)

    # --------------------------------------------------------------
    # 重启恢复：结算幂等、未办结变更
    # --------------------------------------------------------------

    def test_restart_does_not_release_elapsed_slot_twice(self):
        self._make_show()
        claim_id = self.svc.hold_resources(self.stage, "show-1", "prog-nj")
        self.clock[0] = datetime(2026, 10, 1, 3, 0, tzinfo=HK)  # 撤场结束后
        restarted = self._open_service()
        claim = [c for c in restarted.slot_claims("slot-1") if c.claim_id == claim_id][0]
        self.assertEqual("settled", claim.state)
        settled = [
            e for e in self._journal_events()
            if e["event_type"] == "RESOURCE_RELEASED" and e["payload"]["reason"] == "slot_elapsed"
        ]
        self.assertEqual(1, len(settled))
        self.assertEqual("2026-09-30", settled[0]["payload"]["performance_date"])
        count = len(self._journal_events())
        again = self._open_service()  # 再次重启不得重复释放
        self.assertEqual(count, len(self._journal_events()))
        claim = [c for c in again.slot_claims("slot-1") if c.claim_id == claim_id][0]
        self.assertEqual("settled", claim.state)

    def test_unfinished_changes_resume_after_restart(self):
        self._make_show()
        change_id = self.svc.open_change(self.duty, "show-1", description="场地时段压缩，需调整顺序")
        with self.assertRaises(PermissionDenied):
            self.svc.review_change(self.stage, change_id, decision="approved")
        self.svc.review_change(self.organizer, change_id, decision="approved")

        restarted = self._open_service()
        unfinished = restarted.unfinished_changes("show-1")
        self.assertEqual([change_id], [c.change_id for c in unfinished])
        self.assertEqual("approved", unfinished[0].status)

        with self.assertRaises(PermissionDenied):
            restarted.resume_change(self.nanjing, change_id)
        resumed = restarted.resume_change(self.duty, change_id)
        self.assertEqual("in_progress", resumed.status)
        restarted.close_change(self.duty, change_id, outcome="顺序已调整并通知各城市")
        self.assertEqual([], restarted.unfinished_changes("show-1"))

        reloaded = self._open_service()  # 办结状态同样经得起重启
        self.assertEqual([], reloaded.unfinished_changes("show-1"))

    # --------------------------------------------------------------
    # 查询：可执行顺序
    # --------------------------------------------------------------

    def test_executable_order_reflects_readiness(self):
        self._make_show()
        view = self.svc.executable_order("show-1")
        self.assertFalse(view.executable)
        self.assertFalse(view.frozen)
        self.assertEqual(date(2026, 9, 30), view.performance_date)
        self.assertEqual(["prog-nj", "prog-sz"], [e.program_id for e in view.entries])
        self.assertTrue(all(e.blockers for e in view.entries))

        self._fully_confirm("prog-nj", NJ_CONTENT)
        self._fully_confirm("prog-sz", SZ_CONTENT)
        view = self.svc.executable_order("show-1")
        self.assertTrue(view.executable)
        self.assertTrue(all(e.ready for e in view.entries))

        self.clock[0] = datetime(2026, 10, 1, 3, 0, tzinfo=HK)
        view = self.svc.executable_order("show-1")
        self.assertTrue(view.slot_elapsed)
        self.assertFalse(view.executable)

    def test_state_survives_restart(self):
        self._make_show()
        self._fully_confirm("prog-nj", NJ_CONTENT)
        restarted = self._open_service()
        view = restarted.executable_order("show-1")
        entry = {e.program_id: e for e in view.entries}["prog-nj"]
        self.assertTrue(entry.ready)
        missing = restarted.missing_confirmations("show-1")
        self.assertEqual({"prog-sz"}, {item.program_id for item in missing})


if __name__ == "__main__":
    unittest.main()
