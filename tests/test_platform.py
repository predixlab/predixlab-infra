from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "local"))

from plx import cli, jobs, launchd, seed, services  # noqa: E402
from plx.build import ignored  # noqa: E402
from plx.config import LocalConfig, SecretStore  # noqa: E402
from plx.context import ContextError  # noqa: E402
from plx.paths import Home  # noqa: E402
from plx.platform import Platform, container_command, cpu_count, entries_for, memory_arg, parameter_env_name  # noqa: E402
from plx.resolve import Resolver  # noqa: E402
from plx.runtime import ContainerSpec  # noqa: E402


class PlatformFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform = Platform.load()

    def test_platform_is_consistent(self) -> None:
        self.assertEqual(self.platform.validate(), [])

    def test_production_schedules_are_pinned(self) -> None:
        schedules = {job["azureName"]: job["schedule"]["cronExpression"] for job in self.platform.jobs}
        self.assertEqual(schedules, {
            "predixlab-signal": "30 2 * * *",
            "predixlab-options": "30 11 * * *",
        })

    def test_signal_job_runs_collector_first(self) -> None:
        job = self.platform.job("signal")
        self.assertEqual([c["name"] for c in job["initContainers"]], ["predixlab-collector"])
        self.assertEqual(job["initContainers"][0]["args"], ["-m", "predixlab_collector", "update_daily_data"])
        self.assertEqual(job["replicaTimeout"], 1800)
        self.assertEqual(job["replicaRetryLimit"], 0)

    def test_signals_env_only_uses_known_pivot_variables(self) -> None:
        # predixlab-signals rejects unknown PIVOT_* / ALPHARANK_* variables at startup.
        known = {
            "PIVOT_PICKS_COLLECTION", "PIVOT_MARKET", "PIVOT_TAG", "PIVOT_HORIZON", "PIVOT_TRAIN_START",
            "PIVOT_LOOKBACK", "PIVOT_MAX_PER_SIDE", "PIVOT_REQUIRE_TRADING_DAY", "PIVOT_DATA_MAX_BAD_RATIO",
            "PIVOT_ARTIFACTS", "ALPHARANK_TAG", "ALPHARANK_COLLECTION", "ALPHARANK_GENERATIONS_COLLECTION",
            "ALPHARANK_TOP_N",
        }
        container = self.platform.job("signal")["containers"][0]
        for environment in ("prod", "local"):
            names = {entry.name for entry in entries_for(container["env"], environment)}
            self.assertEqual({n for n in names if n.startswith(("PIVOT_", "ALPHARANK_"))} - known, set())

    def test_local_differences_are_explicit(self) -> None:
        api = self.platform.container_apps[0]["containers"][0]
        prod = {entry.name: entry for entry in entries_for(api["env"], "prod")}
        local = {entry.name: entry for entry in entries_for(api["env"], "local")}
        self.assertNotIn("EMAIL_BACKEND", prod)
        self.assertEqual(local["EMAIL_BACKEND"].value, "console")
        self.assertIn("https://dashboard.predixlab.com", prod["CORS_ORIGINS"].value)
        self.assertNotIn("predixlab.com", local["CORS_ORIGINS"].value)
        campaign = {entry.name: entry for entry in entries_for(self.platform.function_apps[0]["settings"], "local")}
        self.assertEqual(campaign["CAMPAIGN_DELIVERY_BACKEND"].value, "console")

    def test_api_public_origin_matches_each_environment(self) -> None:
        api = self.platform.container_apps[0]["containers"][0]
        for environment in ("prod", "local"):
            env = {entry.name: entry for entry in entries_for(api["env"], environment)}
            self.assertEqual(env["PUBLIC_API_URL"].value, self.platform.endpoints(environment)["api"])

    def test_api_session_secret_is_independent_and_key_vault_backed(self) -> None:
        catalog = self.platform.secrets["session-secret-key"]
        self.assertEqual(catalog["keyVaultSecret"], "session-secret-key")
        self.assertEqual(catalog["local"], "generated")
        api = self.platform.container_apps[0]["containers"][0]
        for environment in ("prod", "local"):
            env = {entry.name: entry for entry in entries_for(api["env"], environment)}
            self.assertEqual(env["SESSION_SECRET_KEY"].secret, "session-secret-key")
            self.assertNotEqual(env["SESSION_SECRET_KEY"].secret, env["SECRET_KEY"].secret)

    def test_api_and_campaign_share_independent_unsubscribe_secret(self) -> None:
        catalog = self.platform.secrets["unsubscribe-secret"]
        self.assertEqual(catalog["keyVaultSecret"], "campaign-unsubscribe-signing-key")
        self.assertEqual(catalog["local"], "generated")
        api = self.platform.container_apps[0]
        self.assertEqual(api["identity"], "api")
        settings = self.platform.function_apps[0]["settings"]
        self.assertEqual(next(entry for entry in settings if entry["name"] == "UNSUBSCRIBE_SECRET"), {
            "name": "UNSUBSCRIBE_SECRET", "keyVaultReference": "unsubscribe-secret",
        })
        for environment in ("prod", "local"):
            env = {entry.name: entry for entry in entries_for(api["containers"][0]["env"], environment)}
            campaign = {entry.name: entry for entry in entries_for(settings, environment)}
            self.assertEqual(env["UNSUBSCRIBE_SECRET"].secret, "unsubscribe-secret")
            self.assertEqual(campaign["UNSUBSCRIBE_SECRET"].secret, env["UNSUBSCRIBE_SECRET"].secret)
            self.assertNotEqual(env["UNSUBSCRIBE_SECRET"].secret, env["SECRET_KEY"].secret)
            self.assertNotEqual(env["UNSUBSCRIBE_SECRET"].secret, env["SESSION_SECRET_KEY"].secret)

    def test_existing_campaign_secret_settings_remain_literal_secrets(self) -> None:
        settings = {entry["name"]: entry for entry in self.platform.function_apps[0]["settings"]}
        for name, secret in {
            "AzureWebJobsStorage": "storage-account-connection-string",
            "MONGODB_URL": "mongodb-connection-string",
            "ACS_CONNECTION_STRING": "acs-connection-string",
            "SECRET_KEY": "jwt-secret-key",
        }.items():
            self.assertEqual(settings[name], {"name": name, "secret": secret})
        self.assertEqual(
            [entry["name"] for entry in settings.values() if "keyVaultReference" in entry],
            ["UNSUBSCRIBE_SECRET"],
        )

    def test_key_vault_reference_validation(self) -> None:
        for reference, extra, expected in (
            ("not-declared", {}, "references unknown secret 'not-declared'"),
            ("unsubscribe-secret", {"secret": "jwt-secret-key"}, "needs exactly one of"),
        ):
            with self.subTest(reference=reference, extra=extra):
                data = json.loads(json.dumps(self.platform.data))
                data["functionApps"][0]["settings"].append({
                    "name": "INVALID", "keyVaultReference": reference, **extra,
                })
                self.assertTrue(any(expected in problem for problem in Platform(data).validate()))
        data = json.loads(json.dumps(self.platform.data))
        data["containerApps"][0]["containers"][0]["env"].append({
            "name": "INVALID", "keyVaultReference": "unsubscribe-secret",
        })
        self.assertTrue(any("uses keyVaultReference outside a Function App" in problem for problem in Platform(data).validate()))

    def test_key_vault_reference_schema_is_function_app_only(self) -> None:
        schema = json.loads((ROOT / "config" / "platform.schema.json").read_text(encoding="utf-8"))
        self.assertNotIn("keyVaultReference", schema["$defs"]["envEntry"]["properties"])
        kinds = schema["$defs"]["functionSetting"]["oneOf"]
        self.assertEqual(kinds[0], {"$ref": "#/$defs/envEntry"})
        self.assertEqual(kinds[1]["required"], ["name", "keyVaultReference"])
        self.assertFalse(kinds[1]["additionalProperties"])
        self.assertEqual(schema["properties"]["functionApps"]["items"]["properties"]["settings"]["items"],
                         {"$ref": "#/$defs/functionSetting"})

    def test_static_site_rewrites_reference_local_endpoints(self) -> None:
        endpoints = self.platform.endpoints("local")
        for site in self.platform.static_sites:
            for rule in site["local"]["originRewrites"]:
                self.assertIn(rule["to"], endpoints)
                self.assertTrue(rule["from"].startswith("https://"))

    def test_person_specific_values_use_deployment_parameters(self) -> None:
        for job in self.platform.jobs:
            for container in job.get("initContainers", []) + job["containers"]:
                recipient = next(entry for entry in container["env"] if entry["name"] == "ACS_TO_EMAILS")
                self.assertEqual(recipient, {"name": "ACS_TO_EMAILS", "parameter": "alert-recipients"})

    def test_unknown_deployment_parameter_is_rejected(self) -> None:
        data = json.loads(json.dumps(self.platform.data))
        entry = next(item for item in data["jobs"][0]["containers"][0]["env"] if item["name"] == "ACS_TO_EMAILS")
        entry["parameter"] = "not-declared"
        self.assertTrue(any("references unknown parameter 'not-declared'" in problem for problem in Platform(data).validate()))


class LocalRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ctx = Mock()
        self.ctx.home = Home(root / "state", data_root=root / "raw-data")
        self.ctx.platform = Platform.load()
        self.ctx.network = "predixlab-staging"
        self.ctx.gateway.return_value = "192.168.207.1"
        self.ctx.mongo_host.return_value = "192.168.207.2"
        self.ctx.local_image.side_effect = lambda key: f"predixlab-local/{key}:latest"
        self.ctx.image_arch.return_value = None
        self.ctx.resolver.env.return_value = {}

    def test_default_data_directory_is_independent_of_staging_state(self) -> None:
        with patch.dict(os.environ, {"PLX_HOME": str(Path(self.tmp.name) / "state")}, clear=False):
            with patch.dict(os.environ, {"PLX_DATA_DIR": str(Path(self.tmp.name) / "original")}):
                home = Home()
        self.assertEqual(home.data, Path(self.tmp.name) / "original")
        self.assertEqual(home.root, Path(self.tmp.name) / "state")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Home(self.ctx.home.root).data, Path("~/.predixlab").expanduser())

    def test_mongo_uses_one_localhost_publication(self) -> None:
        spec = services.SERVICES["mongo"].spec(self.ctx)
        self.assertEqual(spec.publish, ["127.0.0.1:27017:27017"])
        with patch.object(services, "tcp_open", return_value=True) as tcp_open:
            self.assertTrue(services.SERVICES["mongo"].healthy(self.ctx))
        tcp_open.assert_called_once_with("127.0.0.1", 27017)

    def test_launchd_agent_uses_same_raw_data_directory(self) -> None:
        config = launchd.render(self.ctx)
        self.assertEqual(config["EnvironmentVariables"]["PLX_DATA_DIR"], str(self.ctx.home.data))

    def test_api_collector_and_signals_mount_original_raw_data(self) -> None:
        api = services.SERVICES["api"].spec(self.ctx)
        job = self.ctx.platform.job("signal")
        collector = jobs.container_spec(self.ctx, job, job["initContainers"][0], "collector-test", {})
        signals = jobs.container_spec(self.ctx, job, job["containers"][0], "signals-test", {})
        for spec in (api, collector, signals):
            self.assertEqual(spec.binds, [(str(self.ctx.home.data), "/data", False)])
        self.assertEqual(api.labels["plx.mongo-address"], "192.168.207.2")
        self.assertEqual(services.SERVICES["campaign"].spec(self.ctx).labels["plx.mongo-address"], "192.168.207.2")
        self.assertEqual(collector.image, "predixlab-local/collector:latest")
        self.assertEqual(signals.image, "predixlab-local/signals:latest")
        self.assertEqual(collector.command + collector.args, ["python", "-m", "predixlab_collector", "update_daily_data"])

    def test_seeding_from_mounted_directory_is_rejected(self) -> None:
        self.ctx.home.data.mkdir(parents=True)
        with self.assertRaisesRegex(seed.SeedError, "already the mounted data directory"):
            seed.seed_from_directory(self.ctx, self.ctx.home.data, options_days=0)

    def test_purge_refuses_to_delete_nested_raw_data(self) -> None:
        root = Path(self.tmp.name) / "state"
        self.ctx.home = Home(root, data_root=root / "raw-data")
        with patch.object(launchd, "uninstall") as uninstall:
            with self.assertRaisesRegex(ContextError, "inside PLX_HOME"):
                cli.cmd_destroy(self.ctx, SimpleNamespace(yes=True, purge_data=True))
        uninstall.assert_not_called()


