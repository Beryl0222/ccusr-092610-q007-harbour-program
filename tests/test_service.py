import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harbour_program.contracts import validate_event
from harbour_program.service import (
    Actor,
    CommitmentService,
    ServiceError,
)

HK = ZoneInfo("Asia/Hong_Kong")
ORG = Actor("organizer", name="主办方")
STAGE = Actor("stage_manager", name="舞台负责人")
JIANGSU = Actor("partner", city="南京", name="江苏团")
OTHER_CITY = Actor("partner", city="珠海", name="珠海团")
DUTY = Actor("duty", name="值班员")


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.svc = CommitmentService(self.path)
        self.svc.register_venue(ORG, "harbour", "Asia/Hong_Kong")
        for pid, city, title in [
            ("p-js", "南京", "江苏文旅推介"),
            ("p-zh", "珠海", "珠海民俗展演"),
            ("p-backup", "广州", "岭南醒狮（替补）"),
        ]:
            self.svc.register_program(ORG, pid, city, title, f"{title}内容", "2026-09-25T10:00:00+08:00")
        self.svc.open_session(ORG, "show-0926", "harbour", ["p-js", "p-zh"])
        # 建册时同步申报设备：江苏用追光，珠海需要主音响和追光（并行共享冲突点）
        self.svc.declare_equipment(JIANGSU, "p-js", ["追光"])
        self.svc.declare_equipment(OTHER_CITY, "p-zh", ["主音响", "追光"])

    def window(self, wid: str, phase: str, start: str, end: str) -> None:
        self.svc.add_window(ORG, "show-0926", wid, phase, start, end)

    def ready(self, program_id: str, partner: Actor, equipment: list[str], hold_window: str, now: str) -> None:
        """把一个节目走到「到场+设备确认+锁定」齐备。"""
        rev = self.svc._state["programs"][program_id]["current_revision"]
        self.svc.confirm_attendance(partner, f"rcp-{program_id}", program_id, rev, 12, "准时到场", now)
        self.svc.declare_equipment(partner, program_id, equipment)
        self.svc.confirm_equipment(STAGE, program_id, now)
        self.svc.lock_resources(STAGE, "show-0926", hold_window, program_id, equipment, now)


