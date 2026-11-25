"""Native Homebox account API client, only inside its container network namespace.

Tokens/passwords stay in memory or explicitly selected mode-0600 state files.
No global-account or collection deletion is implemented.
"""

import argparse
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import stat
import sys
import urllib.error
import urllib.request
import uuid


STATE = Path("/state")
BASE = "http://127.0.0.1:7745/api/v1"
CLIENT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

class NativeError(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__("Native account request denied (HTTP " + str(status) + ").")


def state_file(value, *, existing=True):
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
        raise ValueError("Use a plain filename inside the protected lifecycle state directory.")
    path = STATE / relative
    if path.is_symlink():
        raise ValueError("Refusing a symlink credential/invitation file.")
    if existing:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("Credential/invitation input must be an owned mode-0600 regular file.")
    return path


def request(method, route, payload=None, *, token=None, collection=None):
    # Routes are internal constants/UUIDs. Never print request bodies, tokens or URLs.
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token if token.startswith("Bearer ") else "Bearer " + token
    if collection:
        headers["X-Tenant"] = str(uuid.UUID(collection))
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + route, data=body, method=method, headers=headers)
    try:
        with CLIENT.open(req, timeout=30) as response:
            data = response.read()
            return json.loads(data) if data else None
    except urllib.error.HTTPError as error:
        # Server bodies can reflect personal input. Print the status only.
        raise NativeError(error.code) from None


def credentials(filename, *, registering=False):
    if filename:
        data = json.loads(state_file(filename).read_text())
    else:
        if not sys.stdin.isatty():
            raise ValueError("Use an interactive TTY or --credentials-file (never password arguments/environment).")
        data = {}
        if registering:
            data["name"] = input("Display name: ")
        data["username"] = input("Email: ")
        data["password"] = getpass.getpass("Password: ")
        if registering and getpass.getpass("Repeat password: ") != data["password"]:
            raise ValueError("Passwords differ.")
    if not isinstance(data.get("username"), str) or not isinstance(data.get("password"), str):
        raise ValueError("Credentials need username and password strings.")
    if registering and not isinstance(data.get("name"), str):
        raise ValueError("New users also need a name.")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("bootstrap", "collections", "members", "invite",
                                          "accept-invite", "remove-member"))
    parser.add_argument("--credentials-file", help="Owned 0600 filename inside the private lifecycle state directory")
    parser.add_argument("--collection", help="Explicit collection UUID for scoped owner operations")
    parser.add_argument("--user", help="Exact member UUID, never an email search or wildcard")
    parser.add_argument("--output", default="invitation.json", help="New private invitation filename")
    parser.add_argument("--invitation-file", default="invitation.json")
    parser.add_argument("--confirm-revoke", action="store_true",
                        help="Acknowledge access revocation only, not deletion or logout")
    args = parser.parse_args(argv)
    if args.action in ("members", "invite", "remove-member") and not args.collection:
        parser.error("--collection is required; no default collection is inferred")
    if args.collection:
        args.collection = str(uuid.UUID(args.collection))
    if args.action == "remove-member":
        if not args.user or not args.confirm_revoke:
            parser.error("Specify --user UUID and --confirm-revoke; shared inventory is retained")
        args.user = str(uuid.UUID(args.user))
    data = credentials(args.credentials_file, registering=args.action in ("bootstrap", "accept-invite"))
    if args.action in ("bootstrap", "accept-invite"):
        payload = {"name": data["name"], "email": data["username"], "password": data["password"]}
        if args.action == "accept-invite":
            invitation = json.loads(state_file(args.invitation_file).read_text())
            payload["token"] = invitation["token"]
        attempt = STATE / "bootstrap-attempt.json"
        if args.action == "bootstrap":
            if attempt.exists() and json.loads(attempt.read_text()).get("state") in ("started", "created"):
                raise ValueError("Prior signup may already have committed. Do not create a duplicate owner.")
            if attempt.is_symlink():
                raise ValueError("Refusing a symlink bootstrap receipt.")
            attempt.write_text('{"state":"started"}\n')
            os.chmod(attempt, 0o600)
        try:
            request("POST", "/users/register", payload)
        except NativeError as error:
            if args.action == "bootstrap" and 400 <= error.status < 500:
                attempt.write_text('{"state":"rejected"}\n')
            raise
        if args.action == "bootstrap":
            attempt.write_text('{"state":"created"}\n')
        print("Native account created. Password and invitation were not logged.")
        return
    login = request("POST", "/users/login", {
        "username": data["username"], "password": data["password"], "stayLoggedIn": False})
    token = login["token"]
    if args.action == "collections":
        groups = request("GET", "/groups/all", token=token)
        print(json.dumps([{"id": item["id"], "name": item["name"]} for item in groups], indent=2))
    elif args.action == "members":
        members = request("GET", "/groups/members", token=token, collection=args.collection)
        print(json.dumps(members, indent=2))
    elif args.action == "invite":
        output = state_file(args.output, existing=False)
        # Refuse overwriting before creating a server-side invitation.
        if output.exists():
            raise ValueError("Invitation output already exists; select a new filename.")
        invitation = request("POST", "/groups/invitations", {
            "uses": 1, "expiresAt": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)).isoformat()
        }, token=token, collection=args.collection)
        with output.open("x") as stream:
            os.chmod(output, 0o600)
            json.dump({"token": invitation["token"], "collection": args.collection,
                       "expiresAt": invitation["expiresAt"], "uses": 1}, stream)
            stream.write("\n")
        print("Created a one-use, 15-minute invitation in the private output file. Registration unchanged.")
    elif args.action == "remove-member":
        members = request("GET", "/groups/members", token=token, collection=args.collection)
        if not any(member["id"] == args.user for member in members):
            raise ValueError("Target is not a member of the explicitly selected collection.")
        request("DELETE", "/groups/members/" + args.user, token=token, collection=args.collection)
        print("Selected collection access revoked. Account, other memberships and shared inventory remain; sessions are not logged out.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, urllib.error.URLError) as error:
        print("Account operation failed: " + str(error), file=sys.stderr)
        raise SystemExit(1) from None
