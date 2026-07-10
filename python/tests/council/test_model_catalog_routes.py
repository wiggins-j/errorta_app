from fastapi.testclient import TestClient

from errorta_app.server import app


def test_catalog_routes_are_ui_only_and_round_trip_overrides(tmp_errorta_home) -> None:
    headers = {"x-errorta-origin": "tauri-ui"}
    with TestClient(app) as client:
        assert client.get("/council/model-catalog").status_code == 403
        write = client.put(
            "/council/model-catalog", headers=headers,
            json={"overrides": {"custom.future": {
                "capability_tier": "strong", "cost_tier": 4,
            }}},
        )
        read = client.get("/council/model-catalog", headers=headers)
    assert write.status_code == 200
    assert read.status_code == 200
    assert read.json()["overrides"]["custom.future"]["cost_tier"] == 4
    entry = next(item for item in read.json()["entries"] if item["route_id"] == "custom.future")
    assert entry["capability_tier"] == "strong"
