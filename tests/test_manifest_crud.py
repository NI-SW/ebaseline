import json
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ebaseline import crud, manifest as manifest_mod, schemas, task_graph
from ebaseline.config import load_config
from ebaseline.models import Base


def setUpModule():
    # manifest 解析脚本路径和组变量占位符时需要配置（不会连接数据库）
    load_config(Path(__file__).resolve().parent.parent / "env.conf.example")


class ManifestAndCrudTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.group = crud.create_task_group(
            self.db,
            schemas.TaskGroupCreate(
                name="g1",
                variables={"USER": "u", "DB_PASSWORD": "secret123"},
            ),
        )
        self.subject = crud.create_subject(
            self.db,
            schemas.SubjectCreate(
                name="agent", ipaddr="127.0.0.1", port=9999, secret="k"),
        )
        self.t1 = crud.create_task(
            self.db, self.group.id,
            schemas.TaskCreate(tid=10001, name="t1", input={"prompt": "hi"}))
        self.t2 = crud.create_task(
            self.db, self.group.id,
            schemas.TaskCreate(
                tid=10002, name="t2", depends=[10001],
                input={"prompt": "next"}, expect="check.py"))

    def tearDown(self):
        self.db.close()

    def test_manifest_snapshot(self):
        batch = crud.create_batch(self.db, self.subject.id, self.group.id)
        self.assertEqual(batch.attempt, 0)
        manifest = manifest_mod.parse_manifest(batch.manifest)
        self.assertEqual(manifest["manifest_version"], manifest_mod.MANIFEST_VERSION)
        self.assertEqual(len(manifest["tasks"]), 2)
        self.assertEqual(manifest["group"]["variables"]["DB_PASSWORD"], "secret123")

        frozen = manifest_mod.tasks_from_manifest(manifest)
        self.assertEqual([t.tid for t in frozen], [10001, 10002])
        self.assertEqual(frozen[1].depends, "10001")
        self.assertEqual(
            manifest_mod.manifest_task_signature(manifest, 10002),
            manifest_mod.task_signature(self.t2))

        summary = manifest_mod.manifest_summary(manifest)
        self.assertEqual(summary["variables"]["DB_PASSWORD"], "********")
        self.assertEqual(summary["variables"]["USER"], "u")

    def test_attempt_history_is_appended(self):
        batch = crud.create_batch(self.db, self.subject.id, self.group.id)
        attempt = crud.increment_batch_attempt(self.db, batch.id)
        self.assertEqual(attempt, 1)
        crud.create_result(
            self.db, self.subject.id, self.group.id, 10001, 1, "old",
            batch_id=batch.id, result_detail=2, attempt=0)
        crud.create_result(
            self.db, self.subject.id, self.group.id, 10001, 1, "new",
            batch_id=batch.id, result_detail=3, attempt=1)
        self.assertEqual(
            len(crud.list_results(self.db, batch_id=batch.id, attempt=1)), 1)
        self.assertEqual(len(crud.list_results(self.db, batch_id=batch.id)), 2)
        crashed = crud.create_result(
            self.db, self.subject.id, self.group.id, 10002, 3, "running",
            batch_id=batch.id, attempt=1)
        self.assertEqual(crashed.attempt, 1)

    def test_attempt_zero_includes_legacy_null(self):
        batch = crud.create_batch(self.db, self.subject.id, self.group.id)
        crud.create_result(
            self.db, self.subject.id, self.group.id, 10001, 1, "legacy",
            batch_id=batch.id, result_detail=2, attempt=None)
        crud.create_result(
            self.db, self.subject.id, self.group.id, 10002, 1, "zero",
            batch_id=batch.id, result_detail=2, attempt=0)
        crud.create_result(
            self.db, self.subject.id, self.group.id, 10001, 1, "one",
            batch_id=batch.id, result_detail=2, attempt=1)
        self.assertEqual(
            len(crud.list_results(self.db, batch_id=batch.id, attempt=0)), 2)
        self.assertEqual(
            len(crud.list_results(self.db, batch_id=batch.id, attempt=1)), 1)
        self.assertIsNotNone(crud.get_batch_task_result(
            self.db, batch.id, 10001, attempt=0))
        self.assertEqual(crud.get_batch_task_result(
            self.db, batch.id, 10001, attempt=1).process, "one")

    def test_missing_dependency_rejected(self):
        with self.assertRaises(task_graph.DependencyError):
            crud.create_task(
                self.db, self.group.id,
                schemas.TaskCreate(tid=10003, name="bad", depends=[99999]))

    def test_cycle_rejected_on_update(self):
        t3 = crud.create_task(
            self.db, self.group.id,
            schemas.TaskCreate(tid=10003, name="t3"))
        crud.create_task(
            self.db, self.group.id,
            schemas.TaskCreate(tid=10004, name="t4", depends=[10003]))
        with self.assertRaises(task_graph.DependencyError):
            crud.update_task(
                self.db, t3.tid, schemas.TaskUpdate(depends=[10004]))

    def test_explicit_null_clears_fields(self):
        task = crud.create_task(
            self.db, self.group.id,
            schemas.TaskCreate(
                tid=10005, name="t5", description="desc",
                depends=[10001], pre_script="pre.py",
                input={"prompt": "x"}, expect="check.py", clean="clean.py"))
        updated = crud.update_task(
            self.db, task.tid,
            schemas.TaskUpdate(
                description=None, depends=None, pre_script=None,
                input=None, expect=None, clean=None))
        self.assertIsNone(updated.description)
        self.assertIsNone(updated.depends)
        self.assertIsNone(updated.pre_script)
        self.assertIsNone(updated.input)
        self.assertIsNone(updated.expect)
        self.assertIsNone(updated.clean)

    def test_group_secret_merge(self):
        updated = crud.update_task_group(
            self.db, self.group.id,
            schemas.TaskGroupUpdate(
                variables={"USER": "u2", "DB_PASSWORD": "********"}))
        variables = json.loads(updated.variables)
        self.assertEqual(variables["USER"], "u2")
        self.assertEqual(variables["DB_PASSWORD"], "secret123")


if __name__ == "__main__":
    unittest.main()
