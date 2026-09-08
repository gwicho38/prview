"""Tests for prview.reviews — the review runner. No claude process is ever
spawned; the Popen-driving paths are exercised through finish_run and _drain."""
import prview.core as core
from prview import reviews, state_store
from prview.findings import Finding, Placement


def _run(**kw) -> reviews.ReviewRun:
    return reviews.ReviewRun(id="x", owner="o", repo="r", number=1,
                             skill="pr-review", scope="file", **kw)


# --- the agent's posture ------------------------------------------------------

def test_the_agent_runs_read_only_and_without_skipping_permissions():
    argv = reviews.claude_argv("review this")
    assert argv[0] == "claude"
    assert "--dangerously-skip-permissions" not in argv
    allowed = argv[argv.index("--allowedTools") + 1]
    assert set(allowed.split(",")) == {"Read", "Grep", "Glob"}
    denied = argv[argv.index("--disallowedTools") + 1]
    assert {"Bash", "Write", "Edit"} <= set(denied.split(","))


def test_the_prompt_names_the_scope_and_forbids_posting():
    prompt = reviews.build_prompt("pr-review", ["a.py", "b.py"], "")
    assert "pr-review" in prompt
    assert "- a.py" in prompt and "- b.py" in prompt
    assert "Do not attempt to post" in prompt
    assert '"severity"' in prompt


# --- staging ------------------------------------------------------------------

