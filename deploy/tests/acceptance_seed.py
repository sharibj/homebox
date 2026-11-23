"""Create generated test accounts and verify same-origin companion sign-in."""

import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import urllib.request


credentials = Path("/acceptance/credentials.json")


def post(url, payload, host, opener=None):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Host": host,
        "Origin": "http://localhost:8088",
        "Content-Type": "application/json",
    })
    requester = opener or urllib.request.build_opener()
    with requester.open(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None


if not credentials.exists():
    users = [
        {"name": f"Acceptance {role}", "email": f"{role}-{secrets.token_hex(6)}@example.invalid",
         "password": secrets.token_urlsafe(24)}
        for role in ("owner", "other")
    ]
    for user in users:
        post("http://proxy:8089/api/v1/users/register", user, "localhost:8089")
    descriptor = {"origin": "http://localhost:8088", "homeboxOrigin": "http://localhost:8089", "users": users}
    descriptor["project"] = next(line.split("=", 1)[1] for line in Path("/acceptance/.env").read_text().splitlines()
                                 if line.startswith("ACCEPTANCE_PROJECT="))
    descriptor["instanceId"] = next(line.split("=", 1)[1] for line in Path("/acceptance/.env").read_text().splitlines()
                                    if line.startswith("HOMEBOX_INSTANCE_ID="))
    with credentials.open("x") as target:
        os.chmod(credentials, 0o600)
        json.dump(descriptor, target)

descriptor = json.loads(credentials.read_text())
user = descriptor["users"][0]
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
session = post("http://proxy:8088/api/assistant/v1/auth/login",
               {"username": user["email"], "password": user["password"]}, "localhost:8088", opener)
assert session["user"]["email"] == user["email"]
assert session["collectionId"] in {collection["id"] for collection in session["collections"]}
assert session["csrfToken"]
assert "token" not in session and "password" not in session
print("Created/verified disposable accounts and companion session. No Homebox credential returned by companion.")
