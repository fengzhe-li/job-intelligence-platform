from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import multiprocessing

import pytest

from jobintel.models.submission import AnswerSnapshot, fingerprint
from jobintel.storage.locking import store_lock
from jobintel.storage.submission_store import SubmissionStore


def _append(root, index):
    SubmissionStore(root).append("test", "x", {"n": index}, event_id=str(index))


def test_immutable_answer():
    a = AnswerSnapshot("email", "example@example.com", "profile", "1")
    with pytest.raises(FrozenInstanceError):
        a.value = "changed"


def test_stable_hash():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})


@pytest.mark.parametrize("status", ["revoked", "expired"])
def test_approval_changes_append_only(tmp_path, status):
    store = SubmissionStore(tmp_path)
    store.append("approved", "a", {"package": {}}, event_id="a")
    before = (store.directory / "events.jsonl").read_bytes()
    store.append(status, "a", {"reason": "changed"}, event_id="change")
    assert (store.directory / "events.jsonl").read_bytes().startswith(before)
    assert store.approvals()["a"]["status"] == status
    assert store.events()[0]["kind"] == "approved"


def test_append_replay_and_conflicting_replay(tmp_path):
    store = SubmissionStore(tmp_path)
    store.append("test", "x", {}, event_id="e")
    store.append("test", "x", {}, event_id="e")
    assert len(store.events()) == 1
    with pytest.raises(ValueError):
        store.append("test", "x", {"different": True}, event_id="e")


def test_nested_lock(tmp_path):
    with store_lock(tmp_path):
        with store_lock(tmp_path):
            _append(tmp_path, 1)


def test_thread_concurrency(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: _append(tmp_path, i), range(20)))
    assert len(SubmissionStore(tmp_path).events()) == 20


def test_process_concurrency(tmp_path):
    ctx = multiprocessing.get_context("fork")
    workers = [ctx.Process(target=_append, args=(tmp_path, i)) for i in range(4)]
    for p in workers:
        p.start()
    for p in workers:
        p.join(5)
        assert p.exitcode == 0
    assert len(SubmissionStore(tmp_path).events()) == 4


@pytest.mark.parametrize("tamper", [b'broken', b'{"bad":true}\n'])
def test_damaged_journal_fails_closed_without_rewriting(tmp_path, tamper):
    store = SubmissionStore(tmp_path)
    _append(tmp_path, 1)
    path = store.directory / "events.jsonl"
    with path.open("ab") as handle:
        handle.write(tamper)
    before = path.read_bytes()
    with pytest.raises((ValueError, KeyError)):
        store.events()
    assert path.read_bytes() == before
