import time

from fastapi.testclient import TestClient

from cleaner.web import create_app


def test_local_api_requires_session_token_and_rejects_foreign_origin(tmp_path):
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert client.get("/api/session").status_code == 403
        headers = {"X-Cleaner-Client":"1"}
        session = client.get("/api/session", headers=headers).json()
        assert session["demo"] is True
        body = {"account":"demo_account"}
        assert client.post("/api/connect", headers=headers, json=body).status_code == 403
        headers["X-Cleaner-Token"] = session["token"]
        assert client.post("/api/connect", headers={**headers,"Origin":"https://elsewhere.example"}, json=body).status_code == 403
        assert client.post("/api/connect", headers={**headers,"Host":"attacker.example"}, json=body).status_code == 400
        job = client.post("/api/connect", headers=headers, json=body).json()
        for _ in range(100):
            result = client.get("/api/jobs/"+job["id"], headers=headers).json()
            if result["state"] != "running":
                break
            time.sleep(.01)
        assert result["state"] == "completed"
        assert len(client.get("/api/groups", headers=headers).json()) == 3
        invalid = client.post("/api/search", headers=headers, json={"group_ids":[],"keyword":"草"})
        assert invalid.status_code == 422
        assert "input" not in invalid.text


def test_real_service_through_api_uses_encrypted_fixture(environment, tmp_path):
    service, source, group, *_ = environment
    with TestClient(create_app(tmp_path, service=service)) as client:
        headers = {"X-Cleaner-Client":"1"}
        headers["X-Cleaner-Token"] = client.get("/api/session",headers=headers).json()["token"]
        job = client.post("/api/search", headers=headers, json={"group_ids":[group],"keyword":"草"}).json()
        for _ in range(200):
            result = client.get("/api/jobs/"+job["id"],headers=headers).json()
            if result["state"] != "running":
                break
            time.sleep(.01)
        assert result["state"] == "completed"
        assert result["result"]["eligible"] == 1


def test_contact_and_mixed_scope_api(environment, tmp_path):
    service, source, group, *_ = environment
    with TestClient(create_app(tmp_path, service=service)) as client:
        headers = {"X-Cleaner-Client":"1"}
        headers["X-Cleaner-Token"] = client.get("/api/session",headers=headers).json()["token"]
        contacts = client.get("/api/contacts",headers=headers).json()
        assert {c["id"] for c in contacts} == {"wxid_sender", "wxid_friend"}
        conversations = client.get("/api/conversations",headers=headers).json()
        assert len(conversations) == 4
        response = client.post("/api/search",headers=headers,json={"conversation_ids":[group,"wxid_friend"],"keyword":"草"})
        assert response.status_code == 200
        identifier = response.json()["id"]
        for _ in range(200):
            result = client.get("/api/jobs/"+identifier,headers=headers).json()
            if result["state"] != "running":
                break
            time.sleep(.01)
        assert result["state"] == "completed"
        assert result["result"]["total"] == 5
        assert {m["conversation_type"] for m in result["result"]["messages"]} == {"contact","group"}
        conflict = client.post("/api/search",headers=headers,json={"conversation_ids":[group],"group_ids":[group],"keyword":"草"})
        assert conflict.status_code == 422
