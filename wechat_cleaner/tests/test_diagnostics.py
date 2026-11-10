from io import BytesIO
from contextlib import closing
import json
import sqlite3
import time
import uuid
import zipfile

from fastapi.testclient import TestClient

from cleaner.diagnostics import Diagnostics, event
from cleaner.files import hashes
from cleaner.jobs import Jobs
from cleaner.web import create_app


def wait(jobs, identifier):
    for _ in range(300):
        value = jobs.get(identifier)
        if value["state"] != "running" and not jobs.active:
            return value
        time.sleep(.01)
    raise AssertionError("Task did not finish")


def unpack(diagnostics):
    with zipfile.ZipFile(BytesIO(diagnostics.export())) as archive:
        assert set(archive.namelist()) == {"report.json", "summary.txt"}
        assert "清简任务诊断报告" in archive.read("summary.txt").decode("utf-8-sig")
        return json.loads(archive.read("report.json"))


def test_failure_context_stage_cause_and_code_location_survive_restart(tmp_path):
    diagnostics = Diagnostics(tmp_path)
    jobs = Jobs(diagnostics)
    secret = "ab" * 32

    def fail(progress):
        progress(55, "准备更新全文索引")
        event("test_context", version="4.2.0.13", table_count=1)
        try:
            raise ValueError(secret)
        except ValueError as cause:
            raise RuntimeError("index failure") from cause

    identifier = jobs.submit("prepare", fail, {"keyword": "测试词", "account": "fixture_account"})["id"]
    value = wait(jobs, identifier)
    jobs.close()
    assert value["state"] == "failed"
    report = unpack(Diagnostics(tmp_path))
    item = report["tasks"][0]
    assert report["installation_id"] == diagnostics.installation_id
    assert item["stage"] == "update_messages_and_fts"
    assert item["context"]["keyword"] == "测试词"
    assert item["error"]["location"]["function"] == "fail"
    assert item["error"]["location"]["line"] > 0
    assert item["error"]["causes"][0]["type"] == "ValueError"
    assert secret not in json.dumps(report)
    assert report["environment"]["app_version"] == "0.3.0"


def test_completed_result_does_not_export_chat_content_keys_or_backups(tmp_path):
    diagnostics = Diagnostics(tmp_path)
    # Files elsewhere in data_dir must never be swept into the archive.
    (tmp_path / "plan.json").write_text('"private database dump"')
    jobs = Jobs(diagnostics)
    identifier = jobs.submit("search", lambda p: {"total": 1, "messages": [{"text": "message body sentinel"}],
                             "keys": {"db": "secret key sentinel"}}, {"keyword": "test"})["id"]
    assert wait(jobs, identifier)["state"] == "completed"
    jobs.close()
    report = unpack(diagnostics)
    serialized = json.dumps(report)
    assert "sentinel" not in serialized and "private database dump" not in serialized
    assert report["tasks"][0]["summary"] == {"total": 1}


def test_real_preparation_records_unsupported_fts_schema_without_writing_source(environment, tmp_path, monkeypatch):
    import cleaner.service as service_module
    service, source, group, *_ = environment
    baseline = hashes(source)
    scan = service.search({"group_ids": [group], "keyword": "草"})
    targets = [item["id"] for item in scan["messages"] if not item["blocked_reason"]]
    original = service_module.modify_copies

    def simulate_other_schema(plain, modified, selected):
        path = plain / "message/message_fts.db"
        path.unlink()
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE VIRTUAL TABLE message_fts_v4_0 USING fts5(acontent)")
            connection.execute("INSERT INTO message_fts_v4_0 VALUES('content must stay out of diagnostic')")
        return original(plain, modified, selected)

    monkeypatch.setattr(service_module, "modify_copies", simulate_other_schema)
    diagnostics = Diagnostics(tmp_path / "reports")
    jobs = Jobs(diagnostics)
    identifier = jobs.submit("prepare", lambda p: service.prepare(scan["scan_id"], targets, p))["id"]
    result = wait(jobs, identifier)
    jobs.close()
    assert result["state"] == "failed"
    assert result["diagnostic"]["code"] == "FTS_SCHEMA_UNSUPPORTED"
    assert hashes(source) == baseline
    report = unpack(diagnostics)
    events = report["tasks"][0]["events"]
    actual = next(e["details"] for e in events if e["name"] == "fts_schema")
    assert [v["name"] for v in actual["virtual_tables"]] == ["message_fts_v4_0"]
    assert len(actual["expected_virtual_tables"]) == 4
    mismatch = next(e["details"] for e in events if e["name"] == "fts_schema_mismatch")
    assert len(mismatch["missing"]) == 3 and mismatch["extra"] == []
    assert "content must stay out of diagnostic" not in json.dumps(report)
    assert not list(service.journal.root.glob("*/plain"))


