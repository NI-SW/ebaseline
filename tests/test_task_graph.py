from types import SimpleNamespace
import unittest

from ebaseline import task_graph


def task(tid: int, depends=None, gid: int = 1, name: str | None = None):
    return SimpleNamespace(
        tid=tid, gid=gid, depends=depends, name=name or f"task-{tid}")


class ParseDependsTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(task_graph.parse_depends(None), [])
        self.assertEqual(task_graph.parse_depends(""), [])
        self.assertEqual(task_graph.parse_depends([]), [])

    def test_string_and_list(self):
        self.assertEqual(task_graph.parse_depends("1, 2,3"), [1, 2, 3])
        self.assertEqual(task_graph.parse_depends([4, "5"]), [4, 5])

    def test_invalid(self):
        with self.assertRaises(task_graph.DependencyError):
            task_graph.parse_depends("1,abc")


class ValidateTasksTest(unittest.TestCase):
    def test_valid_dag(self):
        tasks = [task(1, None), task(2, "1"), task(3, "1,2")]
        self.assertEqual(task_graph.validate_tasks(tasks), [])
        ordered = [t.tid for t in task_graph.topological_sort(tasks)]
        self.assertEqual(ordered, [1, 2, 3])

    def test_missing_dependency(self):
        issues = task_graph.validate_tasks([task(1, "99")])
        self.assertTrue(any("99" in x and "不在本组" in x for x in issues))

    def test_self_dependency(self):
        issues = task_graph.validate_tasks([task(1, "1")])
        self.assertTrue(any("依赖自身" in x for x in issues))

    def test_cross_group(self):
        tasks = [task(1, None, gid=1), task(2, "1", gid=2)]
        issues = task_graph.validate_tasks(tasks)
        self.assertTrue(any("其他组" in x for x in issues))

    def test_cycle_reports_path(self):
        tasks = [task(1, "3"), task(2, "1"), task(3, "2")]
        issues = task_graph.validate_tasks(tasks)
        self.assertTrue(any("循环依赖" in x for x in issues))
        self.assertTrue(any("1" in x and "2" in x and "3" in x for x in issues))
        with self.assertRaises(task_graph.DependencyError):
            task_graph.topological_sort(tasks)

    def test_depends_to_str(self):
        self.assertIsNone(task_graph.depends_to_str(None))
        self.assertIsNone(task_graph.depends_to_str([]))
        self.assertEqual(task_graph.depends_to_str([1, 2]), "1,2")


if __name__ == "__main__":
    unittest.main()