class ConversionTests(unittest.TestCase):
    def test_cpu_and_memory(self) -> None:
        self.assertEqual(cpu_count("0.5"), 1)
        self.assertEqual(cpu_count("2"), 2)
        self.assertEqual(memory_arg("1Gi"), "1024M")
        self.assertEqual(memory_arg("1536Mi"), "1536M")
        self.assertEqual(memory_arg("4Gi"), "4096M")

    def test_parameter_environment_name(self) -> None:
        self.assertEqual(parameter_env_name("alert-recipients"), "PLX_PARAM_ALERT_RECIPIENTS")

    def test_key_vault_reference_selects_a_local_secret(self) -> None:
        for environment in ("prod", "local"):
            with self.subTest(environment=environment):
                entries = entries_for([{"name": "UNSUBSCRIBE_SECRET", "keyVaultReference": "unsubscribe-secret"}], environment)
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0].secret, "unsubscribe-secret")
                self.assertIsNone(entries[0].value)

    def test_local_command_override(self) -> None:
        self.assertEqual(container_command({"command": ["python"], "args": ["-m", "x"]}), (["python"], ["-m", "x"]))
        self.assertEqual(container_command({"command": ["uv"], "local": {"command": ["/app/.venv/bin/uvicorn", "a"]}}), (["/app/.venv/bin/uvicorn", "a"], []))

    def test_container_spec_maps_azure_command_semantics(self) -> None:
        spec = ContainerSpec(name="n", image="img", command=["python", "-u"], args=["-m", "pkg"], network="net", cpus=2, memory="4096M")
        args = spec.cli_args(None)
        self.assertEqual(args[args.index("--entrypoint") + 1], "python")
        self.assertEqual(args[args.index("img"):], ["img", "-u", "-m", "pkg"])

    def test_funcignore_matching(self) -> None:
        patterns = [".git*", ".github/", "__pycache__/", "tests/", "local.settings.json", "uv.lock"]
        self.assertTrue(ignored(".gitignore", patterns))
        self.assertTrue(ignored(".github/workflows/ci.yml", patterns))
        self.assertTrue(ignored("predixlab_campaign/__pycache__/x.pyc", patterns))
        self.assertTrue(ignored("tests/test_x.py", patterns))
        self.assertTrue(ignored("uv.lock", patterns))
        self.assertFalse(ignored("function_app.py", patterns))
        self.assertFalse(ignored("predixlab_campaign/template_assets/email/en/daily.html", patterns))


class ResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(Path(self.tmp.name))
        self.platform = Platform.load()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _resolver(self, overrides: dict | None = None) -> Resolver:
        if overrides:
            self.home.config_file.write_text(json.dumps(overrides), encoding="utf-8")
        secrets = SecretStore(self.home.secrets_file)
        secrets.initialize(self.platform)
        secrets = SecretStore(self.home.secrets_file)
        return Resolver(self.platform, LocalConfig(self.platform, self.home), secrets, "192.168.207.1", lambda: "192.168.207.2")

    def test_secrets_resolve_to_emulators_and_generated_values(self) -> None:
        resolver = self._resolver()
        job = self.platform.job("signal")
        env = resolver.env(job["containers"][0]["env"], "signal", "predixlab-signals")
        self.assertEqual(env["MONGODB_URL"], "mongodb://192.168.207.2:27017")
        self.assertIn("QueueEndpoint=http://192.168.207.1:10001/devstoreaccount1", env["CAMPAIGN_STORAGE_CONNECTION_STRING"])
        self.assertEqual(env["ACS_CONNECTION_STRING"], "")
        self.assertEqual(env["ACS_TO_EMAILS"], "")
        api_env = resolver.env(self.platform.container_apps[0]["containers"][0]["env"], "api", "predixlab-api")
        campaign_env = resolver.env(self.platform.function_apps[0]["settings"], "campaign")
        self.assertEqual(len(api_env["SECRET_KEY"]), 64)
        self.assertEqual(len(api_env["SESSION_SECRET_KEY"]), 64)
        self.assertNotEqual(api_env["SESSION_SECRET_KEY"], api_env["SECRET_KEY"])
        self.assertEqual(api_env["SECRET_KEY"], campaign_env["SECRET_KEY"])
        self.assertEqual(len(api_env["UNSUBSCRIBE_SECRET"]), 64)
        self.assertEqual(api_env["UNSUBSCRIBE_SECRET"], campaign_env["UNSUBSCRIBE_SECRET"])
        self.assertEqual(len({api_env[name] for name in ("SECRET_KEY", "SESSION_SECRET_KEY", "UNSUBSCRIBE_SECRET")}), 3)
        self.assertNotIn("SESSION_SECRET_KEY", campaign_env)

    def test_generated_secrets_are_stable_and_private(self) -> None:
        first = self._resolver()
        second = self._resolver()
        for key in ("jwt-secret-key", "session-secret-key", "unsubscribe-secret"):
            self.assertEqual(first.secret(key), second.secret(key))
        self.assertEqual(self.home.secrets_file.stat().st_mode & 0o777, 0o600)

    def test_initialization_adds_session_secret_without_replacing_jwt_key(self) -> None:
        existing_key = "existing-local-jwt-key-" + "x" * 42
        self.home.secrets_file.write_text(f"jwt-secret-key={existing_key}\n")

        resolver = self._resolver()

        self.assertEqual(resolver.secret("jwt-secret-key"), existing_key)
        self.assertEqual(len(resolver.secret("session-secret-key")), 64)
        self.assertNotEqual(resolver.secret("session-secret-key"), existing_key)

    def test_initialization_adds_unsubscribe_secret_without_replacing_existing_keys(self) -> None:
        existing = {
            "jwt-secret-key": "existing-local-jwt-key",
            "session-secret-key": "existing-local-session-key",
            "stripe-secret-key": "existing-local-test-stripe-key",
        }
        self.home.secrets_file.write_text("".join(f"{key}={value}\n" for key, value in existing.items()))
        resolver = self._resolver()
        for key, value in existing.items():
            self.assertEqual(resolver.secret(key), value)
        self.assertEqual(len(resolver.secret("unsubscribe-secret")), 64)
        self.assertNotIn(resolver.secret("unsubscribe-secret"), existing.values())

    def test_key_vault_reference_uses_explicit_local_value(self) -> None:
        self.home.secrets_file.write_text("unsubscribe-secret=existing-local-unsubscribe-key\n")
        resolver = self._resolver()
        for workload, entries in (
            ("api", self.platform.container_apps[0]["containers"][0]["env"]),
            ("campaign", self.platform.function_apps[0]["settings"]),
        ):
            self.assertEqual(resolver.env(entries, workload)["UNSUBSCRIBE_SECRET"], "existing-local-unsubscribe-key")

    def test_key_vault_reference_requires_local_initialization(self) -> None:
        resolver = Resolver(self.platform, LocalConfig(self.platform, self.home),
                            SecretStore(self.home.secrets_file), "192.168.207.1", lambda: "192.168.207.2")
        with self.assertRaisesRegex(KeyError, "unsubscribe-secret.*run `plx init`"):
            resolver.env([{"name": "UNSUBSCRIBE_SECRET", "keyVaultReference": "unsubscribe-secret"}], "campaign")

    def test_plx_env_masks_unsubscribe_secret_for_both_workloads(self) -> None:
        resolver = self._resolver()
        ctx = SimpleNamespace(platform=self.platform, resolver=resolver)
        for workload in ("api", "campaign"):
            for show_secrets in (False, True):
                with self.subTest(workload=workload, show_secrets=show_secrets):
                    output = io.StringIO()
                    args = SimpleNamespace(workload=workload, container=None, show_secrets=show_secrets)
                    with redirect_stdout(output):
                        self.assertEqual(cli.cmd_env(ctx, args), 0)
                    expected = resolver.secret("unsubscribe-secret") if show_secrets else "<secret>"
                    self.assertIn(f"UNSUBSCRIBE_SECRET={expected}\n", output.getvalue())
                    if not show_secrets:
                        self.assertNotIn(resolver.secret("unsubscribe-secret"), output.getvalue())

    def test_mongodb_address_is_resolved_for_each_new_container(self) -> None:
        address = ["192.168.207.2"]
        resolver = self._resolver()
        resolver.mongo_host = lambda: address[0]
        self.assertEqual(resolver.mongodb_url(), "mongodb://192.168.207.2:27017")
        address[0] = "192.168.207.8"
        self.assertEqual(resolver.mongodb_url(), "mongodb://192.168.207.8:27017")

    def test_config_overrides_set_and_unset(self) -> None:
        resolver = self._resolver({"env": {"api": {"DEBUG": "True", "EMAIL_BACKEND": None}}})
        env = resolver.env(self.platform.container_apps[0]["containers"][0]["env"], "api", "predixlab-api")
        self.assertEqual(env["DEBUG"], "True")
        self.assertNotIn("EMAIL_BACKEND", env)


