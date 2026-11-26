"""Guided NEW-account registration through native JSON household invitations."""
import datetime as dt
import hashlib
import getpass
import json
import os
import stat
import sys
import urllib.error
import uuid
import warnings


def private_input(api, filename):
    path = api.state_file(filename)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("Automation input files must be owned mode-0600 regular files.")
    return path


def save(api, path, value):
    if path.exists():
        private_input(api, path.name)
    elif path.is_symlink():
        raise ValueError("Refusing a symlink new-account receipt.")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    with temporary.open("x") as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(value, output)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def login(api, data):
    return api.request("POST", "/users/login", {
        "username": data["username"], "password": data["password"], "stayLoggedIn": False})["token"]


def secure_credentials(api, filename, *, registering=False):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return api.credentials(filename, registering=registering)
    except getpass.GetPassWarning:
        raise ValueError("Secure hidden password input is unavailable. Use a private TTY or mode-0600 files.") from None


def new_credentials(api, filename):
    if filename:
        private_input(api, filename)
    data = secure_credentials(api, filename, registering=True)
    if (set(data) != {"name", "username", "password"} or not data["name"].strip()
            or len(data["name"]) > 255 or not data["username"].strip()
            or len(data["username"]) > 255 or "@" not in data["username"]
            or any(c.isspace() for c in data["username"]) or len(data["password"].encode()) < 6):
        raise ValueError("New-account JSON needs only a nonempty name, email username and a password of at least 6 bytes.")
    return data


def choose_household(api, actor, collection):
    path = api.STATE / "work/household-owners.json"
    if (path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600
            or path.stat().st_uid != os.getuid()):
        raise ValueError("Private read-only household ownership evidence is missing or unsafe.")
    owned = json.loads(path.read_text()).get(actor, [])
    if collection:
        selected = str(uuid.UUID(collection))
        if not any(item["id"] == selected for item in owned):
            raise ValueError("The authenticated account does not own the selected household.")
        return selected
    if not sys.stdin.isatty():
        raise ValueError("Automation requires an explicit --collection UUID; no household is inferred.")
    if not owned:
        raise ValueError("The authenticated account has no owned household.")
    print("Choose an existing household owned by this account:")
    for index, item in enumerate(owned, 1):
        # Escape terminal control characters in user-created names.
        print(str(index) + ". " + json.dumps({"id": item["id"], "name": item["name"]}, ensure_ascii=True))
    choice = input("Household number: ")
    if not choice.isdecimal() or not 1 <= int(choice) <= len(owned):
        raise ValueError("Select a household number from the displayed list.")
    return owned[int(choice) - 1]["id"]


def reconcile(api, receipt, path, data):
    try:
        token = login(api, data)
        own = api.request("GET", "/users/self", token=token)["item"]
        groups = api.request("GET", "/groups/all", token=token)
    except (api.NativeError, OSError, urllib.error.URLError, KeyError, ValueError):
        raise ValueError("A previous registration may have committed, but login/membership cannot be confirmed. "
                         "No registration was retried. Review the existing account and private new-member receipt; "
                         "do not reset its password or delete the receipt.") from None
    identifier = str(uuid.UUID(own["id"]))
    if receipt.get("userId") and receipt["userId"] != identifier:
        raise ValueError("The existing account identity differs from the recorded attempt; no account was changed.")
    if own.get("isSuperuser") is not False:
        raise ValueError("The account is not an ordinary member; review it without recreating or promoting it.")
    collection = receipt["collectionId"]
    if collection not in own["groupIds"] or not any(x["id"] == collection for x in groups):
        save(api, path, {**receipt, "state": "account-created-membership-unverified", "userId": identifier})
        raise ValueError("The account exists, but target household membership is not verified. "
                         "No duplicate account, password change or existing-account join was attempted. "
                         "The household owner must review the existing account and membership.")
    save(api, path, {**receipt, "state": "complete", "userId": identifier})
    print("Verified ordinary new member " + identifier + " in household " + collection +
          ". Registration policy, passwords, other memberships and AI-primary binding were not changed.")


def create(args, api):
    if not sys.stdin.isatty() and not (args.credentials_file and args.new_user_file and args.collection):
        raise ValueError("Use a private interactive TTY, or --credentials-file, --new-user-file and --collection.")
    if args.credentials_file:
        private_input(api, args.credentials_file)
    else:
        print("Sign in as the owner of the existing household.")
    owner = secure_credentials(api, args.credentials_file)
    owner_token = login(api, owner)
    actor = str(uuid.UUID(api.request("GET", "/users/self", token=owner_token)["item"]["id"]))
    collection = choose_household(api, actor, args.collection)
    if not args.new_user_file:
        print("Enter credentials for a NEW ordinary member. Password input is hidden.")
    data = new_credentials(api, args.new_user_file)
    account_hash = hashlib.sha256(data["username"].strip().casefold().encode()).hexdigest()
    path = api.STATE / ("new-member-" + account_hash + ".json")
    receipt = None
    if path.exists() or path.is_symlink():
        receipt = json.loads(private_input(api, path.name).read_text())
        if (receipt.get("version") != 1 or receipt.get("accountSHA256") != account_hash
                or receipt.get("collectionId") != collection or receipt.get("actorId") != actor):
            raise ValueError("This account has a different recorded creation attempt. No existing-account join is supported.")
        if receipt.get("state") not in ("prepared", "invitation-requested", "invitation-created", "invitation-denied"):
            reconcile(api, receipt, path, data)
            return
    try:
        login(api, data)
    except api.NativeError as error:
        if error.status != 401:
            raise
    else:
        raise ValueError("This account already exists. This helper creates only NEW accounts; no join or password change was attempted.")
    receipt = {"version": 1, "state": "prepared", "accountSHA256": account_hash,
               "collectionId": collection, "actorId": actor}
    save(api, path, receipt)
    save(api, path, {**receipt, "state": "invitation-requested"})
    try:
        invitation = api.request("POST", "/groups/invitations", {
            "uses": 1, "expiresAt": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)).isoformat()
        }, token=owner_token, collection=collection)
    except api.NativeError:
        save(api, path, {**receipt, "state": "invitation-denied"})
        raise
    try:
        expiry = dt.datetime.fromisoformat(invitation["expiresAt"])
        valid = (isinstance(invitation.get("token"), str) and bool(invitation["token"])
                 and expiry.utcoffset() is not None and expiry > dt.datetime.now(dt.timezone.utc))
    except (ValueError, TypeError, KeyError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("The native invitation is missing, malformed or expired. No registration was requested.")
    save(api, path, {**receipt, "state": "invitation-created"})
    receipt = {**receipt, "state": "registration-requested"}
    save(api, path, receipt)  # Durable BEFORE the only registration request.
    try:
        api.request("POST", "/users/register", {
            "name": data["name"], "email": data["username"], "password": data["password"],
            "token": invitation["token"]})
        # Native registration succeeds without a response body. Identity comes
        # only from a subsequent authenticated login and /users/self.
        receipt = {**receipt, "state": "account-created"}
        save(api, path, receipt)
    except (api.NativeError, OSError, urllib.error.URLError, KeyError, ValueError):
        # A lost reply is not proof of failure. Reconcile once without registering again.
        reconcile(api, receipt, path, data)
        return
    reconcile(api, receipt, path, data)
