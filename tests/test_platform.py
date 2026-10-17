from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
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

    def test_api_session_secret_is_independent_and_key_vault_backed(self) -> None:
        catalog = self.platform.secrets["session-secret-key"]
        self.assertEqual(catalog["keyVaultSecret"], "session-secret-key")
        self.assertEqual(catalog["local"], "generated")
        api = self.platform.container_apps[0]["containers"][0]
        for environment in ("prod", "local"):
            env = {entry.name: entry for entry in entries_for(api["env"], environment)}
            self.assertEqual(env["SESSION_SECRET_KEY"].secret, "session-secret-key")
            self.assertNotEqual(env["SESSION_SECRET_KEY"].secret, env["SECRET_KEY"].secret)

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
        self.assertNotIn("SESSION_SECRET_KEY", campaign_env)

    def test_generated_secrets_are_stable_and_private(self) -> None:
        first = self._resolver()
        second = self._resolver()
        for key in ("jwt-secret-key", "session-secret-key"):
            self.assertEqual(first.secret(key), second.secret(key))
        self.assertEqual(self.home.secrets_file.stat().st_mode & 0o777, 0o600)

    def test_initialization_adds_session_secret_without_replacing_jwt_key(self) -> None:
        existing_key = "existing-local-jwt-key-" + "x" * 42
        self.home.secrets_file.write_text(f"jwt-secret-key={existing_key}\n")

        resolver = self._resolver()

        self.assertEqual(resolver.secret("jwt-secret-key"), existing_key)
        self.assertEqual(len(resolver.secret("session-secret-key")), 64)
        self.assertNotEqual(resolver.secret("session-secret-key"), existing_key)

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
            "contains(entry, 'secret') || contains(entry, 'value') || contains(entry, 'parameter') "
            "|| contains(entry.?values ?? {}, environmentName)",
            source,
        )
        self.assertIn("contains(entry, 'parameter') ? parameterValues[entry.parameter]", source)
        self.assertIn("loadJsonContent('../config/platform.json')", (ROOT / "azure" / "main.bicep").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
