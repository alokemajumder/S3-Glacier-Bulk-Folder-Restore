from __future__ import annotations

import threading

from s3_glacier_restore.state import NullState, StateFile


def test_roundtrip(tmp_path):
    path = str(tmp_path / "a.state")
    with StateFile(path) as state:
        state.record("one")
        state.record("two")
        state.record("one")  # duplicate is a no-op
    assert len(state) == 2

    reloaded = StateFile(path)
    assert reloaded.load() == 2
    assert reloaded.contains("one")
    assert not reloaded.contains("three")


def test_header_and_comments_ignored_on_reload(tmp_path):
    path = tmp_path / "b.state"
    path.write_text("# a comment\n\nkey-one\nkey-two\n", encoding="utf-8")
    state = StateFile(str(path))
    assert state.load() == 2
    assert state.contains("key-one")


def test_load_of_missing_file_is_empty(tmp_path):
    assert StateFile(str(tmp_path / "nope.state")).load() == 0


def test_parent_directories_are_created(tmp_path):
    path = str(tmp_path / "deep" / "nested" / "c.state")
    with StateFile(path) as state:
        state.record("x")
    assert StateFile(path).load() == 1


def test_partial_run_is_durable_without_a_clean_close(tmp_path):
    """A kill -9 must not lose everything: entries flush as they go."""
    path = str(tmp_path / "d.state")
    state = StateFile(path, flush_every=1)
    state.open()
    for i in range(10):
        state.record(f"key-{i}")
    # Deliberately no close() -- simulate an abrupt exit.
    assert StateFile(path).load() == 10
    state.close()


def test_concurrent_records_are_all_persisted(tmp_path):
    path = str(tmp_path / "e.state")
    with StateFile(path, flush_every=1) as state:

        def worker(start):
            for i in range(start, start + 200):
                state.record(f"key-{i}")

        threads = [threading.Thread(target=worker, args=(n * 200,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert StateFile(path).load() == 1600


def test_versioned_state_ids_survive(tmp_path):
    path = str(tmp_path / "f.state")
    with StateFile(path) as state:
        state.record("some/key.bin\tv1")
    reloaded = StateFile(path)
    reloaded.load()
    assert reloaded.contains("some/key.bin\tv1")
    assert not reloaded.contains("some/key.bin\tv2")


def test_null_state_is_inert():
    null = NullState()
    with null:
        null.record("anything")
    assert null.load() == 0
    assert not null.contains("anything")
    assert len(null) == 0


def test_keys_with_newlines_survive_a_reload(tmp_path):
    """S3 keys may contain newlines; a line-oriented file must still resume."""
    path = str(tmp_path / "weird.state")
    keys = ["plain.bin", "line\nbreak.bin", "back\\slash.bin", "carriage\rreturn.bin"]
    with StateFile(path) as state:
        for key in keys:
            state.record(key)

    reloaded = StateFile(path)
    assert reloaded.load() == len(keys)
    for key in keys:
        assert reloaded.contains(key), key
    assert not reloaded.contains("line\\nbreak.bin")  # the escape is not the key


def test_escaping_cannot_collide(tmp_path):
    r"""A literal '\n' in one key must not alias a real newline in another."""
    path = str(tmp_path / "collide.state")
    with StateFile(path) as state:
        state.record("a\nb")
    reloaded = StateFile(path)
    reloaded.load()
    assert reloaded.contains("a\nb")
    assert not reloaded.contains("a\\nb")