def test_export_auth_and_unexpected_api_error_are_recorded(tmp_path, monkeypatch):
    import cleaner.web as web
    def fail_accounts(directory):
        raise OSError("fixture directory unavailable")
    monkeypatch.setattr(web, "accounts", fail_accounts)
    with TestClient(create_app(tmp_path), raise_server_exceptions=False) as client:
        assert client.get("/api/diagnostics/export").status_code == 403
        headers = {"X-Cleaner-Client": "1"}
        assert client.get("/api/diagnostics/export", headers={**headers, "Origin": "https://example.org"}).status_code == 403
        result = client.get("/api/accounts", headers=headers)
        assert result.status_code == 500
        assert result.json()["diagnostic_id"]
        response = client.get("/api/diagnostics/export", headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            report = json.loads(archive.read("report.json"))
        assert report["tasks"][0]["error"]["location"]["function"] == "fail_accounts"
        assert client.get("/api/diagnostics/tasks", headers=headers).json()["tasks"][0]["state"] == "failed"


def test_interrupted_and_unreadable_records_are_visible(tmp_path):
    diagnostics = Diagnostics(tmp_path)
    identifier = uuid.uuid4().hex
    diagnostics.start(identifier, "prepare")
    diagnostics.progress(identifier, 35)
    (diagnostics.root / ("task-" + uuid.uuid4().hex + ".json")).write_text("broken")
    report = unpack(Diagnostics(tmp_path))
    assert report["tasks"][0]["state"] == "interrupted"
    assert report["tasks"][0]["stage"] == "decode_and_validate"
    assert report["unreadable_records"] == 1


def test_diagnostic_disk_failure_does_not_leave_job_running(tmp_path, monkeypatch):
    diagnostics = Diagnostics(tmp_path)
    jobs = Jobs(diagnostics)
    def failed_storage(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(diagnostics, "finish", failed_storage)
    def fail(progress):
        raise RuntimeError("operation error")
    identifier = jobs.submit("prepare", fail)["id"]
    result = wait(jobs, identifier)
    assert result["state"] == "failed" and result["diagnostic"]["storage_failed"] is True
    successful = jobs.submit("search", lambda p: {"total": 1})["id"]
    assert wait(jobs, successful)["state"] == "completed"
    jobs.close()


def test_error_signature_groups_same_location_across_task_contexts(tmp_path):
    diagnostics = Diagnostics(tmp_path)
    jobs = Jobs(diagnostics)
    def fail(progress):
        raise RuntimeError("same structural failure")
    signatures = []
    for keyword in ("first", "second"):
        identifier = jobs.submit("prepare", fail, {"keyword": keyword})["id"]
        signatures.append(wait(jobs, identifier)["diagnostic"]["signature"])
    jobs.close()
    assert signatures[0] == signatures[1]


def test_failed_fts_open_closes_connection_before_plaintext_cleanup(tmp_path):
    import apsw
    from cleaner.database import open_fts
    path = tmp_path / "unsupported.db"
    connection = apsw.Connection(str(path))
    connection.execute("CREATE VIRTUAL TABLE message_fts_v4_0 USING fts5(unexpected_column)")
    connection.execute("INSERT INTO message_fts_v4_0 VALUES('sample token')")
    connection.close()
    try:
        open_fts(path)
    except RuntimeError as error:
        assert "Unexpected indexed column" in str(error)
    else:
        raise AssertionError("Unknown indexed columns must be rejected")
    # On Windows this fails if setup left an APSW connection open.
    path.unlink()


def test_file_install_and_restore_events_include_verified_plan(environment, tmp_path):
    service, source, group, *_ = environment
    baseline = hashes(source)
    scan = service.search({"group_ids": [group], "keyword": "草"})
    targets = [item["id"] for item in scan["messages"] if not item["blocked_reason"]]
    plan = service.prepare(scan["scan_id"], targets)
    diagnostics = Diagnostics(tmp_path / "reports")
    jobs = Jobs(diagnostics)
    install = jobs.submit("execute", lambda p: service.execute(plan["id"], p))["id"]
    assert wait(jobs, install)["state"] == "completed"
    restore = jobs.submit("restore", lambda p: service.restore(plan["id"], False, p))["id"]
    assert wait(jobs, restore)["state"] == "completed"
    jobs.close()
    report = unpack(diagnostics)
    records = {item["kind"]: item for item in report["tasks"]}
    assert any(e["name"] == "file_installed" for e in records["execute"]["events"])
    assert any(e["name"] == "file_restored" for e in records["restore"]["events"])
    assert any(e["name"] == "verified_plan" for e in records["execute"]["events"])
    assert hashes(source) == baseline
