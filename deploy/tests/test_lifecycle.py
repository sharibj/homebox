"""Stdlib-only lifecycle checks. Execute in Docker with check-lifecycle.sh."""

import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch


def module(name):
    spec = importlib.util.spec_from_file_location(name, "/source/deploy/lifecycle/" + name + ".py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


metadata = module("metadata")
accounts = module("accounts")


class PrivateFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ("source", "deployment", "state", "state/work"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.addCleanup(patch.stopall)
        for key, value in {
            "SOURCE": self.root / "source", "DEPLOY": self.root / "deployment",
            "STATE": self.root / "state", "WORK": self.root / "state/work",
        }.items():
            patch.object(metadata, key, value).start()
        patch.object(accounts, "STATE", self.root / "state").start()
        patch.dict(os.environ, {"LC_ROOT": "/fixture", "LC_STATE": "/private-state",
                               "LC_PROJECT": "homebox-lifecycle-test-unit",
                               "LC_DAEMON": "synthetic-daemon", "LC_COMMAND": "fresh",
                               "LC_RESUME": "0", "LC_TEST_OVERRIDE": ""}).start()
        metadata.save(metadata.WORK / "inspect.json", [])
        (metadata.WORK / "volumes.txt").write_text("")
        (metadata.WORK / "external").write_text("0\n")
        (metadata.WORK / "lan").write_text("0\n")

    def test_environment_preserves_unrelated_bytes(self):
        body = "# keep spacing\r\nSTACK_VERSION=old\r\nAI_CHAT_MODEL='same'\r\n"
        changed = metadata.replace_setting(body, "STACK_VERSION", "new")
        self.assertEqual(changed.replace("new", "old"), body)

    def test_duplicate_setting_rejected(self):
        with self.assertRaises(ValueError):
            metadata.environment("HOMEBOX_HOST=a\nHOMEBOX_HOST=b\n")

    def test_shell_code_is_never_sourced(self):
        self.assertEqual(metadata.environment("AI_MODEL=$(touch /do-not-create)\n")["AI_MODEL"],
                         "$(touch /do-not-create)")
        self.assertFalse(Path("/do-not-create").exists())

    def test_interpolation_names_include_optional_nested_and_unbraced_but_not_escaped(self):
        self.assertEqual(metadata.interpolation_keys(
            "${OUTER:-${INNER:-default}} $BARE $$ESCAPED $${ALSO_ESCAPED} $$$ODD"),
            {"OUTER", "INNER", "BARE", "ODD"})

    def test_environment_authority_includes_omitted_compose_defaults(self):
        (metadata.DEPLOY / ".env").write_text("EXPLICIT=kept\nREFERENCE=${DOTENV_OPTIONAL:-x}\n")
        (metadata.DEPLOY / "docker-compose.yml").write_text(
            "${HBOX_ALLOW_REGISTRATION:-false} ${ASSISTANT_INSTANCE_LABEL:-} $AI_PROVIDER")
        self.assertTrue({"EXPLICIT", "DOTENV_OPTIONAL", "HBOX_ALLOW_REGISTRATION",
                         "ASSISTANT_INSTANCE_LABEL", "AI_PROVIDER"} <= set(metadata.environment_keys()))
        (metadata.DEPLOY / ".env").unlink()
        self.assertIn("HBOX_ALLOW_REGISTRATION", metadata.environment_keys())

    def test_shell_sanitizer_clears_omitted_config_and_all_compose_controls_not_docker_context(self):
        source = Path("/source/deploy/homebox").read_text()
        function = source.split("sanitize_config_environment() {\n", 1)[1].split("\n}\n", 1)[0]
        controls = source.split("clear_compose_controls() {\n", 1)[1].split("\n}\n", 1)[0]
        script = ("meta() { printf '%s\\n' HBOX_ALLOW_REGISTRATION ASSISTANT_INSTANCE_LABEL; }\n" +
                  "clear_compose_controls() {\n" + controls + "\n}\n" +
                  function + "\nprintf '%s\\n' \"${HBOX_ALLOW_REGISTRATION-unset}\" "
                  "\"${ASSISTANT_INSTANCE_LABEL-unset}\" \"${COMPOSE_ENV_FILES-unset}\" "
                  "\"${COMPOSE_FUTURE_CONTROL-unset}\" \"$DOCKER_CONTEXT\" \"$DOCKER_HOST\"\n")
        result = subprocess.run(["sh", "-eu", "-c", script], capture_output=True, text=True, check=True,
            env={"PATH": os.environ["PATH"], "STATE": str(metadata.STATE),
                 "HBOX_ALLOW_REGISTRATION": "true", "ASSISTANT_INSTANCE_LABEL": "ambient",
                 "COMPOSE_ENV_FILES": "/foreign", "COMPOSE_FUTURE_CONTROL": "ambient",
                 "DOCKER_CONTEXT": "selected-context", "DOCKER_HOST": "unix:///selected.sock"})
        self.assertEqual(result.stdout.splitlines(),
                         ["unset"] * 4 + ["selected-context", "unix:///selected.sock"])

    def test_deployment_file_cannot_change_docker_client_selection(self):
        with self.assertRaises(ValueError):
            metadata.environment("DOCKER_CONTEXT=unexpected\n")

    def test_environment_authority_includes_every_selected_overlay(self):
        for name, text in (("docker-compose.yml", "${BASE_OPTIONAL:-}"),
                           ("docker-compose.external-ai.yml", "${EXTERNAL_OPTIONAL:-}"),
                           ("docker-compose.lan.yml", "${LAN_OPTIONAL:-}"),
                           ("test.yml", "${TEST_OPTIONAL:-}")):
            (metadata.DEPLOY / name).write_text(text)
        for flag in ("external", "lan"):
            (metadata.WORK / flag).write_text("1\n")
        with patch.dict(os.environ, {"LC_TEST_OVERRIDE": "selected"}), \
                patch.object(metadata, "TEST_COMPOSE_OVERRIDE", metadata.DEPLOY / "test.yml"):
            self.assertTrue({"BASE_OPTIONAL", "EXTERNAL_OPTIONAL", "LAN_OPTIONAL", "TEST_OPTIONAL"}
                            <= set(metadata.environment_keys()))

    def test_reserved_shell_names_rejected(self):
        with self.assertRaises(ValueError):
            metadata.environment("PATH=/hostile\n")

    def test_env_atomic_update_does_not_follow_symlink(self):
        (metadata.DEPLOY / ".env").symlink_to(self.root / "elsewhere")
        with self.assertRaises(ValueError):
            metadata.write_environment("STACK_VERSION=new\n")

    def test_state_binding_rejects_another_daemon(self):
        metadata.update(binding={"root": "/fixture", "project": "homebox-lifecycle-test-unit", "daemon": "other"})
        with self.assertRaises(ValueError):
            metadata.select()

    def test_fresh_rejects_data_volume_without_receipt(self):
        (metadata.WORK / "volumes.txt").write_text("homebox-lifecycle-test-unit_homebox_data\n")
        with self.assertRaises(ValueError):
            metadata.select()

    def test_fresh_rejects_complete_installation(self):
        metadata.update(binding=metadata.selected(), phase="ready")
        os.environ["LC_RESUME"] = "1"
        with self.assertRaises(ValueError):
            metadata.select()

    def test_existing_config_requires_explicit_resume(self):
        (metadata.DEPLOY / ".env").write_text("HOMEBOX_INSTANCE_ID=existing\n")
        with self.assertRaises(ValueError):
            metadata.select()

    def test_missing_encryption_key_with_data_fails_closed(self):
        (metadata.WORK / "volumes.txt").write_text("existing-data\n")
        with self.assertRaises(ValueError):
            metadata.init_stage()

    def test_partial_initialization_keeps_existing_secret_and_mode(self):
        bootstrap = metadata.STATE / "bootstrap"
        (bootstrap / "secrets").mkdir(parents=True)
        (metadata.DEPLOY / "secrets").mkdir()
        body = "HOMEBOX_INSTANCE_ID=11111111-1111-4111-8111-111111111111\n"
        (bootstrap / ".env").write_text(body)
        (metadata.DEPLOY / ".env").write_text(body)
        preserved = metadata.DEPLOY / "secrets" / metadata.SECRET_NAMES[0]
        preserved.write_text("existing-synthetic-only-key-32-characters")
        os.chmod(preserved, 0o640)
        for name in metadata.SECRET_NAMES:
            (bootstrap / "secrets" / name).write_text("new-synthetic-only-key-32-characters")
        metadata.init_install()
        self.assertEqual(preserved.read_text(), "existing-synthetic-only-key-32-characters")
        self.assertEqual(stat.S_IMODE(preserved.stat().st_mode), 0o640)

    def test_empty_existing_secret_rejected(self):
        (metadata.STATE / "bootstrap/secrets").mkdir(parents=True)
        (metadata.STATE / "bootstrap/.env").write_text(
            "HOMEBOX_INSTANCE_ID=11111111-1111-4111-8111-111111111111\n")
        (metadata.DEPLOY / "secrets").mkdir()
        (metadata.DEPLOY / "secrets/homebox_api_pepper").touch()
        with self.assertRaises(ValueError):
            metadata.init_install()

    def test_source_snapshot_omits_private_generated_state(self):
        directory = metadata.SOURCE / "apps/pwa"
        (directory / "node_modules/private").mkdir(parents=True)
        (directory / "src").mkdir()
        (directory / "node_modules/private/secret.txt").write_text("synthetic")
        (directory / ".env.private").write_text("synthetic")
        (directory / "src/public.ts").write_text("export const value = 1")
        self.assertEqual(set(metadata.source_files("pwa")), {"apps/pwa/src/public.ts"})

    def test_source_symlink_refused(self):
        directory = metadata.SOURCE / "apps/pwa"
        directory.mkdir(parents=True)
        (directory / "escape").symlink_to(self.root / "state")
        with self.assertRaises(ValueError):
            metadata.source_files("pwa")

    def test_invitation_output_cannot_escape_state(self):
        for value in ("../escape.json", "/tmp/escape.json", "subdirectory/secret.json"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                accounts.state_file(value, existing=False)

    def test_credentials_require_private_permissions(self):
        path = accounts.STATE / "credentials.json"
        path.write_text('{"username":"synthetic@example.invalid","password":"synthetic-only"}')
        os.chmod(path, 0o644)
        with self.assertRaises(ValueError):
            accounts.credentials(path.name)
        os.chmod(path, 0o600)
        self.assertEqual(accounts.credentials(path.name)["username"], "synthetic@example.invalid")

    def test_noninteractive_credentials_do_not_fall_back_to_echo(self):
        with patch("sys.stdin.isatty", return_value=False), self.assertRaises(ValueError):
            accounts.credentials(None)

    def test_revoke_requires_target_collection_and_acknowledgment(self):
        for argv in (["remove-member"], ["remove-member", "--collection", "11111111-1111-4111-8111-111111111111"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                accounts.main(argv)

    def test_no_global_account_or_collection_deletion_route(self):
        source = Path("/source/deploy/lifecycle/accounts.py").read_text()
        self.assertNotIn('"/users/self"', source)
        self.assertNotIn('request("DELETE", "/groups",', source)

    def test_unsupported_existing_user_join_cannot_emit_request(self):
        with patch.object(accounts, "request") as request, self.assertRaises(SystemExit):
            accounts.main(["join-invite"])
        request.assert_not_called()

    def registration_fixture(self):
        body = "HBOX_ALLOW_REGISTRATION=false\n"
        (metadata.DEPLOY / ".env").write_text(body)
        metadata.update(binding=metadata.selected(), registration_restore=body,
                        registration_nonce="current")
        metadata.save(metadata.WORK / "registration-container.json", [{
            "Id": "synthetic-native", "Config": {"Labels": {
                "com.docker.compose.project": "homebox-lifecycle-test-unit"},
                "Env": ["HBOX_OPTIONS_ALLOW_REGISTRATION=false"]},
            "State": {"Running": True, "Health": {"Status": "healthy"}}}])
        return body

    def test_recovery_marker_cannot_clear_without_native_verification(self):
        self.registration_fixture()
        with self.assertRaises(ValueError):
            metadata.finish_registration_restore()
        self.assertIsNotNone(metadata.journal()["registration_restore"])

    def test_recovery_permits_only_native_registration_environment_drift(self):
        with patch.dict(os.environ, {"LC_COMMAND": "users", "LC_REGISTRATION_RECOVERY": "1"}):
            metadata.validate_environment_match("homebox", {"HBOX_OPTIONS_ALLOW_REGISTRATION": "false"},
                                                {"HBOX_OPTIONS_ALLOW_REGISTRATION": "true"})
            with self.assertRaises(ValueError):
                metadata.validate_environment_match("homebox", {"HBOX_OPTIONS_HOSTNAME": "protected"},
                                                    {"HBOX_OPTIONS_HOSTNAME": "changed"})
        with patch.dict(os.environ, {"LC_REGISTRATION_RECOVERY": "0"}):
            with self.assertRaises(ValueError):
                metadata.validate_environment_match("homebox", {"HBOX_OPTIONS_ALLOW_REGISTRATION": "false"},
                                                    {"HBOX_OPTIONS_ALLOW_REGISTRATION": "true"})

    def test_recovery_refuses_different_writer_image_but_accepts_same_image_alias(self):
        with patch.object(metadata, "containers", return_value={"homebox": {"Image": "original"}}):
            metadata.save(metadata.WORK / "registration-image.json", [{"Id": "different"}])
            with self.assertRaises(ValueError):
                metadata.verify_recovery_image()
            metadata.save(metadata.WORK / "registration-image.json", [{"Id": "original"}])
            metadata.verify_recovery_image()

    def test_open_native_public_status_cannot_clear_recovery(self):
        self.registration_fixture()
        with patch.object(metadata.urllib.request.OpenerDirector, "open",
                          return_value=io.BytesIO(b'{"allowRegistration":true}')):
            with self.assertRaises(ValueError):
                metadata.verify_registration()
        self.assertIsNotNone(metadata.journal()["registration_restore"])

    def test_verified_native_registration_can_clear_current_recovery(self):
        self.registration_fixture()
        with patch.object(metadata.urllib.request.OpenerDirector, "open",
                          return_value=io.BytesIO(b'{"allowRegistration":false}')):
            metadata.verify_registration()
        metadata.finish_registration_restore()
        self.assertIsNone(metadata.journal()["registration_restore"])

    def test_changed_environment_invalidates_registration_verification(self):
        self.registration_fixture()
        with patch.object(metadata.urllib.request.OpenerDirector, "open",
                          return_value=io.BytesIO(b'{"allowRegistration":false}')):
            metadata.verify_registration()
        (metadata.DEPLOY / ".env").write_text("HBOX_ALLOW_REGISTRATION=false\n# later edit\n")
        with self.assertRaises(ValueError):
            metadata.finish_registration_restore()
        self.assertIsNotNone(metadata.journal()["registration_restore"])

    def test_registration_restore_keeps_unrelated_edits_and_restores_missing_flag(self):
        prior = "HBOX_ALLOW_REGISTRATION=false\nOTHER_SETTING=unchanged\n"
        current = "OTHER_SETTING=unchanged\n# operator note\n"
        metadata.update(registration_restore=prior)
        (metadata.DEPLOY / ".env").write_text(current)
        metadata.restore_registration()
        self.assertEqual(metadata.read_env(), current + "HBOX_ALLOW_REGISTRATION=false\n")
        self.assertEqual(metadata.journal()["registration_restore"], prior)
        nonce = metadata.journal()["registration_nonce"]
        metadata.restore_registration()
        self.assertNotEqual(metadata.journal()["registration_nonce"], nonce)

    def resource_fixture(self):
        (metadata.DEPLOY / ".env").write_text("HOMEBOX_INSTANCE_ID=11111111-1111-4111-8111-111111111111\n")
        project = metadata.selected()["project"]
        volumes, definitions = [], {}
        current = {name: {"Mounts": []} for name in metadata.SERVICES}
        services = {name: {"volumes": []} for name in metadata.SERVICES}
        for service, targets in metadata.DATA_MOUNTS.items():
            for target, logical in targets.items():
                name = project + "_" + logical
                definitions[logical] = {"name": name}
                volumes.append({"Name": name, "Driver": "local", "Scope": "local", "CreatedAt": "original",
                                "Mountpoint": "/volumes/" + name, "Options": None, "Labels": {
                                    "com.docker.compose.project": project, "com.docker.compose.volume": logical}})
                current[service]["Mounts"].append({"Type": "volume", "Name": name,
                    "Source": "/volumes/" + name, "Destination": target, "RW": True})
                services[service]["volumes"].append({"type": "volume", "source": logical, "target": target})
        metadata.save(metadata.WORK / "volume-inspect.json", volumes)
        metadata.save(metadata.WORK / "config.json", {"volumes": definitions, "services": services, "secrets": {}})
        patch.object(metadata, "containers", return_value=current).start()
        metadata.update(resources=metadata.check_mount_bindings())
        return current, volumes

    def test_missing_real_volume_refused_before_writer(self):
        _, volumes = self.resource_fixture()
        metadata.save(metadata.WORK / "volume-inspect.json", volumes[:-1])
        with self.assertRaises(ValueError):
            metadata.check_resources()

    def test_desktop_alias_is_exact_and_never_applied_to_other_engines(self):
        with patch.dict(os.environ, {"LC_ENGINE_OS": "Docker Desktop"}):
            self.assertTrue(metadata.bind_source_matches("/host_mnt/Users/fixture/key", "/Users/fixture/key"))
            self.assertFalse(metadata.bind_source_matches("/host_mnt/Users/other/key", "/Users/fixture/key"))
        with patch.dict(os.environ, {"LC_ENGINE_OS": "Linux"}):
            self.assertFalse(metadata.bind_source_matches("/host_mnt/Users/fixture/key", "/Users/fixture/key"))

    def test_recreated_same_name_volume_refused_before_writer(self):
        _, volumes = self.resource_fixture()
        volumes[0]["CreatedAt"] = "substituted"
        metadata.save(metadata.WORK / "volume-inspect.json", volumes)
        with self.assertRaises(ValueError):
            metadata.check_resources()

    def test_runtime_mount_substitution_refused_before_writer(self):
        current, _ = self.resource_fixture()
        current["homebox"]["Mounts"][0]["Name"] = "different-database"
        with self.assertRaises(ValueError):
            metadata.check_resources()
        with self.assertRaises(ValueError):
            metadata.check_mount_bindings()

    def test_missing_all_original_containers_refused_before_writer(self):
        self.resource_fixture()
        with patch.object(metadata, "containers", return_value={}):
            with self.assertRaises(ValueError):
                metadata.check_resources()

    def test_canonical_owner_refuses_alternate_state_directory(self):
        binding = {**metadata.selected(), "state": "/some-other-private-state"}
        metadata.save(metadata.DEPLOY / ".env.lifecycle-owner", {"binding": binding, "established": True})
        with self.assertRaises(ValueError):
            metadata.select()
        self.assertFalse((metadata.DEPLOY / ".env").exists())

    def test_local_model_plan_does_not_pull_external_role_model(self):
        api = {"AI_PROVIDER": "ollama", "OLLAMA_MODEL": "local-fixture:1",
               "AI_CHAT_PROVIDER": "anthropic-messages", "AI_CHAT_MODEL": "external-fixture"}
        self.assertEqual(metadata.local_models(api), ["local-fixture:1"])

    def test_remote_ollama_is_not_downloaded_locally(self):
        self.assertEqual(metadata.local_models({
            "AI_PROVIDER": "ollama", "AI_BASE_URL": "http://remote.example.invalid:11434",
            "AI_MODEL": "remote-fixture"}), [])

    def test_distinct_local_models_are_deduplicated(self):
        self.assertEqual(metadata.local_models({"AI_CHAT_MODEL": "same:1", "AI_VISION_MODEL": "same:1"}), ["same:1"])

    def test_model_name_cannot_be_shell_option_or_command(self):
        for value in ("--delete", "$(command)", "model;command", "model\ncommand"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                metadata.local_models({"OLLAMA_MODEL": value})

    def test_interrupted_registration_blocks_other_operations(self):
        metadata.update(binding=metadata.selected(), registration_restore="HBOX_ALLOW_REGISTRATION=false\n")
        with self.assertRaises(ValueError):
            metadata.select()

    def test_interrupted_download_requires_explicit_network_recovery(self):
        metadata.update(binding=metadata.selected(), model_download_pending=True)
        with self.assertRaises(ValueError):
            metadata.select()

    def test_mount_order_does_not_change_preservation_result(self):
        (metadata.DEPLOY / ".env").write_text("STACK_VERSION=candidate\n")
        (metadata.DEPLOY / "secrets").mkdir()
        metadata.save(metadata.WORK / "preservation-guards.json", {
            "environment": metadata.digest(b"STACK_VERSION=VERSION\n"), "secrets": {}})
        mount_a = {"Destination": "/data", "Type": "volume", "Name": "synthetic", "RW": True}
        mount_b = {"Destination": "/secret", "Type": "bind", "Source": "/synthetic", "RW": False}
        before = {name: {"Id": name, "Image": name, "State": {
            "Running": True, "Health": {"Status": "healthy"}}, "HostConfig": {"ReadonlyRootfs": True},
            "Mounts": [mount_a, mount_b], "NetworkSettings": {"Networks": {"test": {"NetworkID": "same"}}}}
            for name in metadata.SERVICES}
        metadata.save(metadata.WORK / "before.json", before)
        metadata.update(candidate="candidate", candidate_images={name: name for name in metadata.APPS},
                        changed_services=[])
        current = json.loads(json.dumps(before))
        for item in current.values():
            item["Mounts"].reverse()
        with patch.object(metadata, "containers", return_value=current):
            metadata.verify()
        current["homebox"]["Mounts"].append(dict(mount_b))
        with patch.object(metadata, "containers", return_value=current), self.assertRaises(ValueError):
            metadata.verify()


if __name__ == "__main__":
    unittest.main()
