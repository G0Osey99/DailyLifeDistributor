# agent/tests/test_remote_session.py
import os, json
from agent.remote_session import RemotePlaywrightSession
from agent.secrets_shim import Shim

def test_enter_writes_blob_to_tempfile_and_exit_cleans_up():
    s = Shim(initial={"rock.session": '{"cookies":[]}'})
    with RemotePlaywrightSession(s, "rock.session") as path:
        assert os.path.exists(path)
        assert json.load(open(path)) == {"cookies": []}
        held = path
    assert not os.path.exists(held)

def test_exit_emits_credentials_updated_when_contents_change():
    emitted = []
    s = Shim(initial={"rock.session": '{"v":1}'}, emit=emitted.append)
    with RemotePlaywrightSession(s, "rock.session") as path:
        open(path, "w", encoding="utf-8").write('{"v":2}')
    keys = [e["key"] for e in emitted]
    assert keys == ["rock.session"]
    assert emitted[-1]["value"] == '{"v":2}'

def test_exit_does_not_emit_when_contents_unchanged():
    emitted = []
    s = Shim(initial={"rock.session": '{"v":1}'}, emit=emitted.append)
    with RemotePlaywrightSession(s, "rock.session") as _:
        pass  # no write
    assert emitted == []
