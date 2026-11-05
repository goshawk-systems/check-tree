"""The capped runner fleet module (infra/runner-fleet): static checks on its text, and, when `terraform` is installed,
fmt / validate / `terraform test` in a scratch copy. No AWS: the module is never planned or applied against an account, and the
Terraform runs below have every AWS credential source cut off and use a mocked provider."""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import socket
import subprocess

import pytest

PKG = pathlib.Path(__file__).resolve().parent.parent
FLEET = PKG / "infra" / "runner-fleet"
README = PKG / "infra" / "README.md"

# What the default classes were priced at when written: 4 x m6in.large + 2 x m6in.xlarge, 730 h.
DEFAULT_WORST_CASE = 651.2184


def _tf(name: str) -> str:
    return (FLEET / name).read_text()


def _strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.splitlines())


def _all_tf() -> str:
    return "\n".join(_strip_comments(p.read_text()) for p in sorted(FLEET.glob("*.tf")))


def _block(text: str, open_at: int) -> str:
    """The text of the {...} block whose opening brace is at open_at."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at : i + 1]
    raise AssertionError("unbalanced braces")


def _prices() -> dict[str, float]:
    return {m.group(1): float(m.group(2)) for m in re.finditer(r'"([a-z0-9-]+\.[a-z0-9]+)"\s*=\s*([0-9.]+)', _tf("prices.tf"))}


def _default_classes() -> dict[str, dict]:
    text = _strip_comments(_tf("variables.tf"))
    var = text[text.index('variable "classes"'):]
    default = var[var.index("default = {") + len("default = ") :]
    body = _block(default, 0)
    out: dict[str, dict] = {}
    for m in re.finditer(r'"([a-z0-9-]+)"\s*=\s*\{', body):
        cls = _block(body, m.end() - 1)
        types = re.search(r"instance_types\s*=\s*\[([^\]]*)\]", cls)
        mc = re.search(r"max_count\s*=\s*([0-9]+)", cls)
        cap = re.search(r'capacity\s*=\s*"([a-z-]+)"', cls)
        out[m.group(1)] = {
            "types": re.findall(r'"([^"]+)"', types.group(1)) if types else [],
            "max_count": int(mc.group(1)) if mc else None,
            "capacity": cap.group(1) if cap else None,
        }
    return out


def _worst_case(classes: dict[str, dict], prices: dict[str, float]) -> float:
    return sum(c["max_count"] * max(prices[t] for t in c["types"]) * 730 for c in classes.values())


# ---- the classes ----------------------------------------------------------------------------------------------------

def test_the_two_default_classes_exist_with_the_stated_shapes():
    classes = _default_classes()
    assert set(classes) == {"ci-small", "ci-medium"}
    assert classes["ci-small"]["max_count"] == 4 and classes["ci-medium"]["max_count"] == 2
    assert all(c["capacity"] == "spot" for c in classes.values())


def test_every_default_class_has_a_max_count_and_the_default_fleet_is_at_most_six_machines():
    classes = _default_classes()
    assert classes and all(isinstance(c["max_count"], int) for c in classes.values()), classes
    assert sum(c["max_count"] for c in classes.values()) <= 6


def test_max_count_is_required_by_the_variable_type_not_optional():
    text = _strip_comments(_tf("variables.tf"))
    assert re.search(r"\bmax_count\s*=\s*number\b", text), "max_count must be a required number in the classes type"
    assert not re.search(r"max_count\s*=\s*optional", text)


def test_every_class_listed_is_validated_for_a_whole_nonnegative_ceiling():
    text = _tf("variables.tf")
    assert "max_count >= 0" in text and "floor(c.max_count)" in text


def test_each_runner_class_is_built_from_var_classes_with_its_own_max_count_as_the_ceiling():
    main = _strip_comments(_tf("main.tf"))
    assert re.search(r"for\s+name,\s*c\s+in\s+var\.classes\s*:\s*name\s*=>", main)
    assert re.search(r"runners_maximum_count\s*=\s*c\.max_count", main)
    # -1 is the module's "no limit"; nothing may set it
    assert not re.search(r"runners_maximum_count\s*=\s*-1", main)


def test_launches_are_serialised_so_the_ceiling_is_exact():
    assert re.search(r"scale_up_reserved_concurrent_executions\s*=\s*1\b", _strip_comments(_tf("main.tf")))


# ---- the budget check -----------------------------------------------------------------------------------------------

def test_default_classes_are_priced_and_under_the_default_budget():
    prices = _prices()
    classes = _default_classes()
    unpriced = {t for c in classes.values() for t in c["types"]} - set(prices)
    assert not unpriced, f"default instance types missing from prices.tf: {unpriced}"
    worst = _worst_case(classes, prices)
    assert worst == pytest.approx(DEFAULT_WORST_CASE, abs=0.01)
    budget = float(re.search(r'variable "monthly_budget_usd".*?default\s*=\s*([0-9.]+)', _tf("variables.tf"), re.S).group(1))
    assert worst <= budget


def test_price_table_is_dated_us_east_1_and_positive():
    text = _tf("prices.tf")
    assert re.search(r'price_table_date\s*=\s*"\d{4}-\d{2}-\d{2}"', text)
    assert re.search(r'price_table_region\s*=\s*"us-east-1"', text)
    assert re.search(r"hours_per_month\s*=\s*730\b", text)
    prices = _prices()
    assert len(prices) >= 10 and all(p > 0 for p in prices.values())
    # larger size of the same family costs more
    assert prices["m7i.xlarge"] > prices["m7i.large"]


def test_budget_check_exists_fails_the_plan_and_covers_every_class():
    main = _strip_comments(_tf("main.tf"))
    guard = main[main.index('resource "terraform_data" "budget_guard"'):]
    guard = _block(guard, guard.index("{"))
    assert "precondition" in guard
    assert "worst_case_monthly_usd <= var.monthly_budget_usd" in guard
    # the failure message names the total (and the per-class shares), never silent
    assert "format(\"%.2f\", local.worst_case_monthly_usd)" in guard and "worst_case_breakdown" in guard
    # unpriced types fail the plan instead of pricing at zero
    assert "unpriced_instance_types" in guard
    # the figure sums EVERY class, each at max_count x dearest listed type
    assert re.search(r"for\s+name,\s*c\s+in\s+var\.classes\s*:\s*name\s*=>\s*c\.max_count\s*\*\s*local\.class_dearest_hourly_usd\[name\]\s*\*\s*local\.hours_per_month", main)
    assert re.search(r"worst_case_monthly_usd\s*=\s*sum\(values\(local\.class_worst_case_monthly_usd\)\)", main)
    assert re.search(r"max\(\[for t in c\.instance_types", main)


def test_runner_module_is_gated_behind_the_guards():
    main = _strip_comments(_tf("main.tf"))
    assert re.search(r"depends_on\s*=\s*\[[^\]]*terraform_data\.budget_guard", main)


def test_python_mirror_of_the_check_fails_an_oversized_class_and_passes_the_kill_switch():
    prices = _prices()
    classes = _default_classes()
    big = {**classes, "ci-small": {**classes["ci-small"], "max_count": 20}}
    assert _worst_case(big, prices) > 700
    off = {n: {**c, "max_count": 0} for n, c in classes.items()}
    assert _worst_case(off, prices) == 0


# ---- budget alarm ---------------------------------------------------------------------------------------------------

def test_budget_alarm_at_80_and_100_percent_to_the_variable_address():
    text = _strip_comments(_tf("budget.tf"))
    assert 'resource "aws_budgets_budget" "fleet"' in text
    assert re.findall(r"threshold\s*=\s*(\d+)", text) == ["80", "100"]
    assert len(re.findall(r"subscriber_email_addresses\s*=\s*\[var\.budget_alert_email\]", text)) == 2
    assert "format(\"%.2f\", var.monthly_budget_usd)" in text


# ---- shape of the fleet ---------------------------------------------------------------------------------------------

def test_module_is_pinned_ephemeral_spot_first_with_bounded_failover():
    main = _strip_comments(_tf("main.tf"))
    assert 'github-aws-runners/github-runner/aws//modules/multi-runner' in main
    assert re.search(r'version\s*=\s*"7\.11\.0"', main)
    assert re.search(r"enable_ephemeral_runners\s*=\s*true", main)
    assert "price-capacity-optimized" in main
    assert "enable_on_demand_failover_for_errors" in main and "MaxSpotInstanceCountExceeded" in main
    assert "http_tokens" in main and '"required"' in main  # IMDSv2 only


def test_required_variables_are_the_ones_the_owner_named():
    text = _tf("variables.tf")
    for name in ("github_org", "github_app", "vpc_id", "classes", "monthly_budget_usd", "aws_region", "prefix", "budget_alert_email"):
        assert f'variable "{name}"' in text, name
    # no default for the things only the owner can supply
    for name in ("github_org", "github_app", "budget_alert_email"):
        block = _block(text, text.index(f'variable "{name}"') + len(f'variable "{name}" '))
        assert not re.search(r"^\s*default\s*=", block, re.M), name


# ---- no secrets -----------------------------------------------------------------------------------------------------

def test_no_secret_literal_anywhere_in_the_module():
    secret_shapes = [
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a PEM block"),
        (r"\bAKIA[0-9A-Z]{16}\b", "an AWS access key id"),
        (r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", "a GitHub token"),
        (r'"(?=[^"]*[A-Z])(?=[^"]*[a-z])(?=[^"]*[0-9])[A-Za-z0-9+/=_]{40,}"', "a long opaque string literal"),
    ]
    for path in sorted(FLEET.rglob("*")):
        if not path.is_file() or path.suffix not in {".tf", ".hcl", ".example", ".sh"}:
            continue
        text = path.read_text()
        for pattern, what in secret_shapes:
            if path.name == "download-lambdas.sh" and what == "a long opaque string literal":
                continue  # the pinned sha256 of release assets are public checksums, not secrets
            assert not re.search(pattern, text), f"{path.name} holds {what}"


def test_secrets_are_ssm_names_never_values():
    main = _strip_comments(_tf("main.tf"))
    variables = _strip_comments(_tf("variables.tf"))
    # the module gets the key and the webhook secret only through *_ssm blocks
    assert "key_base64_ssm" in main and "webhook_secret_ssm" in main
    assert not re.search(r"\b(key_base64|webhook_secret)\s*=\s*(\"|var\.)", main), "a secret value passed to the module"
    # the variable holds names and demands a leading slash
    assert "key_base64_ssm_parameter_name" in variables and "webhook_secret_ssm_parameter_name" in variables
    assert 'startswith(var.github_app.key_base64_ssm_parameter_name, "/")' in variables
    # the example file has names and an App id, no key material
    example = (FLEET / "terraform.tfvars.example").read_text()
    assert "ssm_parameter_name" in example and "BEGIN" not in example


def test_state_and_secrets_files_are_gitignored():
    ignore = (FLEET / ".gitignore").read_text().split()
    for entry in ("lambdas/", ".terraform/", "terraform.tfvars", "*.tfstate"):
        assert entry in ignore, entry


# ---- the README -----------------------------------------------------------------------------------------------------

def test_readme_has_the_owner_steps_and_not_the_mac_dns_workaround():
    text = README.read_text()
    for needle in ("GitHub App", "SecureString", "terraform init", "terraform plan", "terraform apply", "download-lambdas.sh",
                   "webhook_endpoint", "max_count", "Kill switch", "Refresh the price table", "monthly_budget_usd",
                   "Reading the plan", "destroy"):
        assert needle in text, needle
    assert "curloptResolve" not in text and "DNS workaround" not in text


def test_no_file_of_the_package_infra_mentions_the_mac_dns_workaround():
    for path in list(FLEET.rglob("*")) + [README]:
        if path.is_file() and path.suffix in {".tf", ".hcl", ".md", ".sh", ".example"}:
            assert "curloptResolve" not in path.read_text(), path.name


# ---- terraform itself (only when installed) -------------------------------------------------------------------------

TERRAFORM = shutil.which("terraform")


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_") and k not in {"AWS_PROFILE"}}
    env.update({"AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null", "AWS_DEFAULT_REGION": "us-east-1",
                "AWS_EC2_METADATA_DISABLED": "true", "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"})
    return env


def _git_resolve_env(env: dict[str, str]) -> dict[str, str]:
    """If github.com does not resolve here, pin its address for git (module downloads) in this one command's environment."""
    try:
        socket.gethostbyname("github.com")
        socket.gethostbyname("codeload.github.com")
        return env
    except OSError:
        pass
    pairs = []
    for host in ("github.com", "codeload.github.com"):
        out = subprocess.run(["host", host], capture_output=True, text=True).stdout
        m = re.search(r"has address (\S+)", out)
        if m:
            pairs.append(f"{host}:443:{m.group(1)}")
    env = dict(env, GIT_CONFIG_COUNT=str(len(pairs)))
    for i, p in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = "http.curloptResolve"
        env[f"GIT_CONFIG_VALUE_{i}"] = p
    return env


