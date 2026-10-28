"""Helm render assertions: requires helm and PyYAML (requirements.txt)."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

CHART = Path(__file__).resolve().parents[1]


def render(*args, dev=True):
    command = ["helm", "template", "whatsapp-mcp", str(CHART), "-n", "whatsapp-mcp-dev"]
    # Synthetic historical assignment fixture; no private deployment values.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as fixture:
        if dev:
            yaml.safe_dump({"fullnameOverride": "whatsapp-mcp",
                "app": {"baseUrl": "https://archive.test"},
                "accounts": [{"id": "personal", "label": "Personal", "sessionSecretName": "whatsapp-mcp-dev-personal"},
                             {"id": "profile2", "label": "Profile 2", "profileSlot": True, "sessionSecretName": "whatsapp-mcp-dev-profile2"}],
                "ingress": {"enabled": True, "host": "archive.test"}}, fixture)
            fixture.flush()
            command += ["-f", fixture.name]
        result = subprocess.run([*command, *args], capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


class ChartTests(unittest.TestCase):
    def test_media_recovery_defaults_and_overrides_are_bridge_only(self):
        baseline = render()
        configured = render("--set", "bridge.mediaRecovery.enabled=false",
                            "--set", "bridge.mediaRecovery.maxAttempts=2",
                            "--set", "bridge.mediaRecovery.timeoutSeconds=300",
                            "--set", "bridge.mediaRecovery.minIntervalSeconds=60",
                            "--set", "bridge.mediaDownloadMaxAttempts=12")
        expected = {"MEDIA_RECOVERY_ENABLED": "true", "MEDIA_RECOVERY_MAX_ATTEMPTS": "1",
                    "MEDIA_RECOVERY_TIMEOUT_SECONDS": "120", "MEDIA_RECOVERY_MIN_INTERVAL_SECONDS": "30",
                    "MEDIA_DOWNLOAD_MAX_ATTEMPTS": "8"}
        changed = dict(zip(expected, ("false", "2", "300", "60", "12")))
        bridges = 0
        for before, after in zip(baseline, configured):
            is_bridge = before["kind"] == "Deployment" and before["spec"]["template"]["metadata"]["labels"].get("component") == "bridge"
            if is_bridge:
                bridges += 1
                container = before["spec"]["template"]["spec"]["containers"][0]
                updated = after["spec"]["template"]["spec"]["containers"][0]
                env = {e["name"]: e.get("value") for e in container["env"]}
                new_env = {e["name"]: e.get("value") for e in updated["env"]}
                self.assertEqual({key: env[key] for key in expected}, expected)
                self.assertEqual({key: new_env[key] for key in expected}, changed)
                # After removing only these values the full workload is unchanged.
                container["env"] = [e for e in container["env"] if e["name"] not in expected]
                updated["env"] = [e for e in updated["env"] if e["name"] not in expected]
            self.assertEqual(before, after)
        self.assertEqual(bridges, 2)

    def test_media_recovery_schema_rejects_unsafe_types_and_bounds(self):
        cases = [("enabled", "true"), ("enabled", 1), ("maxAttempts", 0),
                 ("maxAttempts", 4), ("maxAttempts", True), ("maxAttempts", "1"),
                 ("timeoutSeconds", 14), ("timeoutSeconds", 3601), ("timeoutSeconds", 30.5),
                 ("minIntervalSeconds", 4), ("minIntervalSeconds", 3601), ("unexpected", 1)]
        for key, value in cases:
            with self.subTest(key=key, value=value), tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
                yaml.safe_dump({"bridge": {"mediaRecovery": {key: value}}}, config); config.flush()
                with self.assertRaises(subprocess.CalledProcessError):
                    render("-f", config.name)
        for value in (0, 101, True, "8"):
            with self.subTest(download=value), tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
                yaml.safe_dump({"bridge": {"mediaDownloadMaxAttempts": value}}, config); config.flush()
                with self.assertRaises(subprocess.CalledProcessError):
                    render("-f", config.name)

    def test_webhook_relay_is_opt_in_and_disabled_defaults_are_unchanged(self):
        for dev in (True, False):
            baseline = render(dev=dev)
            self.assertEqual(render("--set", "webhookRelay.enabled=false", dev=dev), baseline)
            self.assertEqual(len([d for d in baseline if d["kind"] in ("Deployment", "Job")]), 5)
            self.assertFalse(any(d["metadata"]["name"].endswith("-webhook-relay") for d in baseline))
            for doc in baseline:
                if doc["kind"] in ("Deployment", "Job"):
                    env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
                    self.assertFalse(any(e["name"].startswith("WEBHOOK_RELAY_") for e in env))

    def test_webhook_relay_has_only_dedicated_credentials_and_hardened_runtime(self):
        docs = render("--set", "webhookRelay.enabled=true", "--set", "databaseCA.existingConfigMap=private-db-ca",
                      "--set", "webhookRelay.existingSecret=dedicated-relay", "--set", "webhookRelay.secretKey=relay-token",
                      "--set", "webhookRelay.maxRequestsPerMinute=17", "--set", "imagePullSecrets[0].name=pull-key")
        relay = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-webhook-relay"))
        pod = relay["spec"]["template"]["spec"]
        self.assertEqual(relay["spec"]["replicas"], 1)
        self.assertEqual(relay["spec"]["template"]["metadata"]["labels"], {"app.kubernetes.io/instance": "whatsapp-mcp", "component": "webhook-relay"})
        self.assertEqual(pod["serviceAccountName"], "whatsapp-mcp-webhook-relay")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["imagePullSecrets"], [{"name": "pull-key"}])
        self.assertEqual(pod["securityContext"], {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                                   "fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch", "seccompProfile": {"type": "RuntimeDefault"}})
        container = pod["containers"][0]
        self.assertEqual(container["command"], ["python", "-m", "app.relay"])
        self.assertEqual(container["securityContext"], {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                        "capabilities": {"drop": ["ALL"]}})
        self.assertEqual(container["env"], [
            {"name": "WEBHOOK_RELAY_TOKEN", "valueFrom": {"secretKeyRef": {"name": "dedicated-relay", "key": "relay-token"}}},
            {"name": "WEBHOOK_RELAY_MAX_REQUESTS_PER_MINUTE", "value": "17"},
        ])
        self.assertNotIn("envFrom", container)
        self.assertEqual(container["volumeMounts"], [{"name": "tmp", "mountPath": "/tmp"}])
        self.assertEqual(pod["volumes"], [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}])
        self.assertEqual(container["resources"], {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}})
        for probe in ("readinessProbe", "livenessProbe"):
            self.assertEqual(container[probe]["httpGet"], {"path": "/healthz", "port": "http"})
        sa = next(d for d in docs if d["kind"] == "ServiceAccount" and d["metadata"]["name"] == pod["serviceAccountName"])
        self.assertFalse(sa["automountServiceAccountToken"])
        for binding in (d for d in docs if d["kind"] in ("RoleBinding", "ClusterRoleBinding")):
            self.assertFalse(any(subject["name"] == pod["serviceAccountName"] for subject in binding["subjects"]))

    def test_webhook_relay_clients_are_only_api_and_controller(self):
        docs = render("--set", "webhookRelay.enabled=true", "--set", "webhookRelay.existingSecret=dedicated-relay")
        workloads = [d for d in docs if d["kind"] in ("Deployment", "Job")]
        workloads.append(json.loads(next(d for d in docs if d["kind"] == "ConfigMap")["data"]["job.json"]))
        clients = []
        for workload in workloads:
            container = workload["spec"]["template"]["spec"]["containers"][0]
            env = {item["name"]: item for item in container["env"]}
            if container["name"] in ("app", "controller"):
                self.assertEqual(env["WEBHOOK_RELAY_URL"]["value"], "http://whatsapp-mcp-webhook-relay:8001")
                self.assertEqual(env["WEBHOOK_RELAY_TOKEN"]["valueFrom"]["secretKeyRef"], {"name": "dedicated-relay", "key": "WEBHOOK_RELAY_TOKEN"})
                clients.append(container["name"])
            elif container["name"] != "webhook-relay":
                self.assertFalse(any(key.startswith("WEBHOOK_RELAY_") for key in env))
        self.assertEqual(set(clients), {"app", "controller"})

    def test_webhook_relay_uses_app_image_and_placement_with_own_resources(self):
        placement = {"nodeSelector": {"pool": "general"}, "tolerations": [{"key": "general", "operator": "Exists"}],
                     "affinity": {"nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}}}
        resources = {"requests": {"cpu": "150m", "memory": "192Mi"}, "limits": {"cpu": "600m", "memory": "640Mi"}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
            yaml.safe_dump({"app": placement, "webhookRelay": {"enabled": True, "resources": resources}}, config); config.flush()
            docs = render("-f", str(CHART / "values-doks.example.yaml"), "-f", config.name,
                          "--set", "app.image.digest=sha256:" + "a" * 64, "--set", "imageRegistry=registry.example", dev=False)
        deployments = {d["spec"]["template"]["metadata"]["labels"]["component"]: d for d in docs if d["kind"] == "Deployment"}
        relay = deployments["webhook-relay"]["spec"]["template"]["spec"]
        app = deployments["app"]["spec"]["template"]["spec"]
        self.assertEqual({key: relay[key] for key in placement}, placement)
        self.assertEqual(relay["containers"][0]["resources"], resources)
        self.assertEqual(relay["containers"][0]["image"], app["containers"][0]["image"])
        self.assertIn("@sha256:", relay["containers"][0]["image"])

    def test_webhook_relay_is_private_without_public_routes_or_egress_grants(self):
        for args in ((), ("-f", str(CHART / "values-doks.example.yaml"))):
            docs = render(*args, "--set", "webhookRelay.enabled=true")
            service = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"].endswith("-webhook-relay"))
            self.assertEqual(service["spec"], {"type": "ClusterIP", "selector": {"app.kubernetes.io/instance": "whatsapp-mcp", "component": "webhook-relay"},
                                                "ports": [{"name": "http", "port": 8001, "targetPort": "http"}]})
            for doc in docs:
                if doc["kind"] in ("Ingress", "HTTPRoute"):
                    self.assertNotIn("webhook-relay", json.dumps(doc))
                if doc["kind"] in ("NetworkPolicy", "CiliumNetworkPolicy"):
                    self.assertNotIn("egress", doc["spec"])
                    self.assertNotIn("Egress", doc["spec"].get("policyTypes", []))
        docs = render("--set", "webhookRelay.enabled=true", "--set", "networkPolicy.enabled=false")
        self.assertFalse(any(d["kind"] in ("NetworkPolicy", "CiliumNetworkPolicy") for d in docs))

    def test_webhook_relay_ingress_is_only_same_release_namespace_app_controller(self):
        exclusion = [{"key": "component", "operator": "NotIn", "values": ["webhook-relay"]}]
        callers = [{"key": "component", "operator": "In", "values": ["app", "controller"]}]
        for cilium in (False, True):
            options = ("-f", str(CHART / "values-doks.example.yaml")) if cilium else ()
            docs = render(*options, "--set", "webhookRelay.enabled=true")
            kind = "CiliumNetworkPolicy" if cilium else "NetworkPolicy"
            policies = [d for d in docs if d["kind"] == kind]
            broad = next(d for d in policies if d["metadata"]["name"].endswith("-private-services"))["spec"]
            selector = "endpointSelector" if cilium else "podSelector"
            self.assertEqual(broad[selector]["matchExpressions"], exclusion)
            relay = next(d for d in policies if d["metadata"]["name"].endswith("-webhook-relay"))["spec"]
            self.assertEqual(relay[selector], {"matchLabels": {"app.kubernetes.io/instance": "whatsapp-mcp", "component": "webhook-relay"}})
            if cilium:
                self.assertEqual(broad["ingress"][0]["fromEndpoints"][0]["matchExpressions"], exclusion)
                self.assertEqual(relay["ingress"], [{"fromEndpoints": [{"matchLabels": {
                    "app.kubernetes.io/instance": "whatsapp-mcp", "k8s:io.kubernetes.pod.namespace": "whatsapp-mcp-dev"},
                    "matchExpressions": callers}], "toPorts": [{"ports": [{"port": "8001", "protocol": "TCP"}]}]}])
            else:
                self.assertEqual(broad["ingress"][0]["from"][0]["podSelector"]["matchExpressions"], exclusion)
                self.assertEqual(relay["policyTypes"], ["Ingress"])
                self.assertEqual(relay["ingress"], [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "whatsapp-mcp-dev"}},
                    "podSelector": {"matchLabels": {"app.kubernetes.io/instance": "whatsapp-mcp"}, "matchExpressions": callers}}],
                    "ports": [{"protocol": "TCP", "port": 8001}]}])

    def test_webhook_relay_schema_rejects_invalid_boolean_rate_and_secret_references(self):
        cases = [("enabled", "true"), ("enabled", 1), ("maxRequestsPerMinute", "60"), ("maxRequestsPerMinute", True),
                 ("maxRequestsPerMinute", 0), ("maxRequestsPerMinute", 601), ("maxRequestsPerMinute", 1.5),
                 ("existingSecret", ""), ("existingSecret", "Invalid Secret"), ("existingSecret", "-relay"),
                 ("secretKey", ""), ("secretKey", "bad/key"), ("resources", []), ("unexpected", True)]
        for key, value in cases:
            with self.subTest(key=key, value=value), tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
                yaml.safe_dump({"webhookRelay": {key: value}}, config); config.flush()
                with self.assertRaises(subprocess.CalledProcessError):
                    render("-f", config.name)
        for rate in (1, 600):
            render("--set", "webhookRelay.enabled=true", "--set", "webhookRelay.maxRequestsPerMinute="+str(rate))

    def test_webhook_relay_preserves_existing_workloads_storage_and_worker_checksum(self):
        baseline = render()
        configured = render("--set", "webhookRelay.enabled=true")
        for kind in ("Deployment", "Job", "ConfigMap", "PersistentVolumeClaim", "Role", "RoleBinding", "ServiceAccount", "Service"):
            before = [d for d in baseline if d["kind"] == kind]
            after = [d for d in configured if d["kind"] == kind and not d["metadata"]["name"].endswith("-webhook-relay")]
            if kind == "Deployment":
                for doc in after:
                    env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
                    env[:] = [e for e in env if not e["name"].startswith("WEBHOOK_RELAY_")]
            self.assertEqual(after, before)

    def test_dynamic_registration_switch_reaches_api_controller_and_bootstrap(self):
        for enabled in (True, False):
            docs = render("--set", "app.oauthDynamicRegistration="+str(enabled).lower())
            seen = []
            for doc in docs:
                if doc["kind"] not in ("Deployment", "Job"):
                    continue
                component = doc["spec"]["template"]["metadata"]["labels"]["component"]
                env = {item["name"]: item for item in doc["spec"]["template"]["spec"]["containers"][0]["env"]}
                if component in ("app", "controller", "schema-bootstrap"):
                    self.assertEqual(env["OAUTH_DYNAMIC_REGISTRATION_ENABLED"]["value"], str(enabled).lower())
                    seen.append(component)
                else:
                    self.assertNotIn("OAUTH_DYNAMIC_REGISTRATION_ENABLED", env)
            self.assertEqual(set(seen), {"app", "controller", "schema-bootstrap"})
        result = subprocess.run(["helm", "template", "whatsapp-mcp", str(CHART), "--set-string", "app.oauthDynamicRegistration=false"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_private_ingress_and_service_ports(self):
        docs = render()
        ingress = next(d for d in docs if d["kind"] == "Ingress")
        paths = ingress["spec"]["rules"][0]["http"]["paths"]
        self.assertFalse(any(p["path"].startswith("/internal") for p in paths))
        self.assertTrue({"/login", "/logout", "/accounts", "/profiles", "/connections", "/static", "/oauth"}.issubset({p["path"] for p in paths}))
        self.assertEqual(next(p for p in paths if p["path"] == "/")["pathType"], "Exact")
        self.assertEqual({p["backend"]["service"]["name"] for p in paths}, {"whatsapp-mcp"})
        self.assertFalse(any(p["path"] in ("/qr", "/status") for p in paths))
        for service in (d for d in docs if d["kind"] == "Service"):
            self.assertEqual(service["spec"]["type"], "ClusterIP")
            self.assertIn(service["spec"]["ports"][0]["port"], [8000, 8080])

    def test_worker_template_has_caps_persistence_and_no_extra_secrets(self):
        docs = render()
        configmap = next(d for d in docs if d["kind"] == "ConfigMap")
        job = json.loads(configmap["data"]["job.json"])
        self.assertNotIn("activeDeadlineSeconds", job["spec"])
        pod = job["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(job["spec"]["parallelism"], 1)
        container = pod["containers"][0]
        self.assertEqual(container["resources"]["limits"], {"cpu": "1500m", "memory": "3Gi"})
        self.assertEqual(container["resources"]["requests"], {"cpu": "1000m", "memory": "3Gi"})
        self.assertNotIn("envFrom", container)
        env = {e["name"]: e for e in container["env"]}
        self.assertFalse({"DATABASE_URL", "ADMIN_PASSWORD", "SESSION_DATABASE_URL", "JOB_ID", "JOB_TOKEN"} & env.keys())
        self.assertIn("persistentVolumeClaim", pod["volumes"][0])
        self.assertEqual(container["image"], "whatsapp-mcp-worker:dev")
        self.assertEqual(env["MAX_MEDIA_BYTES"]["value"], "104857600")

    def test_worker_template_change_rolls_controller(self):
        def checksum(docs):
            controller = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-controller"))
            return controller["spec"]["template"]["metadata"]["annotations"]["checksum/worker-template"]
        self.assertNotEqual(checksum(render()), checksum(render("--set", "worker.audioChunkSeconds=20")))

    def test_scheduling_defaults_do_not_add_placement_constraints_or_annotations(self):
        for dev in (True, False):
            docs = render(dev=dev)
            configmap = next(d for d in docs if d["kind"] == "ConfigMap")
            job = json.loads(configmap["data"]["job.json"])
            workloads = [d for d in docs if d["kind"] in ("Deployment", "Job")]
            for workload in [*workloads, job]:
                pod = workload["spec"]["template"]["spec"]
                self.assertFalse({"nodeSelector", "tolerations", "affinity"} & pod.keys())
            self.assertNotIn("annotations", job["metadata"])
            self.assertNotIn("annotations", job["spec"]["template"]["metadata"])

    def test_each_component_placement_preserves_other_rendered_configuration(self):
        placement = {}
        for component in ("app", "controller", "bridge", "worker"):
            placement[component] = {
                "nodeSelector": {"fixture.example/component": component},
                "tolerations": [{"key": "fixture.example/component", "operator": "Equal", "value": component, "effect": "NoSchedule"}],
                "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [{"matchExpressions": [{"key": "fixture.example/component", "operator": "In", "values": [component]}]}]}}},
            }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
            yaml.safe_dump(placement, config); config.flush()
            for dev, args in ((True, ()), (False, ()), (False, ("-f", str(CHART / "values-doks.example.yaml")))):
                with self.subTest(dev=dev, args=args):
                    baseline = render(*args, dev=dev)
                    configured = render(*args, "-f", config.name, dev=dev)
                    bridges = 0
                    for doc in configured:
                        if doc["kind"] not in ("Deployment", "Job"):
                            continue
                        component = doc["spec"]["template"]["metadata"]["labels"]["component"]
                        if component == "bridge":
                            bridges += 1
                        source = "app" if component == "schema-bootstrap" else component
                        pod = doc["spec"]["template"]["spec"]
                        for key, value in placement[source].items():
                            self.assertEqual(pod.pop(key), value)
                    self.assertEqual(bridges, 2)
                    configmap = next(d for d in configured if d["kind"] == "ConfigMap")
                    job = json.loads(configmap["data"]["job.json"])
                    for key, value in placement["worker"].items():
                        self.assertEqual(job["spec"]["template"]["spec"].pop(key), value)
                    configmap["data"]["job.json"] = json.dumps(job, sort_keys=True)
                    for docs in (baseline, configured):
                        cm = next(d for d in docs if d["kind"] == "ConfigMap")
                        cm["data"]["job.json"] = json.dumps(json.loads(cm["data"]["job.json"]), sort_keys=True)
                        controller = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-controller"))
                        controller["spec"]["template"]["metadata"]["annotations"].pop("checksum/worker-template")
                    # Includes image/model pins, resources, security, credentials and PVC names.
                    self.assertEqual(configured, baseline)

    def test_worker_pod_annotations_reach_embedded_pod_only_and_roll_controller(self):
        annotations = {
            "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
            "fixture.example/description": 'quoted: "value"\nsecond line',
        }
        baseline = render()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
            yaml.safe_dump({"worker": {"podAnnotations": annotations}}, config); config.flush()
            docs = render("-f", config.name)
        configmap = next(d for d in docs if d["kind"] == "ConfigMap")
        job = json.loads(configmap["data"]["job.json"])
        self.assertEqual(job["spec"]["template"]["metadata"]["annotations"], annotations)
        self.assertNotIn("annotations", job["metadata"])
        for doc in docs:
            if doc["kind"] in ("Deployment", "Job"):
                self.assertFalse(annotations.keys() & doc["spec"]["template"]["metadata"].get("annotations", {}).keys())
        def checksum(rendered):
            controller = next(d for d in rendered if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-controller"))
            return controller["spec"]["template"]["metadata"]["annotations"]["checksum/worker-template"]
        self.assertNotEqual(checksum(docs), checksum(baseline))

    def test_worker_scheduling_reaches_embedded_job_only_and_rolls_controller(self):
        scheduling = {
            "nodeSelector": {"example.com/workload": "transcription"},
            "tolerations": [{"key": "example.com/workload", "operator": "Equal", "value": "transcription", "effect": "NoSchedule"},
                            {"key": "maintenance", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300}],
            "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64"]}]}]}}},
        }
        def checksum(docs):
            controller = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-controller"))
            return controller["spec"]["template"]["metadata"]["annotations"]["checksum/worker-template"]
        baseline = checksum(render())
        # Each field independently changes the controller rollout checksum.
        for overrides in ({key: value} for key, value in scheduling.items()):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
                yaml.safe_dump({"worker": overrides}, config); config.flush()
                docs = render("-f", config.name)
            configmap = next(d for d in docs if d["kind"] == "ConfigMap")
            pod = json.loads(configmap["data"]["job.json"])["spec"]["template"]["spec"]
            for key, value in overrides.items():
                self.assertEqual(pod[key], value)
            self.assertNotEqual(checksum(docs), baseline)
            for workload in (d for d in docs if d["kind"] in ("Deployment", "Job")):
                self.assertFalse(scheduling.keys() & workload["spec"]["template"]["spec"].keys())
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
            yaml.safe_dump({"worker": scheduling}, config); config.flush()
            docs = render("-f", str(CHART / "values-doks.example.yaml"), "-f", config.name, dev=False)
        pod = json.loads(next(d for d in docs if d["kind"] == "ConfigMap")["data"]["job.json"])["spec"]["template"]["spec"]
        self.assertEqual({key: pod[key] for key in scheduling}, scheduling)

    def test_invalid_scheduling_value_shapes_are_rejected(self):
        cases = (("nodeSelector", []), ("nodeSelector", {"pool": 1}), ("nodeSelector", {"pool": False}),
                 ("tolerations", {}), ("tolerations", ["bad"]), ("affinity", []))
        for component in ("app", "controller", "bridge", "worker"):
            for key, value in cases:
                with self.subTest(component=component, key=key, value=value), tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
                    yaml.safe_dump({component: {key: value}}, config); config.flush()
                    with self.assertRaises(subprocess.CalledProcessError):
                        render("-f", config.name)

    def test_component_settings_must_be_objects(self):
        for component in ("app", "controller", "bridge", "worker"):
            for value in ([], "invalid"):
                with self.subTest(component=component, value=value), tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
                    yaml.safe_dump({component: value}, config); config.flush()
                    with self.assertRaises(subprocess.CalledProcessError):
                        render("-f", config.name)

    def test_worker_pod_annotations_require_string_values(self):
        for value in ([], "invalid", {"fixture.example/value": False}, {"fixture.example/value": 1}):
            with self.subTest(value=value), tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
                yaml.safe_dump({"worker": {"podAnnotations": value}}, config); config.flush()
                with self.assertRaises(subprocess.CalledProcessError):
                    render("-f", config.name)

    def test_separate_account_credentials_and_minimal_role(self):
        docs = render()
        role = next(d for d in docs if d["kind"] == "Role")
        self.assertEqual(role["rules"][0]["resources"], ["jobs"])
        self.assertNotIn("*", role["rules"][0]["verbs"])
        bridge = next(d for d in docs if d["kind"] == "Deployment" and "-bridge-" in d["metadata"]["name"])
        pod = bridge["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        env = {e["name"]: e for e in pod["containers"][0]["env"]}
        self.assertEqual(env["SESSION_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"], "whatsapp-mcp-dev-personal")
        self.assertNotIn("DATABASE_URL", env)
        self.assertEqual(bridge["spec"]["strategy"]["type"], "Recreate")

    def test_multiaccount_bridge_configuration(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
            yaml.safe_dump({"accounts": [{"id": "personal", "label": "Personal", "sessionSecretName": "personal-secret"}, {"id": "work", "label": "Work", "sessionSecretName": "work-secret"}]}, config)
            config.flush()
            docs = render("-f", config.name)
        bridges = [d for d in docs if d["kind"] == "Deployment" and "-bridge-" in d["metadata"]["name"]]
        self.assertEqual(len(bridges), 2)
        for bridge in bridges:
            env = {e["name"]: e for e in bridge["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertEqual({a["id"] for a in json.loads(env["ACCOUNTS_JSON"]["value"])}, {"personal", "work"})
            self.assertEqual(env["SESSION_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"], env["ACCOUNT_ID"]["value"] + "-secret")

    def test_default_and_doks_starters_are_two_unassigned_profile_slots(self):
        for docs in (render(dev=False), render("-f", str(CHART / "values-doks.example.yaml"), dev=False)):
            bridges = [d for d in docs if d["kind"] == "Deployment" and "-bridge-" in d["metadata"]["name"]]
            self.assertEqual(len(bridges), 2)
            app = next(d for d in docs if d["kind"] == "Deployment" and d["spec"]["template"]["spec"]["containers"][0]["name"] == "app")
            env = {e["name"]: e for e in app["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertEqual(json.loads(env["ACCOUNTS_JSON"]["value"]), [
                {"id": "profile1", "label": "Profile 1", "profileSlot": True},
                {"id": "profile2", "label": "Profile 2", "profileSlot": True},
            ])
            self.assertEqual(set(json.loads(env["BRIDGE_URLS_JSON"]["value"])), {"profile1", "profile2"})
            secrets = {next(e for e in d["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "SESSION_DATABASE_URL")["valueFrom"]["secretKeyRef"]["name"] for d in bridges}
            self.assertEqual(secrets, {"whatsapp-mcp-session-profile1", "whatsapp-mcp-session-profile2"})

    def test_dev_preserves_personal_assignment_and_adds_only_one_slot(self):
        docs = render()
        app = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "whatsapp-mcp")
        env = {e["name"]: e for e in app["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(json.loads(env["ACCOUNTS_JSON"]["value"]), [
            {"id": "personal", "label": "Personal", "profileSlot": False},
            {"id": "profile2", "label": "Profile 2", "profileSlot": True},
        ])
        self.assertEqual(json.loads(env["BRIDGE_URLS_JSON"]["value"]), {
            "personal": "http://whatsapp-mcp-bridge-personal:8080",
            "profile2": "http://whatsapp-mcp-bridge-profile2:8080",
        })
        bridge = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "whatsapp-mcp-bridge-profile2")
        env = {e["name"]: e for e in bridge["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["SESSION_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"], "whatsapp-mcp-dev-profile2")

    def test_profile_slot_must_be_a_boolean(self):
        with self.assertRaises(subprocess.CalledProcessError):
            render("--set", "accounts[0].profileSlot=invalid")

    def test_schema_bootstrap_is_explicit_private_hook(self):
        hook = next(d for d in render() if d["kind"] == "Job")
        self.assertEqual(hook["metadata"]["annotations"]["helm.sh/hook"], "pre-install,pre-upgrade")
        pod = hook["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["serviceAccountName"], "default")
        self.assertEqual(pod["containers"][0]["command"], ["python", "-m", "app.init_db"])

    def test_registry_and_pull_secrets_reach_job(self):
        docs = render("--set", "imageRegistry=registry.example", "--set", "imagePullSecrets[0].name=pull-key", "--set", "worker.image.tag=test")
        configmap = next(d for d in docs if d["kind"] == "ConfigMap")
        pod = json.loads(configmap["data"]["job.json"])["spec"]["template"]["spec"]
        self.assertEqual(pod["containers"][0]["image"], "registry.example/whatsapp-mcp-worker:test")
        self.assertEqual(pod["imagePullSecrets"], [{"name": "pull-key"}])

    def test_invalid_worker_bounds_rejected_by_schema(self):
        for value in ("0", "-1", "1.5", "true", "null"):
            with self.subTest(value=value), self.assertRaises(subprocess.CalledProcessError):
                render("--set", "worker.threads=" + value)
        with self.assertRaises(subprocess.CalledProcessError):
            render("--set-string", "worker.threads=4")

    def test_worker_threads_and_resources_remain_infrastructure_values(self):
        schema = json.loads((CHART / "values.schema.json").read_text())
        self.assertNotIn("maximum", schema["properties"]["worker"]["properties"]["threads"])
        before = render()
        old_controller = next(doc for doc in before if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("-controller"))
        for count in (4, 64, 256, 999):
            with self.subTest(threads=count):
                docs = render("--set", f"worker.threads={count}",
                              "--set-string", "worker.resources.requests.cpu=8",
                              "--set-string", "worker.resources.limits.cpu=16",
                              "--set-string", "worker.resources.requests.memory=8Gi",
                              "--set-string", "worker.resources.limits.memory=16Gi")
                cm = next(doc for doc in docs if doc["kind"] == "ConfigMap" and "job.json" in doc.get("data", {}))
                container = json.loads(cm["data"]["job.json"])["spec"]["template"]["spec"]["containers"][0]
                env = {entry["name"]: entry.get("value") for entry in container["env"]}
                self.assertEqual(env["WORKER_THREADS"], str(count))
                self.assertEqual(container["resources"], {"requests": {"cpu": "8", "memory": "8Gi"}, "limits": {"cpu": "16", "memory": "16Gi"}})
                controller = next(doc for doc in docs if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("-controller"))
                self.assertNotEqual(controller["spec"]["template"]["metadata"]["annotations"]["checksum/worker-template"], old_controller["spec"]["template"]["metadata"]["annotations"]["checksum/worker-template"])

    def test_gateway_route_exposes_only_public_paths(self):
        docs = render("-f", str(CHART / "values-doks.example.yaml"))
        self.assertFalse(any(d["kind"] in ("Ingress", "NetworkPolicy") for d in docs))
        route = next(d for d in docs if d["kind"] == "HTTPRoute")
        paths = {m["path"]["value"]: m["path"]["type"] for m in route["spec"]["rules"][0]["matches"]}
        self.assertEqual(paths, {"/": "Exact", "/login": "Exact", "/logout": "Exact", "/mcp": "PathPrefix", "/oauth": "PathPrefix", "/accounts": "PathPrefix", "/profiles": "PathPrefix", "/connections": "PathPrefix", "/static": "PathPrefix", "/.well-known": "PathPrefix"})
        self.assertEqual(route["spec"]["rules"][0]["backendRefs"][0]["port"], 8000)
        self.assertEqual(route["spec"]["rules"][0]["backendRefs"][0]["name"], "whatsapp-mcp")
        self.assertNotIn("filters", route["spec"]["rules"][0])
        policies = [d for d in docs if d["kind"] == "CiliumNetworkPolicy"]
        self.assertEqual(len(policies), 2)
        gateway = next(p for p in policies if p["metadata"]["name"].endswith("-from-gateway"))
        self.assertEqual(gateway["spec"]["endpointSelector"]["matchLabels"]["component"], "app")
        self.assertEqual(gateway["spec"]["ingress"], [{"fromEntities": ["ingress"], "toPorts": [{"ports": [{"port": "8000", "protocol": "TCP"}]}]}])
        private = next(p for p in policies if p["metadata"]["name"].endswith("-private-services"))
        peer = private["spec"]["ingress"][0]["fromEndpoints"][0]["matchLabels"]
        self.assertEqual(peer["k8s:io.kubernetes.pod.namespace"], "whatsapp-mcp-dev")

    def test_conflicting_or_incomplete_route_modes_rejected(self):
        for options in (("httpRoute.enabled=true",), ("ciliumNetworkPolicy.enabled=true",)):
            with self.subTest(options=options), self.assertRaises(subprocess.CalledProcessError):
                render("--set", options[0])
        with self.assertRaises(subprocess.CalledProcessError):
            render("--set", "ingress.enabled=false", "--set", "httpRoute.enabled=true")

    def test_database_ca_reaches_all_database_clients_only(self):
        docs = render("--set", "databaseCA.existingConfigMap=test-db-ca", "--set", "databaseCA.key=custom-ca.crt")
        for doc in (d for d in docs if d["kind"] in ("Deployment", "Job")):
            pod = doc["spec"]["template"]["spec"]
            container = pod["containers"][0]
            env = {e["name"]: e for e in container["env"]}
            self.assertEqual(env["PGSSLROOTCERT"]["value"], "/etc/postgresql/ca.crt")
            mount = next(m for m in container["volumeMounts"] if m["name"] == "database-ca")
            self.assertTrue(mount["readOnly"])
            volume = next(v for v in pod["volumes"] if v["name"] == "database-ca")
            self.assertEqual(volume["configMap"], {"name": "test-db-ca", "items": [{"key": "custom-ca.crt", "path": "ca.crt"}]})
        job = json.loads(next(d for d in docs if d["kind"] == "ConfigMap")["data"]["job.json"])
        self.assertNotIn("PGSSLROOTCERT", {e["name"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]})
        self.assertFalse(any(v["name"] == "database-ca" for v in job["spec"]["template"]["spec"]["volumes"]))

    def test_login_identity_mode_uses_secret_reference_only(self):
        docs = render("--set", "app.loginProxy.mode=signed", "--set", "app.loginProxy.secretName=private-proxy-identity")
        for doc in (d for d in docs if d["kind"] in ("Deployment", "Job") and d["spec"]["template"]["spec"]["containers"][0]["name"] != "bridge"):
            env = {e["name"]: e for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertEqual(env["LOGIN_PROXY_MODE"]["value"], "signed")
            self.assertEqual(env["LOGIN_PROXY_SECRET"], {"name": "LOGIN_PROXY_SECRET", "valueFrom": {"secretKeyRef": {"name": "private-proxy-identity", "key": "LOGIN_PROXY_SECRET"}}})
        docs = render("--set", "app.loginProxy.mode=envoy")
        app = next(d for d in docs if d["kind"] == "Deployment" and d["spec"]["template"]["spec"]["containers"][0]["name"] == "app")
        self.assertNotIn("LOGIN_PROXY_SECRET", {e["name"] for e in app["spec"]["template"]["spec"]["containers"][0]["env"]})

    def test_cloudflare_origin_secret_is_opt_in_and_http_app_only(self):
        for enabled in (False, True):
            docs = render("--set", "cloudflareProxy.enabled=" + str(enabled).lower(),
                          "--set", "cloudflareProxy.originSecretName=private-origin",
                          "--set", "cloudflareProxy.originSecretKey=origin-key")
            workloads = [d for d in docs if d["kind"] in ("Deployment", "Job")]
            workloads.append(json.loads(next(d for d in docs if d["kind"] == "ConfigMap")["data"]["job.json"]))
            for workload in workloads:
                container = workload["spec"]["template"]["spec"]["containers"][0]
                env = {e["name"]: e for e in container["env"]}
                if container["name"] == "app":
                    self.assertEqual(env["CLOUDFLARE_PROXY_ENABLED"]["value"], str(enabled).lower())
                    if enabled:
                        self.assertEqual(env["CLOUDFLARE_ORIGIN_SECRET"]["valueFrom"]["secretKeyRef"], {"name": "private-origin", "key": "origin-key"})
                    else:
                        self.assertNotIn("CLOUDFLARE_ORIGIN_SECRET", env)
                else:
                    self.assertNotIn("CLOUDFLARE_PROXY_ENABLED", env)
                    self.assertNotIn("CLOUDFLARE_ORIGIN_SECRET", env)

    def test_cloudflare_requires_explicit_secret_reference_when_enabled(self):
        for key in ("originSecretName", "originSecretKey"):
            with self.subTest(key=key), self.assertRaises(subprocess.CalledProcessError):
                render("--set", "cloudflareProxy.enabled=true", "--set", "cloudflareProxy." + key + "=")


    def test_storage_backends_share_one_durable_archive_only_in_disk_mode(self):
        for backend in ("s3", "disk"):
            docs = render("--set", "storage.backend=" + backend, "--set", "storage.disk.storageClass=shared-nfs")
            pvcs = [d for d in docs if d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"].endswith("-archive")]
            self.assertEqual(len(pvcs), int(backend == "disk"))
            if pvcs:
                self.assertEqual(pvcs[0]["spec"]["accessModes"], ["ReadWriteMany"])
                self.assertEqual(pvcs[0]["spec"]["storageClassName"], "shared-nfs")
                self.assertEqual(pvcs[0]["spec"]["resources"]["requests"]["storage"], "10Gi")
                self.assertEqual(pvcs[0]["metadata"]["annotations"]["helm.sh/resource-policy"], "keep")
            workloads = [d for d in docs if d["kind"] in ("Deployment", "Job")]
            workloads.append(json.loads(next(d for d in docs if d["kind"] == "ConfigMap")["data"]["job.json"]))
            for workload in workloads:
                pod = workload["spec"]["template"]["spec"]
                container = pod["containers"][0]
                env = {e["name"]: e for e in container["env"]}
                self.assertEqual(env["STORAGE_BACKEND"]["value"], backend)
                self.assertEqual(env["MEDIA_ROOT"]["value"], "/media/archive" if backend == "disk" else "/media")
                self.assertEqual(env["ASR_ENABLED"]["value"], "true")
                self.assertEqual("S3_ACCESS_KEY_ID" in env, backend == "s3")
                self.assertEqual("S3_SECRET_ACCESS_KEY" in env, backend == "s3")
                if backend == "disk":
                    self.assertFalse(any(name.startswith("S3_") for name in env))
                mounts = [m for m in container["volumeMounts"] if m["name"] == "archive"]
                uses_archive = backend == "disk" and container["name"] in ("app", "bridge", "worker")
                self.assertEqual(len(mounts), int(uses_archive))
                volumes = [v for v in pod["volumes"] if v["name"] == "archive"]
                self.assertEqual(len(volumes), int(uses_archive))
                if mounts:
                    self.assertEqual(mounts[0], {"name": "archive", "mountPath": "/media", "readOnly": container["name"] != "bridge"})
                    self.assertEqual(volumes[0]["persistentVolumeClaim"]["claimName"], "whatsapp-mcp-archive")
                    # Bridge owns/chmods a private child, never the root-owned PVC mount.
                    self.assertEqual(Path(env["MEDIA_ROOT"]["value"]).parent.as_posix(), mounts[0]["mountPath"])
                    self.assertEqual(pod["securityContext"]["runAsUser"], 10001)
                    self.assertEqual(pod["securityContext"]["fsGroup"], 10001)
                    self.assertEqual(pod["securityContext"]["fsGroupChangePolicy"], "OnRootMismatch")
                    self.assertTrue(pod["securityContext"]["runAsNonRoot"])
                    self.assertNotIn("initContainers", pod)

    def test_disk_existing_claim_is_shared_without_creating_archive_claim(self):
        docs = render("--set", "storage.backend=disk", "--set", "storage.disk.existingClaim=existing-archive")
        self.assertFalse(any(d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"].endswith("-archive") for d in docs))
        jobs = [d for d in docs if d["kind"] == "Deployment"]
        jobs.append(json.loads(next(d for d in docs if d["kind"] == "ConfigMap")["data"]["job.json"]))
        for job in jobs:
            for volume in job["spec"]["template"]["spec"]["volumes"]:
                if volume["name"] == "archive":
                    self.assertEqual(volume["persistentVolumeClaim"]["claimName"], "existing-archive")

    def test_disabled_asr_omits_template_cache_and_references_but_keeps_controller(self):
        enabled = render()
        disabled = render("--set", "worker.enabled=false", "--set", "storage.backend=disk", "--set", "webhookRelay.enabled=true")
        self.assertFalse(any(d["kind"] == "ConfigMap" and "job.json" in d.get("data", {}) for d in disabled))
        self.assertFalse(any(d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"].endswith("-model-cache") for d in disabled))
        self.assertTrue(any(d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"].endswith("-archive") for d in disabled))
        for doc in disabled:
            if doc["kind"] in ("Deployment", "Job"):
                pod = doc["spec"]["template"]["spec"]
                env = {e["name"]: e for e in pod["containers"][0]["env"]}
                if pod["containers"][0]["name"] == "webhook-relay":
                    continue
                self.assertEqual(env["ASR_ENABLED"]["value"], "false")
                self.assertEqual(env["MEDIA_ROOT"]["value"], "/media/archive")
                if pod["containers"][0]["name"] == "schema-bootstrap":
                    self.assertFalse(any(v["name"] == "archive" for v in pod["volumes"]))
                self.assertFalse(any(v["name"] in ("template", "model-cache") for v in pod["volumes"]))
                self.assertNotIn("WORKER_JOB_TEMPLATE_FILE", env)
        def checksum(docs):
            return next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-controller"))["spec"]["template"]["metadata"]["annotations"]["checksum/worker-template"]
        self.assertNotEqual(checksum(enabled), checksum(disabled))
        self.assertEqual(checksum(disabled), checksum(render("--set", "worker.enabled=false", "--set", "worker.threads=2")))
        cache = next(d for d in enabled if d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"].endswith("-model-cache"))
        self.assertEqual(cache["metadata"]["annotations"]["helm.sh/resource-policy"], "keep")

    def test_storage_and_asr_schema_rejects_invalid_types_and_unshared_access(self):
        cases = [{"worker": {"enabled": value}} for value in ("false", 0, [])]
        cases += [{"storage": value} for value in ([], "disk")]
        cases += [{"storage": {"backend": value}} for value in ("local", True, 1)]
        cases += [{"storage": {"disk": {key: value}}} for key, value in (
            ("existingClaim", True), ("existingClaim", "../secret"), ("storageClass", 1),
            ("size", 10), ("size", "0Gi"), ("accessModes", "ReadWriteMany"),
            ("accessModes", []), ("accessModes", ["ReadWriteOnce"]),
            ("accessModes", ["ReadWriteMany", "ReadWriteMany"]), ("mountPath", "/unsafe"))]
        for values in cases:
            with self.subTest(values=values), tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
                yaml.safe_dump(values, config); config.flush()
                with self.assertRaises(subprocess.CalledProcessError):
                    render("-f", config.name)


    def test_aws_regional_endpoints_may_be_empty_and_public_endpoint_falls_back(self):
        configurations = [
            ({"region": "ap-southeast-1", "addressingStyle": "virtual"}, "", ""),
            ({"endpointUrl": "", "publicEndpointUrl": "", "region": "ap-southeast-1", "addressingStyle": "virtual"}, "", ""),
            # Omitted publicEndpointUrl must work after merging actual chart defaults.
            ({"endpointUrl": "https://s3.internal.example:9000"}, "https://s3.internal.example:9000", "https://s3.internal.example:9000"),
            ({"endpointUrl": "https://s3.internal.example:9000", "publicEndpointUrl": None}, "https://s3.internal.example:9000", "https://s3.internal.example:9000"),
            ({"endpointUrl": "https://s3.internal.example", "publicEndpointUrl": "https://s3.public.example"}, "https://s3.internal.example", "https://s3.public.example"),
        ]
        for configuration, private, public in configurations:
            with self.subTest(configuration=configuration), tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
                yaml.safe_dump({"s3": configuration}, config); config.flush()
                docs = render("-f", config.name)
            workloads = [d for d in docs if d["kind"] in ("Deployment", "Job")]
            workloads.append(json.loads(next(d for d in docs if d["kind"] == "ConfigMap")["data"]["job.json"]))
            for workload in workloads:
                env = {e["name"]: e for e in workload["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertEqual(env["STORAGE_BACKEND"]["value"], "s3")
                self.assertEqual(env["S3_ENDPOINT_URL"]["value"], private)
                self.assertEqual(env["S3_PUBLIC_ENDPOINT_URL"]["value"], public)
                if not private:
                    self.assertEqual(env["S3_REGION"]["value"], "ap-southeast-1")
                    self.assertEqual(env["S3_ADDRESSING_STYLE"]["value"], "virtual")

    def test_s3_endpoint_schema_rejects_malformed_nonempty_urls(self):
        for key in ("endpointUrl", "publicEndpointUrl"):
            for endpoint in ("s3.example", "ftp://s3.example", "https://", "https:///bucket", " https://s3.example", "https://user:secret@s3.example", "https://s3.example/bucket", "https://s3.example?token=x", "https://s3.example#fragment", True, []):
                with self.subTest(key=key, endpoint=endpoint), tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config:
                    yaml.safe_dump({"s3": {key: endpoint}}, config); config.flush()
                    with self.assertRaises(subprocess.CalledProcessError):
                        render("-f", config.name)


if __name__ == "__main__":
    unittest.main()