def test_staging_writes_each_tier_to_its_own_home(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    state_store.reset_locks()
    pairs = [
        (Placement("anchored", "a.py", 11, None, None),
         Finding("a.py", 11, None, "fix", "boom", None)),
        (Placement("file", "a.py", None, None, "line 99 is not in the diff for a.py"),
         Finding("a.py", 99, None, None, "elsewhere", None)),
        (Placement("pr", None, None, None, None),
         Finding(None, None, None, "consider", "no tests", None)),
    ]
    counts = reviews.stage_placements("o", "r", 1, "run1", "pr-review", pairs)
    assert counts == {"anchored": 1, "file": 1, "pr": 1}

    st = core.load_review_state("o", "r", 1)
    threads = st["comment_threads"]["a.py"]
    assert [c["line"] for c in threads] == [11, None]
    assert all(c["staged"] and c["source"] == "pr-review" for c in threads)
    assert st["review_notes"][0]["text"] == "no tests"


def test_staging_never_increments_the_posted_comment_count(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    state_store.reset_locks()
    reviews.stage_placements("o", "r", 2, "run1", "pr-review", [
        (Placement("anchored", "a.py", 1, None, None),
         Finding("a.py", 1, None, "fix", "boom", None)),
    ])
    assert core.load_review_state("o", "r", 2)["comments"] == 0


def test_a_demoted_finding_records_why_in_its_body(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    state_store.reset_locks()
    reviews.stage_placements("o", "r", 3, "run1", "pr-review", [
        (Placement("file", "a.py", None, None, "line 99 is not in the diff for a.py"),
         Finding("a.py", 99, None, None, "boom", None)),
    ])
    body = core.load_review_state("o", "r", 3)["comment_threads"]["a.py"][0]["text"]
    assert "boom" in body
    assert "line 99" in body


def test_every_staged_record_gets_its_own_id(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    state_store.reset_locks()
    reviews.stage_placements("o", "r", 4, "run1", "pr-review", [
        (Placement("anchored", "a.py", 1, None, None),
         Finding("a.py", 1, None, None, "one", None)),
        (Placement("anchored", "a.py", 1, None, None),
         Finding("a.py", 1, None, None, "two", None)),
    ])
    ids = [c["id"] for c in core.load_review_state("o", "r", 4)["comment_threads"]["a.py"]]
    assert len(set(ids)) == 2


def test_staging_leaves_a_hand_written_comment_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    state_store.reset_locks()
    core.save_review_state("o", "r", 5, {
        "comment_threads": {"a.py": [{"text": "mine", "line": 2}]},
    })
    reviews.stage_placements("o", "r", 5, "run1", "pr-review", [
        (Placement("anchored", "a.py", 1, None, None),
         Finding("a.py", 1, None, None, "theirs", None)),
    ])
    threads = core.load_review_state("o", "r", 5)["comment_threads"]["a.py"]
    assert [c["text"] for c in threads] == ["mine", "theirs"]
    assert threads[0].get("staged") is None


# --- terminal status ----------------------------------------------------------

def test_a_run_that_produced_no_parseable_findings_is_an_error():
    run = _run()
    reviews.finish_run(run, returncode=0, output="I reviewed it. Looks fine.")
    assert run.status == "error"
    assert "no findings" in run.error.lower()


def test_a_run_with_findings_and_a_clean_exit_is_done():
    run = _run(staged=3)
    reviews.finish_run(run, returncode=0, output='{"path":"a.py","body":"b"}')
    assert run.status == "done"


def test_a_nonzero_exit_is_an_error_even_with_findings():
    run = _run(staged=3)
    reviews.finish_run(run, returncode=1, output="")
    assert run.status == "error" and "exited 1" in run.error


def test_a_cancelled_run_keeps_what_it_already_staged():
    run = _run(staged=2)
    run.status = "cancelled"
    reviews.finish_run(run, returncode=-9, output="")
    assert run.status == "cancelled" and run.staged == 2


# --- clearing a run ----------------------------------------------------------

def test_clearing_a_run_removes_only_its_own_records(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    state_store.reset_locks()
    core.save_review_state("o", "r", 6, {
        "comment_threads": {"a.py": [
            {"text": "mine", "line": 2},
            {"text": "run1", "line": 3, "staged": True, "id": "d1", "run_id": "run1"},
            {"text": "run2", "line": 4, "staged": True, "id": "d2", "run_id": "run2"},
        ]},
        "review_notes": [{"text": "n1", "id": "n1", "run_id": "run1"}],
    })
    removed = reviews.clear_run("o", "r", 6, "run1")
    assert removed == 2
    st = core.load_review_state("o", "r", 6)
    assert [c["text"] for c in st["comment_threads"]["a.py"]] == ["mine", "run2"]
    assert st["review_notes"] == []


def test_clearing_the_last_record_for_a_file_drops_the_empty_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    state_store.reset_locks()
    core.save_review_state("o", "r", 7, {
        "comment_threads": {"a.py": [
            {"text": "run1", "line": 3, "staged": True, "id": "d1", "run_id": "run1"},
        ]},
    })
    reviews.clear_run("o", "r", 7, "run1")
    assert core.load_review_state("o", "r", 7)["comment_threads"] == {}


# --- one run per PR ----------------------------------------------------------

def test_a_running_run_is_reported_as_active(monkeypatch):
    monkeypatch.setattr(reviews, "_runs", {})
    run = reviews.ReviewRun(id="live", owner="o", repo="r", number=9,
                            skill="pr-review", scope="file")
    reviews._runs["live"] = run
    assert reviews.active_run_for("o", "r", 9) == "live"
    run.status = "done"
    assert reviews.active_run_for("o", "r", 9) is None


def test_an_unknown_run_snapshots_as_none():
    assert reviews.get_run("nope") is None


def test_cancelling_an_unknown_run_is_false():
    assert reviews.cancel_run("nope") is False


# --- the streaming drain ------------------------------------------------------

class _DeadProc:
    """A Popen stand-in that has already exited."""

    def poll(self):
        return 0


def test_the_drain_stages_findings_from_the_log(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    state_store.reset_locks()
    log = tmp_path / "run.log"
    log.write_text(
        "Reading the diff.\n"
        '{"path": "a.py", "line": 10, "body": "first", "severity": "fix"}\n'
        '{"path": "a.py", "line": 11, "body": "second"}\n'
    )
    run = _run()
    run.logfile = str(log)
    run._proc = _DeadProc()
    reviews._drain(run, {"a.py": [10, 11]})
    assert run.staged == 2 and run.dropped == 0 and run.demoted == 0
    texts = [c["text"] for c in
             core.load_review_state("o", "r", 1)["comment_threads"]["a.py"]]
    assert texts == ["first", "second"]


def test_the_drain_stages_a_final_line_with_no_trailing_newline(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    state_store.reset_locks()
    log = tmp_path / "run.log"
    log.write_text('{"path": "a.py", "line": 10, "body": "last one"}')
    run = _run()
    run.logfile = str(log)
    run._proc = _DeadProc()
    reviews._drain(run, {"a.py": [10]})
    assert run.staged == 1


def test_the_drain_counts_a_demotion(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    state_store.reset_locks()
    log = tmp_path / "run.log"
    log.write_text('{"path": "a.py", "line": 999, "body": "wrong line"}\n')
    run = _run()
    run.logfile = str(log)
    run._proc = _DeadProc()
    reviews._drain(run, {"a.py": [10]})
    assert run.staged == 1 and run.demoted == 1
    entry = core.load_review_state("o", "r", 1)["comment_threads"]["a.py"][0]
    assert entry["line"] is None
    assert "line 999" in entry["text"]


def test_the_drain_survives_a_missing_log(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CACHE_DIR", tmp_path)
    run = _run()
    run.logfile = str(tmp_path / "never-written.log")
    run._proc = _DeadProc()
    reviews._drain(run, {})
    assert run.staged == 0