@pytest.fixture(scope="module")
def scratch(tmp_path_factory):
    if TERRAFORM is None:
        pytest.skip("terraform is not installed")
    dest = tmp_path_factory.mktemp("fleet")
    shutil.copytree(FLEET, dest / "fleet", ignore=shutil.ignore_patterns(".terraform", "lambdas", "*.tfstate*"))
    return dest / "fleet"


def _run(args, cwd, env):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=900)


def test_terraform_fmt_is_clean(scratch):
    r = _run([TERRAFORM, "fmt", "-check", "-recursive", "-diff"], scratch, _clean_env())
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.fixture(scope="module")
def initialised(scratch):
    r = _run([TERRAFORM, "init", "-backend=false", "-input=false", "-no-color"], scratch, _git_resolve_env(_clean_env()))
    if r.returncode != 0:
        if re.search(r"Could not resolve host|Failed to download|connection|timed out|dial tcp", r.stdout + r.stderr, re.I):
            pytest.skip("module download needs the network: " + (r.stdout + r.stderr)[-300:])
        pytest.fail(r.stdout + r.stderr)
    # the module wants its three Lambda zips on disk to plan; placeholders are enough for validate and the mocked tests
    (scratch / "lambdas").mkdir(exist_ok=True)
    for n in ("webhook", "runners", "runner-binaries-syncer"):
        (scratch / "lambdas" / f"{n}.zip").write_bytes(b"placeholder")
    return scratch


def test_terraform_validate_passes(initialised):
    r = _run([TERRAFORM, "validate", "-no-color"], initialised, _clean_env())
    assert r.returncode == 0, r.stdout + r.stderr


def test_terraform_test_the_budget_guard_under_a_mocked_provider(initialised):
    r = _run([TERRAFORM, "test", "-no-color"], initialised, _clean_env())
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert "5 passed, 0 failed" in r.stdout
