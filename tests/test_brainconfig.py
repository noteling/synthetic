"""The generic entry point validates connections without fetching credentials."""

from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from synthetics.brainconfig import BrainConfigError, MAX_CONFIG_BYTES, load


class BrainConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.path = self.folder / "brain.json"

    def config(self, adapter="codex-cli", protocol="chatgpt-login", **connection):
        return dict(version=1, executor=dict(adapter=adapter, executable=sys.executable),
                    connection=dict(protocol=protocol, **connection))

    def gateway(self, **connection):
        value = dict(protocol="openai-responses", model="engineering-default",
                     base_url="https://models.company.example/v1",
                     auth=dict(type="bearer-env", variable="COMPANY_MODEL_TOKEN"))
        value.update(connection)
        return self.config(**value)

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")
        return load(self.path)

    def test_login_defaults_are_runtime_specific(self):
        codex = self.write(self.config())
        self.assertEqual((codex.adapter, codex.protocol, codex.model),
                         ("codex-cli", "chatgpt-login", ""))
        claude = self.write(self.config("claude-cli", "claude-login"))
        self.assertEqual(claude.model, "claude-opus-5-5")
        self.assertEqual(codex.executable, str(Path(sys.executable).resolve()))
        self.assertEqual(codex.path, self.path.resolve())

    def test_gateway_is_normalized_without_resolving_secret_environment(self):
        config = self.gateway(base_url="https://Models.Company.Example/v1/",
                              headers_from_env={"X-Employee-Account": "COMPANY_ACCOUNT"})
        with patch.dict(os.environ, {"COMPANY_MODEL_TOKEN": "secret-one",
                                     "COMPANY_ACCOUNT": "private-account"}):
            first = self.write(config)
        with patch.dict(os.environ, {"COMPANY_MODEL_TOKEN": "secret-two",
                                     "COMPANY_ACCOUNT": "another-account"}):
            second = load(self.path)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.connection["base_url"], "https://models.company.example/v1")
        self.assertEqual(first.connection["headers_from_env"],
                         {"x-employee-account": "COMPANY_ACCOUNT"})
        self.assertNotIn("secret-one", repr(first))
        self.assertNotIn("secret-two", json.dumps(first.to_dict()))
        self.assertEqual(set(first.metadata()), {"adapter", "protocol", "model", "config_sha256"})
        exported = json.dumps(first.metadata())
        for private in ("models.company", "COMPANY_MODEL_TOKEN", "x-employee", "COMPANY_ACCOUNT"):
            self.assertNotIn(private, exported)

    def test_gateway_does_not_require_credential_to_be_available_at_load(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.write(self.gateway()).protocol, "openai-responses")

    def test_fingerprint_changes_with_connection_and_not_json_formatting(self):
        first = self.write(self.gateway())
        self.path.write_text(json.dumps(self.gateway(), sort_keys=True, indent=4))
        self.assertEqual(first.fingerprint, load(self.path).fingerprint)
        second = self.write(self.gateway(model="different-alias"))
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        third = self.write(self.gateway(auth=dict(type="bearer-env", variable="OTHER_TOKEN")))
        self.assertNotEqual(first.fingerprint, third.fingerprint)

    def test_snapshot_and_nested_mappings_are_immutable(self):
        config = self.write(self.gateway(headers_from_env={"X-Team": "TEAM"}))
        with self.assertRaises(FrozenInstanceError):
            config.fingerprint = "changed"
        with self.assertRaises(TypeError):
            config.connection["model"] = "changed"
        with self.assertRaises(TypeError):
            config.connection["auth"]["variable"] = "changed"
        with self.assertRaises(TypeError):
            config.connection["headers_from_env"]["x-team"] = "changed"
        detached = config.to_dict()
        detached["connection"]["auth"]["variable"] = "OTHER"
        detached["executor"]["executable"] = "/other"
        self.write(self.config())
        self.assertEqual(config.protocol, "openai-responses")
        self.assertEqual(config.connection["auth"]["variable"], "COMPANY_MODEL_TOKEN")

    def test_relative_executable_resolves_from_config_not_cwd(self):
        binary = self.folder / "bin" / "runtime"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        config = self.config()
        config["executor"]["executable"] = "bin/runtime"
        self.assertEqual(self.write(config).executable, str(binary.resolve()))

    def test_bare_executable_uses_path_lookup(self):
        binary = self.folder / "runtime"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        config = self.config()
        config["executor"]["executable"] = "runtime"
        with patch.dict(os.environ, {"PATH": str(self.folder)}):
            self.assertEqual(self.write(config).executable, str(binary.resolve()))

    def test_missing_or_nonexecutable_runtime_is_rejected_without_echoing_path(self):
        binary = self.folder / "private-runtime"
        binary.write_text("not executable")
        for executable in (str(binary), str(self.folder / "missing"), str(self.folder)):
            with self.subTest(executable=executable):
                config = self.config()
                config["executor"]["executable"] = executable
                with self.assertRaises(BrainConfigError) as caught:
                    self.write(config)
                self.assertNotIn(executable, str(caught.exception))

    def test_unresolvable_user_home_is_a_redacted_config_error(self):
        config = self.config()
        config["executor"]["executable"] = "~synthetics-no-such-user-9f0d/bin/runtime"
        with self.assertRaises(BrainConfigError) as caught:
            self.write(config)
        self.assertNotIn("synthetics-no-such-user", str(caught.exception))

    def test_gateway_protocol_matches_runtime_explicitly(self):
        pairs = [("claude-cli", "chatgpt-login"), ("codex-cli", "claude-login"),
                 ("claude-cli", "openai-responses"), ("codex-cli", "openai-chat"),
                 ("unknown-cli", "chatgpt-login")]
        for adapter, protocol in pairs:
            with self.subTest(adapter=adapter, protocol=protocol), self.assertRaises(BrainConfigError):
                self.write(self.config(adapter, protocol))

    def test_unknown_fields_and_raw_credentials_are_rejected_at_every_layer(self):
        for section in (None, "executor", "connection", "auth"):
            for field in ("token", "api_key", "system_prompt", "headers", "unexpected"):
                with self.subTest(section=section, field=field):
                    config = self.gateway()
                    target = (config if section is None else config["connection"]["auth"]
                              if section == "auth" else config[section])
                    target[field] = "private-value-do-not-echo"
                    with self.assertRaises(BrainConfigError) as caught:
                        self.write(config)
                    self.assertNotIn("private-value-do-not-echo", str(caught.exception))

    def test_gateway_requires_model_url_and_auth(self):
        for field in ("model", "base_url", "auth"):
            with self.subTest(field=field), self.assertRaises(BrainConfigError):
                config = self.gateway()
                del config["connection"][field]
                self.write(config)

    def test_only_loopback_may_use_http(self):
        for url in ("http://localhost:8765/v1", "http://127.0.0.1/v1", "http://127.5.6.7:1234", "http://[::1]:8765/v1"):
            with self.subTest(url=url):
                self.assertEqual(self.write(self.gateway(base_url=url)).connection["base_url"], url)
        for url in ("http://company.example/v1", "http://localhost.evil/v1", "http://192.168.1.2/v1", "http://[::2]/v1"):
            with self.subTest(url=url), self.assertRaises(BrainConfigError):
                self.write(self.gateway(base_url=url))

    def test_urls_reject_credentials_query_fragment_ambiguity_and_invalid_ports(self):
        urls = ["https://user:secret@company.example/v1", "https://user@company.example/v1",
                "https://company.example/v1?key=secret", "https://company.example/v1?",
                "https://company.example/v1#secret", "https://company.example/v1#",
                "ftp://company.example/v1", "https:///v1", "https://company.example:0/v1",
                "https://company.example:99999/v1", "https://company.example:port/v1",
                "https://company.example\\@evil/v1", "https://company.example\n/v1"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(BrainConfigError) as caught:
                self.write(self.gateway(base_url=url))
            self.assertNotIn("secret", str(caught.exception))

    def test_environment_references_are_names_not_values_or_employee_actor_fields(self):
        for reference in ("", "token-value-with-dashes", "1TOKEN", "TOKEN=value", "TOKEN\n", 123,
                          "SYNTHETICS_HOME", "SYNTHETICS_SESSION_RUN", "synthetics_home"):
            with self.subTest(reference=reference):
                for use in ("auth", "header"):
                    config = self.gateway()
                    if use == "auth":
                        config["connection"]["auth"]["variable"] = reference
                    else:
                        config["connection"]["headers_from_env"] = {"X-Team": reference}
                    with self.assertRaises(BrainConfigError):
                        self.write(config)

    def test_extra_headers_reject_auth_cookie_host_bad_names_and_case_duplicates(self):
        for headers in ({"Authorization": "TOKEN"}, {"cOoKiE": "TOKEN"}, {"HOST": "HOSTNAME"},
                        {"Bad:Header": "TOKEN"}, {"Bad\nHeader": "TOKEN"},
                        {"X-Team": "TOKEN", "x-team": "OTHER"}):
            with self.subTest(headers=headers), self.assertRaises(BrainConfigError):
                self.write(self.gateway(headers_from_env=headers))

    def test_login_cannot_carry_stale_gateway_settings(self):
        for field, value in (("base_url", "https://company.example"),
                             ("auth", {"type": "bearer-env", "variable": "TOKEN"}),
                             ("headers_from_env", {})):
            with self.subTest(field=field), self.assertRaises(BrainConfigError):
                self.write(self.config(**{field: value}))

    def test_strict_json_schema_types(self):
        for version in (True, "1", 1.0, None, 2):
            config = self.config()
            config["version"] = version
            with self.subTest(version=version), self.assertRaises(BrainConfigError):
                self.write(config)
        for field in ("executor", "connection"):
            for value in (None, [], "string", True):
                config = self.config()
                config[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(BrainConfigError):
                    self.write(config)
        for value in (True, None, 123, [], "", "model\ud800"):
            with self.subTest(model=value), self.assertRaises(BrainConfigError):
                self.write(self.gateway(model=value))

    def test_duplicate_fields_nonfinite_and_invalid_json_fail_without_echoing_input(self):
        for raw in ('{"version":1,"version":1}', '{"secret":NaN}', '{"secret":Infinity}',
                    '{"private-value-do-not-echo":', '[]', 'null'):
            self.path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(BrainConfigError) as caught:
                load(self.path)
            self.assertNotIn("private-value-do-not-echo", str(caught.exception))

    def test_read_is_bounded_and_utf8_strict(self):
        valid = json.dumps(self.config()).encode("utf-8")
        self.path.write_bytes(valid + b" " * (MAX_CONFIG_BYTES - len(valid)))
        self.assertEqual(load(self.path).protocol, "chatgpt-login")
        self.path.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
        with self.assertRaisesRegex(BrainConfigError, "64 KiB"):
            load(self.path)
        self.path.write_bytes(b"\xff")
        with self.assertRaisesRegex(BrainConfigError, "UTF-8"):
            load(self.path)
        with self.assertRaises(BrainConfigError):
            load(self.folder / "missing.json")


if __name__ == "__main__":
    unittest.main()
