from __future__ import annotations



from daemon.github.issues import (
    parse_dependencies,
    resolve_dependency_dag,
)


def test_parse_dependencies_from_task_list() -> None:
    body = """## Dependencies
- [ ] #12
- [ ] #13
- [x] #14
"""
    blocked_by = parse_dependencies(body)
    assert blocked_by == [12, 13, 14]


def test_parse_dependencies_empty() -> None:
    body = "## Task\nDo something"
    assert parse_dependencies(body) == []


def test_resolve_dependency_dag_picks_unblocked() -> None:
    issues = [
        {"number": 1, "body": "", "state": "open", "assignee": None,
         "labels": ["ph:ready"]},
        {"number": 2, "body": "- [ ] #1", "state": "open", "assignee": None,
         "labels": ["ph:blocked"]},
        {"number": 3, "body": "", "state": "open", "assignee": None,
         "labels": ["ph:ready"]},
    ]
    closed = set()
    ready = resolve_dependency_dag(issues, closed)
    assert [i["number"] for i in ready] == [1, 3]


def test_resolve_dependency_dag_unblocks_when_dep_closed() -> None:
    issues = [
        {"number": 2, "body": "- [ ] #1", "state": "open", "assignee": None,
         "labels": ["ph:blocked"]},
    ]
    closed = {1}
    ready = resolve_dependency_dag(issues, closed)
    assert [i["number"] for i in ready] == [2]


def test_resolve_dependency_dag_skips_assigned() -> None:
    issues = [
        {"number": 1, "body": "", "state": "open", "assignee": "bot",
         "labels": ["ph:in-progress"]},
        {"number": 2, "body": "", "state": "open", "assignee": None,
         "labels": ["ph:ready"]},
    ]
    ready = resolve_dependency_dag(issues, set())
    assert [i["number"] for i in ready] == [2]