class PermissionTests(ServiceTestBase):
    def test_partner_only_owns_city_facts(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            OTHER_CITY  # noqa: B018 - 保留阅读性
            self.svc.confirm_attendance(OTHER_CITY, "rcp-x", "p-js", 1, 10, "", "2026-09-25T18:00:00+08:00")
        self.assertEqual(ctx.exception.code, "permission_denied")
        result = self.svc.confirm_attendance(JIANGSU, "rcp-js", "p-js", 1, 10, "", "2026-09-25T18:00:00+08:00")
        self.assertFalse(result["replayed"])

    def test_only_stage_manager_confirms_equipment(self) -> None:
        self.svc.declare_equipment(JIANGSU, "p-js", ["追光"])
        with self.assertRaises(ServiceError):
            self.svc.confirm_equipment(ORG, "p-js", "2026-09-25T18:00:00+08:00")
        self.svc.confirm_equipment(STAGE, "p-js", "2026-09-25T18:00:00+08:00")

    def test_only_organizer_freezes_order(self) -> None:
        with self.assertRaises(ServiceError):
            self.svc.freeze_order(STAGE, "show-0926", "2026-09-25T19:00:00+08:00")
        self.svc.freeze_order(ORG, "show-0926", "2026-09-25T19:00:00+08:00")


class RevisionAndSubstitutionTests(ServiceTestBase):
    def test_revision_chain_and_event(self) -> None:
        out = self.svc.revise_program(JIANGSU, "p-js", "江苏推介·精简版", "压缩为8分钟", "航班延误", "2026-09-25T17:00:00+08:00")
        self.assertEqual((out["revision"], out["supersedes"]), (2, 1))
        event = self.svc.events()[-1]
        self.assertEqual(event["event_type"], "PROGRAM_CHANGED")
        self.assertEqual(event["payload"]["reason"], "航班延误")

    def test_substitution_keeps_snapshot_and_reason(self) -> None:
        record = self.svc.substitute(
            ORG, "show-0926", "p-zh", "p-backup", "合作城市临时换演出内容", "2026-09-25T17:30:00+08:00"
        )
        self.assertEqual(record["out_snapshot"]["title"], "珠海民俗展演")
        self.assertEqual(record["out_revision"], 1)
        self.assertEqual(self.svc._state["sessions"]["show-0926"]["order"], ["p-js", "p-backup"])
        board = self.svc.session_board("show-0926", "2026-09-25T18:00:00+08:00")
        self.assertEqual(board["substitutions"][0]["reason"], "合作城市临时换演出内容")

    def test_performed_program_cannot_be_revised(self) -> None:
        self.svc.record_performance(STAGE, "p-js", "2026-09-25T20:00:00+08:00")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.revise_program(JIANGSU, "p-js", "改名", "", "想改", "2026-09-25T21:00:00+08:00")
        self.assertEqual(ctx.exception.code, "performed_immutable")

    def test_frozen_order_rejects_substitution(self) -> None:
        self.svc.freeze_order(ORG, "show-0926", "2026-09-25T19:00:00+08:00")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.substitute(ORG, "show-0926", "p-zh", "p-backup", "来不及了", "2026-09-25T19:30:00+08:00")
        self.assertEqual(ctx.exception.code, "already_frozen")


class AttendanceTests(ServiceTestBase):
    NOW = "2026-09-25T18:00:00+08:00"

    def test_same_receipt_replays(self) -> None:
        first = self.svc.confirm_attendance(JIANGSU, "rcp-1", "p-js", 1, 12, "已到", self.NOW)
        second = self.svc.confirm_attendance(JIANGSU, "rcp-1", "p-js", 1, 12, "已到", self.NOW)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(len([e for e in self.svc.events() if e["event_type"] == "ATTENDANCE_CONFIRMED"]), 1)

    def test_content_drift_requires_reconfirm(self) -> None:
        self.svc.confirm_attendance(JIANGSU, "rcp-1", "p-js", 1, 12, "已到", self.NOW)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.confirm_attendance(JIANGSU, "rcp-1", "p-js", 1, 8, "人数变了", self.NOW)
        self.assertEqual(ctx.exception.code, "drift_detected")
        # 原回执保持争议状态，必须用新回执重新确认
        with self.assertRaises(ServiceError):
            self.svc.confirm_attendance(JIANGSU, "rcp-1", "p-js", 1, 8, "人数变了", self.NOW)
        fresh = self.svc.confirm_attendance(JIANGSU, "rcp-2", "p-js", 1, 8, "人数变了", self.NOW)
        self.assertFalse(fresh["replayed"])
        # 两次漂移尝试（变更人数、重放争议回执）都记入审计
        self.assertEqual([c["kind"] for c in self.svc.conflict_log()], ["attendance_drift", "attendance_drift"])

    def test_new_revision_invalidates_old_confirmation_on_board(self) -> None:
        self.svc.confirm_attendance(JIANGSU, "rcp-1", "p-js", 1, 12, "已到", self.NOW)
        self.svc.revise_program(JIANGSU, "p-js", "江苏二版", "", "编排调整", self.NOW)
        board = self.svc.session_board("show-0926", self.NOW)
        kinds = {(m["program_id"], m["revision"], m["kind"]) for m in board["missing_confirmations"]}
        self.assertIn(("p-js", 2, "attendance"), kinds)


class ResourceLockTests(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.window("w1", "performance", "2026-09-25T19:00:00+08:00", "2026-09-25T20:00:00+08:00")
        self.window("w2", "performance", "2026-09-25T19:30:00+08:00", "2026-09-25T20:30:00+08:00")

    def test_parallel_programs_sharing_device_conflict_is_atomic(self) -> None:
        ok = self.svc.lock_resources(STAGE, "show-0926", "w1", "p-js", ["主音响"], "2026-09-25T18:00:00+08:00")
        self.assertFalse(ok["replayed"])
        with self.assertRaises(ServiceError) as ctx:
            # 主音响冲突；即使追光空闲，也不能只锁住追光
            self.svc.lock_resources(STAGE, "show-0926", "w2", "p-zh", ["追光", "主音响"], "2026-09-25T18:05:00+08:00")
        self.assertEqual(ctx.exception.code, "resource_conflict")
        self.assertEqual({c["resource"] for c in ctx.exception.details["conflicts"]}, {"主音响"})
        held = {r for h in self.svc._state["holds"].values() if h["program_id"] == "p-zh" for r in h["resources"]}
        self.assertEqual(held, set())

    def test_non_overlapping_windows_can_share(self) -> None:
        self.svc.lock_resources(STAGE, "show-0926", "w1", "p-js", ["主音响"], "2026-09-25T18:00:00+08:00")
        self.window("w3", "performance", "2026-09-25T20:00:00+08:00", "2026-09-25T21:00:00+08:00")
        second = self.svc.lock_resources(STAGE, "show-0926", "w3", "p-zh", ["主音响"], "2026-09-25T18:00:00+08:00")
        self.assertFalse(second["replayed"])

    def test_same_request_replays_without_duplicate_hold(self) -> None:
        self.svc.lock_resources(STAGE, "show-0926", "w1", "p-js", ["主音响"], "2026-09-25T18:00:00+08:00")
        again = self.svc.lock_resources(STAGE, "show-0926", "w1", "p-js", ["主音响"], "2026-09-25T18:01:00+08:00")
        self.assertTrue(again["replayed"])
        holds = [h for h in self.svc._state["holds"].values() if h["program_id"] == "p-js"]
        self.assertEqual(len(holds), 1)


class TimeAndRestartTests(ServiceTestBase):
    def test_cross_midnight_window_uses_venue_local_date(self) -> None:
        # 北京时间 23:30 到次日 00:30，按香港本地日期归在开场当天
        self.window("w-night", "performance", "2026-09-25T23:30:00+08:00", "2026-09-26T00:30:00+08:00")
        self.assertEqual(self.svc.window_local_date("w-night"), "2026-09-25")
        board = self.svc.session_board("show-0926", "2026-09-25T20:00:00+08:00")
        self.assertEqual(board["local_date"], "2026-09-25")

    def test_past_window_stays_released_after_restart(self) -> None:
        self.window("w1", "performance", "2026-09-25T19:00:00+08:00", "2026-09-25T20:00:00+08:00")
        self.svc.lock_resources(STAGE, "show-0926", "w1", "p-js", ["主音响"], "2026-09-25T18:00:00+08:00")
        released = self.svc.sweep_expired("2026-09-25T20:30:00+08:00")
        self.assertEqual(released, ["w1"])
        # 重复清扫是幂等的
        self.assertEqual(self.svc.sweep_expired("2026-09-25T21:00:00+08:00"), [])
        # 模拟服务重启
        restarted = CommitmentService(self.path)
        self.assertEqual(restarted.sweep_expired("2026-09-25T21:30:00+08:00"), [])
        with self.assertRaises(ServiceError) as ctx:
            restarted.lock_resources(STAGE, "show-0926", "w1", "p-zh", ["主音响"], "2026-09-25T20:45:00+08:00")
        self.assertEqual(ctx.exception.code, "window_closed")
        hold = next(h for h in restarted._state["holds"].values())
        self.assertEqual(hold["status"], "released")

    def test_pending_changes_survive_restart(self) -> None:
        self.svc.open_change(DUTY, "show-0926", "嘉宾航班延误", "江苏团改高铁，到场推迟20分钟", "2026-09-25T17:00:00+08:00")
        restarted = CommitmentService(self.path)
        pending = restarted.pending_changes("show-0926")
        self.assertEqual(len(pending), 1)
        restarted.apply_change(ORG, pending[0]["change_id"], "2026-09-25T17:10:00+08:00")
        self.assertEqual(restarted.pending_changes(), [])


class BoardTests(ServiceTestBase):
    def test_board_reports_executable_order_and_missing(self) -> None:
        self.window("w1", "performance", "2026-09-25T19:00:00+08:00", "2026-09-25T21:00:00+08:00")
        now = "2026-09-25T18:30:00+08:00"
        # 一开始两个节目都缺到场、设备、锁定
        board = self.svc.session_board("show-0926", now)
        self.assertEqual(board["executable_order"], [])
        self.assertEqual({m["kind"] for m in board["missing_confirmations"]}, {"attendance", "equipment"})
        self.ready("p-js", JIANGSU, ["追光"], "w1", now)
        board = self.svc.session_board("show-0926", now)
        self.assertEqual([e["program_id"] for e in board["executable_order"]], ["p-js"])
        # 第二个节目人员、舞台确认齐备，但申报的追光被 p-js 占用
        self.svc.confirm_attendance(OTHER_CITY, "rcp-p-zh", "p-zh", 1, 8, "已到", now)
        self.svc.confirm_equipment(STAGE, "p-zh", now)
        self.svc.lock_resources(STAGE, "show-0926", "w1", "p-zh", ["主音响"], now)
        with self.assertRaises(ServiceError):
            self.svc.lock_resources(STAGE, "show-0926", "w1", "p-zh", ["追光"], now)
        board = self.svc.session_board("show-0926", now)
        self.assertIn("p-js", {c["source"] for c in board["conflicts"] if c["kind"] == "resource"})
        self.assertEqual([e["program_id"] for e in board["executable_order"]], ["p-js"])

    def test_board_after_window_compression(self) -> None:
        self.window("w1", "performance", "2026-09-25T19:00:00+08:00", "2026-09-25T21:00:00+08:00")
        self.svc.adjust_window(ORG, "w1", "2026-09-25T19:30:00+08:00", "2026-09-25T18:00:00+08:00")
        board = self.svc.session_board("show-0926", "2026-09-25T19:31:00+08:00")
        self.assertTrue(all(any(b["kind"] == "window" for b in e["blockers"]) for e in board["entries"]))

    def test_cost_commitments_listed(self) -> None:
        self.svc.commit_cost(ORG, "show-0926", "p-js", 5000, "CNY", "航班改签补贴", "2026-09-25T17:00:00+08:00")
        board = self.svc.session_board("show-0926", "2026-09-25T18:00:00+08:00")
        self.assertEqual(board["costs"][0]["amount"], 5000)
        with self.assertRaises(ServiceError):
            self.svc.commit_cost(OTHER_CITY, "show-0926", "p-js", 1, "CNY", "", "2026-09-25T17:00:00+08:00")


class OpeningNightScenarioTests(ServiceTestBase):
    """临近开场的完整链路：延误、压缩、换内容、设备撞车，看板给出答案。"""

    def test_opening_night_scenario(self) -> None:
        now = "2026-09-26T17:00:00+08:00"
        self.window("w-setup", "setup", "2026-09-26T15:00:00+08:00", "2026-09-26T18:00:00+08:00")
        self.window("w-show", "performance", "2026-09-26T20:00:00+08:00", "2026-09-26T23:30:00+08:00")
        self.window("w-out", "teardown", "2026-09-26T23:30:00+08:00", "2026-09-27T01:00:00+08:00")
        # 嘉宾航班延误：登记现场变更，值班人员稍后办结
        change = self.svc.open_change(DUTY, "show-0926", "嘉宾航班延误", "江苏团嘉宾改签，到场待定", now)
        # 场地时段压缩：演出窗口缩短半小时
        self.svc.adjust_window(ORG, "w-show", "2026-09-26T23:00:00+08:00", now)
        # 合作城市临时换演出内容：珠海节目改版，需重新确认
        self.svc.revise_program(OTHER_CITY, "p-zh", "珠海民俗展演·新版", "临时更换曲目", "合作城市临时换内容", now)
        # 江苏团一路齐备；舞台团队却把同一追光先锁给了江苏团
        self.ready("p-js", JIANGSU, ["追光"], "w-show", now)
        self.svc.confirm_attendance(OTHER_CITY, "rcp-zh", "p-zh", 2, 10, "新版已确认", now)
        self.svc.confirm_equipment(STAGE, "p-zh", now)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.lock_resources(STAGE, "show-0926", "w-show", "p-zh", ["追光"], now)
        self.assertEqual(ctx.exception.details["conflicts"][0]["held_by_program"], "p-js")
        # 看板：可执行顺序只有江苏团；珠海团缺设备锁定，冲突来源是 p-js
        board = self.svc.session_board("show-0926", "2026-09-26T19:00:00+08:00")
        self.assertEqual([e["program_id"] for e in board["executable_order"]], ["p-js"])
        self.assertEqual(board["local_date"], "2026-09-26")
        self.assertIn("p-js", {c["source"] for c in board["conflicts"]})
        self.assertEqual([c["change_id"] for c in board["pending_changes"]], [change["change_id"]])
        # 值班人员办结航班延误变更；主办方冻结最终顺序
        self.svc.apply_change(DUTY, change["change_id"], "2026-09-26T19:30:00+08:00")
        self.svc.freeze_order(ORG, "show-0926", "2026-09-26T19:35:00+08:00")
        final = self.svc.session_board("show-0926", "2026-09-26T19:40:00+08:00")
        self.assertTrue(final["frozen"])
        self.assertEqual(final["pending_changes"], [])


class EventContractTests(ServiceTestBase):
    def test_emitted_events_satisfy_contract(self) -> None:
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        self.window("w1", "performance", "2026-09-25T19:00:00+08:00", "2026-09-25T21:00:00+08:00")
        now = "2026-09-25T18:00:00+08:00"
        self.ready("p-js", JIANGSU, ["追光"], "w1", now)
        self.svc.freeze_order(ORG, "show-0926", "2026-09-25T18:30:00+08:00")
        self.svc.open_change(DUTY, "show-0926", "延时", "撤场延后", "2026-09-25T21:30:00+08:00")
        change_id = self.svc.pending_changes()[0]["change_id"]
        self.svc.close_change(ORG, change_id, "2026-09-25T21:45:00+08:00")
        for event in self.svc.events():
            self.assertEqual(validate_event(event, schema), [], msg=json.dumps(event, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