class BicepParityTests(unittest.TestCase):
    """The Bicep helpers must select the same env entries as the local resolver."""

    def test_bicep_functions_follow_the_same_entry_rules(self) -> None:
        source = (ROOT / "azure" / "modules" / "platform-functions.bicep").read_text(encoding="utf-8")
        self.assertIn(
            "contains(entry, 'secret') || contains(entry, 'keyVaultReference') || contains(entry, 'value') || contains(entry, 'parameter') "
            "|| contains(entry.?values ?? {}, environmentName)",
            source,
        )
        self.assertIn("contains(entry, 'parameter') ? parameterValues[entry.parameter]", source)
        self.assertIn("loadJsonContent('../config/platform.json')", (ROOT / "azure" / "main.bicep").read_text(encoding="utf-8"))

    def test_function_app_renders_versionless_key_vault_reference_and_preserves_literal_settings(self) -> None:
        source = (ROOT / "azure" / "modules" / "function-app.bicep").read_text(encoding="utf-8")
        self.assertIn("filter(functionApp.settings, entry => appliesTo(entry, environmentName))", source)
        self.assertIn(
            "value: contains(entry, 'keyVaultReference')\n"
            "      ? '@Microsoft.KeyVault(SecretUri=${keyVaultUri}secrets/${secretCatalog[entry.keyVaultReference].keyVaultSecret}/)'\n"
            "      : (contains(entry, 'secret') ? resolvedSecrets[entry.secret] : envValue(entry, environmentName, parameterValues))",
            source,
        )
        self.assertIn("'storage-account-connection-string': storageConnectionString", source)
        self.assertIn("identity: {\n    type: 'SystemAssigned'\n  }", source)
        self.assertIn("keyVaultReferenceIdentity: 'SystemAssigned'", source)
        self.assertIn("output principalId string = site.identity.principalId", source)
        main = (ROOT / "azure" / "main.bicep").read_text(encoding="utf-8")
        function_module = main.split("module functionApps ", 1)[1].split("module staticSites ", 1)[0]
        self.assertIn("keyVaultUri: keyVaultUri", function_module)
        self.assertIn("secretCatalog: platform.secrets", function_module)

    def test_function_app_vault_role_is_gated_and_depends_on_its_identity(self) -> None:
        main = (ROOT / "azure" / "main.bicep").read_text(encoding="utf-8")
        self.assertIn("param deployRoleAssignments bool = false", main)
        self.assertIn("module roleAssignments 'modules/role-assignments.bicep' = if (deployRoleAssignments)", main)
        self.assertIn("functionAppName: functionApps[0].outputs.name", main)
        self.assertIn("functionAppPrincipalId: functionApps[0].outputs.principalId", main)
        source = (ROOT / "azure" / "modules" / "role-assignments.bicep").read_text(encoding="utf-8")
        assignment = source.split("resource functionSecretsUser ", 1)[1].split("resource certificateUser ", 1)[0]
        self.assertIn("name: guid(vault.id, functionAppPrincipalId, roles.keyVaultSecretsUser)", assignment)
        self.assertIn("scope: vault", assignment)
        self.assertIn("principalId: functionAppPrincipalId", assignment)
        self.assertIn("principalType: 'ServicePrincipal'", assignment)
        self.assertIn("roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)", assignment)
        prod = (ROOT / "azure" / "parameters" / "prod.bicepparam").read_text(encoding="utf-8")
        self.assertIn("param deployRoleAssignments = false", prod)


if __name__ == "__main__":
    unittest.main()
