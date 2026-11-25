"""Seed only the isolated synthetic conversational QA collection, never production."""

import json
import os
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    assert os.environ.get("ALLOW_DISPOSABLE_CONVERSATION_SEED") == "1"
    root = Path(os.environ.get("QA_OUTPUT_DIR", "/tmp/qa-output"))
    credentials = json.loads((root / "qa-credentials.json").read_text())
    assert credentials["username"].endswith("@example.invalid")
    with httpx.Client(base_url="http://assistant-api:8000/api/assistant/v1",
                      headers={"Origin": "http://localhost:8088"}, timeout=60) as app:
        login_payload = {key: credentials[key] for key in ("username", "password")}
        response = app.post("/auth/login", json=login_payload); response.raise_for_status()
        session = response.json()
        primary = session["collectionId"]
        app.headers.update({"X-CSRF-Token": session["csrfToken"], "X-Collection-ID": primary})
        initial = app.get("/items"); initial.raise_for_status()
        resuming = initial.json()["total"] == 25 and os.environ.get("RESUME_SEEDED_DISPOSABLE") == "1"
        assert initial.json()["total"] == 0 or resuming, "Seed requires empty or explicitly resumed synthetic data"
        if resuming:
            secondary = next(c["id"] for c in session["collections"] if c["name"] == "QA Empty")
        else:
            with httpx.Client(base_url="http://homebox:7745/api/v1", timeout=30) as native:
                login = native.post("/users/login", json=login_payload); login.raise_for_status()
                native.headers.update({"Authorization": login.json()["token"], "X-Tenant": primary})
                group = native.get("/groups"); group.raise_for_status()
                changed = native.put("/groups", json={"name": "QA Household", "currency": group.json()["currency"]})
                changed.raise_for_status()
                second = native.post("/groups", json={"name": "QA Empty"}); second.raise_for_status()
                secondary = second.json()["id"]

        def create(kind, payload):
            proposal = app.post("/actions", json={"type": kind, "payload": payload})
            proposal.raise_for_status()
            value = proposal.json()
            confirmed = app.post("/actions/" + value["id"] + "/confirm",
                                 json={"revision": value["revision"]},
                                 headers={"Idempotency-Key": uuid4().hex})
            confirmed.raise_for_status()
            action = confirmed.json()
            assert action["status"] == "succeeded"
            return action["result"]["id"]

        if resuming:
            existing = app.get("/locations"); existing.raise_for_status()
            paths = {"Ground floor", "Upper floor", "Workshop", "Ground floor / Entry", "Upper floor / Entry"}
            locations = {loc["path"]: loc["id"] for loc in existing.json()["locations"] if loc["path"] in paths}
            assert set(locations) == paths
        else:
            locations = {}
            for name in ("Ground floor", "Upper floor", "Workshop"):
                locations[name] = create("location.create", {"name": name})
            for parent in ("Ground floor", "Upper floor"):
                path = parent + " / Entry"
                locations[path] = create("location.create", {"name": "Entry", "parentId": locations[parent]})
        items = [
            {"name": "Portable charger", "quantity": 3, "locationId": locations["Ground floor / Entry"]},
            {"name": "USB cable", "quantity": 5, "locationId": locations["Upper floor / Entry"]},
            {"name": "Empty blue tote", "quantity": 1, "locationId": locations["Workshop"]},
        ]
        items += [{"name": f"QA inventory record {n:02d}", "quantity": 1,
                   "locationId": locations["Workshop"]} for n in range(1, 23)]
        for item in items:
            if resuming:
                found = [record for record in initial.json()["items"] if record["name"] == item["name"]]
                assert len(found) == 1
                assert found[0]["quantity"] == item["quantity"] and found[0]["locationId"] == item["locationId"]
                item["id"] = found[0]["id"]
            else:
                item["id"] = create("item.create", {k: v for k, v in item.items() if k != "id"})
        # Independent oracle: explicit records created above, not the new overview.
        expected = {item["id"] for item in items}
        current = app.get("/items"); current.raise_for_status()
        assert current.json()["total"] == len(items) == 25
        assert {item["id"] for item in current.json()["items"]} == expected
        actual_locations = app.get("/locations"); actual_locations.raise_for_status()
        actual_paths = {loc["path"]: loc["id"] for loc in actual_locations.json()["locations"]}
        # Original Homebox registration source seeds eight defaults. GroupCreate
        # does not. Count these explicitly, rather than deriving an expected total
        # from the new overview or weakening an assertion to match its result.
        defaults = {"Living Room", "Garage", "Kitchen", "Bedroom", "Bathroom", "Office", "Attic", "Basement"}
        assert set(actual_paths) == defaults | set(locations)
        assert len(actual_paths) == 8 + 5 == 13
        assert all(actual_paths[path] == identity for path, identity in locations.items())
        assert sum(item["quantity"] for item in items) == 31
        switched = app.post("/session/collection", json={"collectionId": secondary})
        switched.raise_for_status()
        state = switched.json()
        app.headers.update({"X-CSRF-Token": state["csrfToken"], "X-Collection-ID": secondary})
        empty = app.get("/items"); empty.raise_for_status()
        assert empty.json() == {"items": [], "total": 0}
        empty_locations = app.get("/locations"); empty_locations.raise_for_status()
        assert empty_locations.json()["locations"] == []
        restored = app.post("/session/collection", json={"collectionId": primary})
        restored.raise_for_status()

        oracle = {"primaryCollection": {"id": primary, "name": "QA Household", "itemRecords": 25,
                                       "locationRecords": 13, "quantitySumNotRecordCount": 31},
                  "secondaryCollection": {"id": secondary, "name": "QA Empty", "itemRecords": 0, "locationRecords": 0},
                  "locations": actual_paths, "explicitlySeededLocations": locations,
                  "registrationDefaultLocations": sorted(defaults),
                  "items": items, "emptyItemContainer": "Empty blue tote",
                  "pendingCreateExpectedName": "mobile charge"}
        (root / "fixture-oracle.json").write_text(json.dumps(oracle, indent=2) + "\n")
        os.chmod(root / "fixture-oracle.json", 0o600)
        credentials["preferredCollectionId"] = primary
        credentials["preferredCollectionName"] = "QA Household"
        (root / "qa-credentials.json").write_text(json.dumps(credentials) + "\n")
        os.chmod(root / "qa-credentials.json", 0o600)
        print(json.dumps({"seeded": True, "primaryItemRecords": 25, "primaryLocations": 13,
                          "quantitySum": 31, "secondaryItemRecords": 0, "secondaryLocations": 0,
                          "entryPaths": ["Ground floor / Entry", "Upper floor / Entry"],
                          "emptyItemContainer": "Empty blue tote", "gatewayCalls": 0}), flush=True)


if __name__ == "__main__":
    main()
