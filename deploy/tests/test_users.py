"""Docker-only primary evidence and guided registration checks with synthetic data."""
import contextlib
import copy
import datetime as dt
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lifecycle"))
import accounts
import guided_users as guided
import primary

OWNER = "11111111-1111-4111-8111-111111111111"
MEMBER = "22222222-2222-4222-8222-222222222222"
GROUP = "33333333-3333-4333-8333-333333333333"
INSTANCE = "44444444-4444-4444-8444-444444444444"


class PrimaryEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "homebox.db"

    def database(self, rows):
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE users(id TEXT, created_at TEXT)")
            connection.executemany("INSERT INTO users VALUES (?,?)", rows)

    def test_utc_offset_and_full_nanosecond_ordering(self):
        self.database([(OWNER, "2026-11-26 01:00:00.000000002+01:00"),
                       (MEMBER, "2026-11-26 00:00:00.000000003+00:00")])
        before = self.path.read_bytes()
        candidate = primary.native_candidate(self.path, INSTANCE)
        self.assertEqual(candidate["userId"], OWNER)
        self.assertEqual(candidate["createdAt"], "2026-11-26T00:00:00.000000+00:00")
        self.assertEqual(self.path.read_bytes(), before)

    def test_offsets_override_wall_clock_order(self):
        self.database([(OWNER, "2026-11-26 01:15:00.123456789+01:00"),
                       (MEMBER, "2026-11-26 00:30:00.000000001Z")])
        self.assertEqual(primary.native_candidate(self.path, INSTANCE)["userId"], OWNER)

    def test_normalized_utc_tie_refused(self):
        self.database([(OWNER, "2026-11-26 01:00:00.123456789+01:00"),
                       (MEMBER, "2026-11-26 00:00:00.123456789Z")])
        with self.assertRaises(ValueError):
            primary.native_candidate(self.path, INSTANCE)

    def test_bad_or_naive_time_refused(self):
        for value in ("2026-11-26 00:00:00", "not-a-time", "2026-02-31 00:00:00Z",
                      "2026-11-26 00:00:00.1234567890Z"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                primary.timestamp(value)

    def test_missing_database_not_created(self):
        with self.assertRaises(ValueError):
            primary.native_candidate(self.path, INSTANCE)
        self.assertFalse(self.path.exists())

    def test_empty_duplicate_and_malformed_rows_refused(self):
        for rows in ([], [(OWNER, "2026-11-26 00:00:00Z")] * 2,
                     [("not-a-uuid", "2026-11-26 00:00:00Z")]):
            with self.subTest(rows=rows):
                if self.path.exists():
                    self.path.unlink()
                self.database(rows)
                with self.assertRaises(ValueError):
                    primary.native_candidate(self.path, INSTANCE)

    def test_symlink_refused(self):
        self.database([(OWNER, "2026-11-26 00:00:00Z")])
        link = self.path.with_name("link.db")
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            primary.native_candidate(link, INSTANCE)

    def test_owner_hint_contains_only_owner_household_ids_names(self):
        with sqlite3.connect(self.path) as c:
            c.executescript("CREATE TABLE groups(id TEXT,name TEXT);"
                            "CREATE TABLE user_groups(user_id TEXT,group_id TEXT,role TEXT);")
            c.execute("INSERT INTO groups VALUES (?,?)", (GROUP, "Synthetic household"))
            c.executemany("INSERT INTO user_groups VALUES (?,?,?)",
                          [(OWNER, GROUP, "owner"), (MEMBER, GROUP, "user")])
        self.assertEqual(primary.native_owners(self.path), {OWNER: [{"id": GROUP, "name": "Synthetic household"}]})

    def test_full_mount_fields_and_order(self):
        value = {"Id": "a", "Image": "b", "HostConfig": {}, "Config": {},
                 "Mounts": [{"Destination": "/one", "RW": False}, {"Destination": "/two", "RW": True}],
                 "State": {"StartedAt": "c"}, "NetworkSettings": {"Networks": {"n": {"NetworkID": "d"}}}}
        other = copy.deepcopy(value)
        other["Mounts"].reverse()
        self.assertEqual(primary.identity(value), primary.identity(other))
        other["Mounts"][0]["RW"] = False
        self.assertNotEqual(primary.identity(value), primary.identity(other))

    def test_ready_health_and_pending_guards(self):
        binding = {"selected": True}
        services = ("homebox", "assistant-api", "homebox-mcp", "pwa", "proxy", "ollama")
        state = {"phase": "ready", "binding": binding, "resources": {"original": True}}
        current = {name: {"State": {"Running": True, "Health": {"Status": "healthy"}}} for name in services}
        m = SimpleNamespace(journal=lambda: state, installation_owner=lambda: {"established": True, "binding": binding},
                            selected=lambda: binding, SERVICES=services, containers=lambda: current,
                            check_resources=Mock(), validate_config=Mock())
        self.assertEqual(primary.guard(m), current)
        for key, value in (("phase", "cutover"), ("registration_restore", False),
                           ("model_download_pending", True), ("access_change", {"status": "pending"})):
            with patch.dict(state, {key: value}), self.assertRaises(ValueError):
                primary.guard(m)
        current["homebox"]["State"]["Health"]["Status"] = "unhealthy"
        with self.assertRaises(ValueError):
            primary.guard(m)

    def test_no_implicit_yes_attestation(self):
        candidate = {"instanceId": INSTANCE, "userId": OWNER,
                     "createdAt": "2026-11-26T00:00:00.000000+00:00", "nativeProofVersion": 1}
        with patch.dict(sys.modules, {"metadata": SimpleNamespace()}), patch.object(primary, "review", return_value=candidate), \
                patch.object(primary, "save") as save, patch.dict(os.environ, {"YES": "1"}):
            with self.assertRaises(ValueError):
                primary.main(["attest"])
            with self.assertRaises(ValueError):
                primary.main(["attest", MEMBER])
            save.assert_not_called()

    def assert_api_precision_completes(self, native_time):
        self.database([(OWNER, native_time)])
        candidate = primary.native_candidate(self.path, INSTANCE)
        proof = {**candidate, "operatorConfirmedOriginal": True}
        # primary_binding serializes its parsed datetime using isoformat().
        shown = {"bound": True, "instanceId": INSTANCE, "userId": OWNER,
                 "createdAt": dt.datetime.fromisoformat(candidate["createdAt"]).isoformat()}
        self.assertEqual(shown["createdAt"], "2026-11-26T00:00:00+00:00")
        m = SimpleNamespace(WORK=Path(self.temp.name), STATE=Path(self.temp.name), load=lambda _: shown)
        with patch.dict(sys.modules, {"metadata": m}), patch.object(primary, "unchanged"), \
                patch.object(primary, "private_json", return_value=proof), patch.object(primary, "save") as save, \
                contextlib.redirect_stdout(io.StringIO()):
            primary.main(["complete"])
            self.assertEqual(save.call_args.args[2]["status"], "complete")
            self.assertEqual(primary.binding(m, INSTANCE), candidate)

    def test_zero_fraction_API_serialization_completes(self):
        self.assert_api_precision_completes("2026-11-26 00:00:00Z")

    def test_submicrosecond_native_API_serialization_completes(self):
        self.assert_api_precision_completes("2026-11-26 01:00:00.000000999+01:00")


class GuidedRegistration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        (self.state / "work").mkdir(mode=0o700)
        self.write("owner.json", {"username": "owner@example.invalid", "password": "SYNTHETIC_OWNER_PASSWORD"})
        self.write("member.json", {"name": "Synthetic member", "username": "member@example.invalid",
                                   "password": "SYNTHETIC_MEMBER_PASSWORD"})
        self.write("work/household-owners.json", {OWNER: [{"id": GROUP, "name": "Synthetic household"}]})
        self.args = SimpleNamespace(credentials_file="owner.json", new_user_file="member.json", collection=GROUP)
        self.created = False
        self.registrations = self.invitations = 0
        self.allow_owner = True
        self.expired = self.lost_before = self.lost_after = self.missing_membership = False
        self.addCleanup(patch.stopall)
        patch.object(accounts, "STATE", self.state).start()
        patch.object(accounts, "request", side_effect=self.request).start()
        patch.object(sys.stdin, "isatty", return_value=False).start()
        self.output = io.StringIO()
        self.addCleanup(self.assert_redacted)

    def write(self, name, data):
        path = self.state / name
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def assert_redacted(self):
        self.assertNotIn("SYNTHETIC_OWNER_PASSWORD", self.output.getvalue())
        self.assertNotIn("SYNTHETIC_MEMBER_PASSWORD", self.output.getvalue())
        self.assertNotIn("SYNTHETIC_INVITATION_TOKEN", self.output.getvalue())

    def run_create(self):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            guided.create(self.args, accounts)

    def request(self, method, route, payload=None, *, token=None, collection=None):
        if route == "/users/login":
            if payload["username"] == "owner@example.invalid":
                return {"token": "owner-token"}
            if not self.created:
                raise accounts.NativeError(401)
            return {"token": "member-token"}
        if route == "/users/self":
            return {"item": {"id": OWNER if token == "owner-token" else MEMBER,
                             "isSuperuser": False, "groupIds": [] if self.missing_membership else [GROUP]}}
        if route == "/groups/all":
            return [] if self.missing_membership else [{"id": GROUP}]
        if route == "/groups/invitations":
            self.invitations += 1
            if not self.allow_owner:
                raise accounts.NativeError(403)
            self.assertEqual(payload["uses"], 1)
            return {"token": "SYNTHETIC_INVITATION_TOKEN",
                    "expiresAt": (dt.datetime.now(dt.timezone.utc) +
                                  dt.timedelta(minutes=-1 if self.expired else 15)).isoformat()}
        if route == "/users/register":
            self.assertEqual(set(payload), {"name", "email", "password", "token"})
            self.assertEqual(payload["token"], "SYNTHETIC_INVITATION_TOKEN")
            self.registrations += 1
            if self.lost_before:
                raise OSError("SYNTHETIC_INVITATION_TOKEN")
            self.created = True
            if self.lost_after:
                raise OSError("SYNTHETIC_INVITATION_TOKEN")
            return None  # Native POST /users/register has no success body.
        self.fail("Unexpected route")

    def receipt(self):
        paths = list(self.state.glob("new-member-*.json"))
        self.assertEqual(len(paths), 1)
        text = paths[0].read_text()
        for secret in ("SYNTHETIC_MEMBER_PASSWORD", "SYNTHETIC_INVITATION_TOKEN", "member@example.invalid"):
            self.assertNotIn(secret, text)
        self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
        return json.loads(text)

    def test_create_and_repeat_reconcile_only(self):
        self.run_create()
        self.run_create()
        self.assertEqual((self.registrations, self.invitations), (1, 1))
        self.assertEqual(self.receipt()["state"], "complete")

    def test_existing_account_not_joined_or_reset(self):
        self.created = True
        with self.assertRaises(ValueError):
            self.run_create()
        self.assertEqual((self.registrations, self.invitations), (0, 0))

    def test_stale_owner_hint_cannot_bypass_native_acl(self):
        self.allow_owner = False
        with self.assertRaises(accounts.NativeError):
            self.run_create()
        self.assertEqual(self.registrations, 0)
        self.assertEqual(self.receipt()["state"], "invitation-denied")

    def test_expired_invite_does_not_register(self):
        self.expired = True
        with self.assertRaises(ValueError):
            self.run_create()
        self.assertEqual(self.registrations, 0)

    def test_lost_success_reply_reconciles_without_second_signup(self):
        self.lost_after = True
        self.run_create()
        self.run_create()
        self.assertEqual(self.registrations, 1)
        self.assertEqual(self.receipt()["state"], "complete")

    def test_uncertain_failure_never_repeats_signup(self):
        self.lost_before = True
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.run_create()
        self.assertEqual(self.registrations, 1)
        self.assertEqual(self.receipt()["state"], "registration-requested")

    def test_existing_account_missing_membership_not_recreated(self):
        self.missing_membership = True
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.run_create()
        self.assertEqual(self.registrations, 1)
        self.assertEqual(self.receipt()["state"], "account-created-membership-unverified")

    def test_private_file_permissions_and_symlinks(self):
        (self.state / "member.json").chmod(0o644)
        with self.assertRaises(ValueError):
            self.run_create()
        (self.state / "member.json").unlink()
        (self.state / "member.json").symlink_to(self.state / "owner.json")
        with self.assertRaises(ValueError):
            self.run_create()
        self.assertEqual(self.registrations, 0)

    def test_non_tty_requires_explicit_files_and_household(self):
        self.args.collection = None
        with self.assertRaises(ValueError):
            self.run_create()
        self.assertEqual(self.registrations, 0)

    def test_no_echo_fallback(self):
        def fallback(*a, **kw):
            warnings.warn("Fallback would echo", getpass.GetPassWarning)
            self.fail("Fallback must be stopped before reading a password")
        with patch.object(accounts, "credentials", side_effect=fallback), self.assertRaises(ValueError):
            self.run_create()

    def test_password_mismatch_before_invitation_or_signup(self):
        self.args.new_user_file = None
        with patch.object(sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=["Synthetic member", "member@example.invalid"]), \
                patch.object(getpass, "getpass", side_effect=["one", "two"]), self.assertRaises(ValueError):
            self.run_create()
        self.assertEqual((self.registrations, self.invitations), (0, 0))

    def test_no_privilege_fields_accepted(self):
        data = json.loads((self.state / "member.json").read_text())
        self.write("member.json", {**data, "isSuperuser": True})
        with self.assertRaises(ValueError):
            self.run_create()
        self.assertEqual(self.registrations, 0)

    def test_short_password_creates_no_invite_or_receipt_then_corrected_email_succeeds(self):
        data = json.loads((self.state / "member.json").read_text())
        self.write("member.json", {**data, "password": "abc"})
        with self.assertRaises(ValueError):
            self.run_create()
        self.assertEqual((self.registrations, self.invitations), (0, 0))
        self.assertFalse(list(self.state.glob("new-member-*.json")))
        self.write("member.json", data)
        self.run_create()
        self.assertEqual((self.registrations, self.invitations), (1, 1))
        self.assertEqual(self.receipt()["state"], "complete")


if __name__ == "__main__":
    unittest.main()
