import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ebaseline import crud, executor, runtime, schemas
from ebaseline.agent_client import AgentResult
from ebaseline.config import load_config
from ebaseline.metrics import result_summary
from ebaseline.models import Base


def setUpModule():
    load_config(Path(__file__).resolve().parent.parent / "env.conf.example")


def _agent_result(success: bool) -> AgentResult:
    return AgentResult(
        success=success,
        output="done" if success else "",
        error=None if success else "agent failed",
        duration_sec=0.05,
        rounds=1,
        tokens={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
        usage_seen=True,
        tool_calls=0,
        first_token_ms=10,
    )


class ExecutorFlowTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.workspace = self.tmp_path / "workspaces" / "group_1"

        self.group = crud.create_task_group(
            self.db, schemas.TaskGroupCreate(
                name="g", variables={"DB_PASSWORD": "s3cr3t", "WHO": "world"}))
        self.subject = crud.create_subject(
            self.db, schemas.SubjectCreate(
                name="agent", ipaddr="127.0.0.1", port=1))

        self.prompts: list[str] = []
        self.agent_success = True

        def fake_agent(**kwargs):
            self.prompts.append(kwargs["input_text"])
            return _agent_result(self.agent_success)

        self.patches = [
            mock.patch.object(executor, "_get_session_factory", lambda: self.Session),
            mock.patch.object(executor, "run_agent", side_effect=fake_agent),
            mock.patch.object(runtime, "RUNTIME_ROOT", self.tmp_path / "runtime"),
            mock.patch(
                "ebaseline.routers.workspace._workspace_dir",
                return_value=self.workspace),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.db.close()
        self.tmp.cleanup()

    def _run(self, batch_id):
        executor._run_batch_sync(batch_id, threading.Event())
        self.db.expire_all()

    def test_manifest_frozen_and_attempts_appended(self):
        crud.create_task(
            self.db, self.group.id,
            schemas.TaskCreate(
                tid=10001, name="t1", input={"prompt": "hi {WHO}"},
                expect="bash -c 'exit 0'"))
        crud.create_task(
            self.db, self.group.id,
            schemas.TaskCreate(
                tid=10002, name="t2", depends=[10001],
                input={"prompt": "next"}))
        batch = crud.create_batch(
            self.db, self.subject.id, self.group.id)

        # 批次创建后修改任务：整批执行仍使用冻结的 prompt
        crud.update_task(
            self.db, 10001,
            schemas.TaskUpdate(input={"prompt": "edited {WHO}"}))

        self._run(batch.id)
        self.assertEqual(self.prompts, ["hi world", "next"])
        self.assertEqual(crud.get_batch(self.db, batch.id).attempt, 1)

        self._run(batch.id)
        self.assertEqual(crud.get_batch(self.db, batch.id).attempt, 2)
        self.assertEqual(
            len(crud.list_results(self.db, batch_id=batch.id, attempt=1)), 2)
        self.assertEqual(
            len(crud.list_results(self.db, batch_id=batch.id, attempt=2)), 2)

        attempt2 = crud.list_results(self.db, batch_id=batch.id, attempt=2)
        verdicts = {r.tid: result_summary(r)["verdict"] for r in attempt2}
        self.assertEqual(verdicts, {10001: "PASS", 10002: "UNVALIDATED"})
        self.assertTrue(all("s3cr3t" not in (r.process or "") for r in attempt2))

    def test_clean_failure_overrides_pass_and_partial(self):
        crud.create_task(
            self.db, self.group.id,
            schemas.TaskCreate(
                tid=10001, name="pass_clean_fail",
                input={"prompt": "ok"}, expect="bash -c 'exit 0'",
                clean="bash -c 'exit 1'"))
        crud.create_task(
            self.db, self.group.id,
            schemas.TaskCreate(
                tid=10002, name="partial",
                input={"prompt": "agentfail"}, expect="bash -c 'exit 0'"))
        crud.create_task(
            self.db, self.group.id,
            schemas.TaskCreate(
                tid=10003, name="partial_clean_fail",
                input={"prompt": "agentfail"}, expect="bash -c 'exit 0'",
                clean="bash -c 'exit 1'"))
        batch = crud.create_batch(self.db, self.subject.id, self.group.id)

        original = executor.run_agent

        def fake_agent(**kwargs):
            self.agent_success = kwargs["input_text"] != "agentfail"
            return original(**kwargs)

        with mock.patch.object(executor, "run_agent", side_effect=fake_agent):
            self._run(batch.id)

        rows = {r.tid: r for r in crud.list_results(
            self.db, batch_id=batch.id, attempt=1)}
        summaries = {tid: result_summary(r) for tid, r in rows.items()}
        self.assertEqual(summaries[10001]["verdict"], "FAIL")
        self.assertFalse(summaries[10001]["clean_success"])
        self.assertEqual(summaries[10002]["verdict"], "PARTIAL")
        self.assertTrue(summaries[10002]["clean_success"])
        self.assertEqual(summaries[10003]["verdict"], "FAIL")
        self.assertFalse(summaries[10003]["clean_success"])


if __name__ == "__main__":
    unittest.main()
