import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_ollama_is_private_and_model_init_pulls_the_approved_model():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    ollama = compose["services"]["ollama"]
    model_init = compose["services"]["model-init"]
    backend_environment = compose["services"]["backend"]["environment"]

    assert ollama["image"] == "ollama/ollama:0.32.1"
    assert "ports" not in ollama
    assert ollama["volumes"] == ["ollama-data:/root/.ollama"]
    assert model_init["command"] == ["pull", "qwen3:1.7b-q4_K_M"]
    assert model_init["restart"] == "no"
    assert model_init["depends_on"]["ollama"]["condition"] == "service_healthy"
    assert backend_environment["DEMO_OLLAMA_BASE_URL"] == "http://ollama:11434"
    assert backend_environment["DEMO_OLLAMA_MODEL"] == "qwen3:1.7b-q4_K_M"
    assert "ollama-data" in compose["volumes"]


def test_compose_enables_auth_and_never_publishes_ollama():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    backend_environment = compose["services"]["backend"]["environment"]

    assert backend_environment["DEMO_AUTH_REQUIRED"] == "${DEMO_AUTH_REQUIRED:-true}"
    assert "x-required-auth" not in compose
    assert backend_environment["DEMO_AUTH_USERNAME"] == (
        "${DEMO_AUTH_USERNAME:?set DEMO_AUTH_USERNAME in .env}"
    )
    assert backend_environment["DEMO_AUTH_PASSWORD_HASH"] == (
        "${DEMO_AUTH_PASSWORD_HASH:?set DEMO_AUTH_PASSWORD_HASH in .env}"
    )
    assert backend_environment["DEMO_SESSION_SECRET"] == (
        "${DEMO_SESSION_SECRET:?set DEMO_SESSION_SECRET in .env}"
    )
    assert compose["services"]["backend"]["env_file"] == [
        "${COMPOSE_ENV_FILE_PATH:-.env}"
    ]
    assert "ports" not in compose["services"]["ollama"]
    assert compose["services"]["frontend"]["ports"] == ["8080:80"]

    frontend_definition = json.dumps(
        compose["services"]["frontend"], ensure_ascii=False
    )
    assert "DEMO_AUTH_PASSWORD_HASH" not in frontend_definition
    assert "DEMO_SESSION_SECRET" not in frontend_definition


def test_example_environment_documents_auth_without_shipping_credentials():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "DEMO_AUTH_REQUIRED=true" in example
    assert "DEMO_AUTH_USERNAME=" in example
    assert "DEMO_AUTH_PASSWORD_HASH=" in example
    assert "DEMO_SESSION_SECRET=" in example
    assert not re.search(r"(?m)^DEMO_AUTH_PASSWORD_HASH=.+$", example)
    assert not re.search(r"(?m)^DEMO_SESSION_SECRET=.+$", example)


def test_readme_covers_the_full_server_operations_and_secret_boundary():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required_phrases = (
        "/opt/local-llm-demo",
        "/opt/backups/local-llm-demo-",
        "backend-data",
        "ollama-data",
        "scripts/generate-demo-auth.ps1",
        "docker compose up -d ollama",
        "docker compose run --rm model-init",
        "docker compose exec ollama ollama list",
        "qwen3:1.7b-q4_K_M",
        "docker compose up --build -d backend frontend",
        "docker compose logs",
        "curl -fsS http://127.0.0.1:8080/api/health",
        "docker compose restart",
        "DEPLOY_SHA=",
        "git status --porcelain",
        'git fetch --no-tags origin "${DEPLOY_SHA}"',
        'test "$(git rev-parse FETCH_HEAD)" = "${DEPLOY_SHA}"',
        'git checkout --detach "${DEPLOY_SHA}"',
        'test "$(git rev-parse HEAD)" = "${DEPLOY_SHA}"',
        "https://git.code.tencent.com/hongxulan/LLM-demo-local-deoplyment.git",
        "在设置页把“运维知识库”切换为 Local",
        "在设置页把“坐席辅助”单独切换为 Local",
        "已下载",
        "已加载",
        "轮换",
        "回滚",
        "一次性",
        "HTTP",
    )

    for phrase in required_phrases:
        assert phrase in readme

    assert "sudo " not in readme
    assert "盲目执行 `git pull`" in readme


def test_readme_linux_operations_fail_fast_and_rollback_validates_before_mutation():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    bash_blocks = re.findall(r"```bash\n(.*?)\n```", readme, flags=re.DOTALL)

    assert bash_blocks
    assert all(block.startswith("set -Eeuo pipefail\n") for block in bash_blocks)

    rollback = readme[readme.index("### 9. 回滚") :]
    host_validation = 'tar -tzf "${BACKUP_DIR}/backend-data.tgz"'
    read_only_container_validation = (
        'docker run --rm -v "${BACKUP_DIR}:/backup:ro" '
        "alpine:3.21 tar -tzf /backup/backend-data.tgz"
    )
    stop_services = "docker compose stop frontend backend"
    overwrite_source = (
        'rsync -a --delete "${BACKUP_DIR}/source/" /opt/local-llm-demo/'
    )
    container_validation = "tar -tzf /backup/backend-data.tgz"
    extract_to_temporary = 'tar -xzf /backup/backend-data.tgz -C "${RESTORE_DIR}"'
    delete_live_data = (
        'find /data -mindepth 1 -maxdepth 1 ! -path "${RESTORE_DIR}" '
        "-exec rm -rf -- {} +"
    )
    copy_restored_data = 'cp -a "${RESTORE_DIR}/." /data/'
    all_container_query = (
        'BACKEND_CONTAINER="$(docker compose ps --all -q backend)"'
    )
    inspect_container = (
        'BACKEND_VOLUME="$(docker inspect "${BACKEND_CONTAINER}"'
    )

    assert rollback.index(host_validation) < rollback.index(stop_services)
    assert rollback.index(host_validation) < rollback.index(overwrite_source)
    assert rollback.index(read_only_container_validation) < rollback.index(
        stop_services
    )
    assert rollback.index(read_only_container_validation) < rollback.index(
        overwrite_source
    )
    assert rollback.index(read_only_container_validation) < rollback.index(
        delete_live_data
    )
    assert rollback.index(container_validation) < rollback.index(extract_to_temporary)
    assert rollback.index(extract_to_temporary) < rollback.index(delete_live_data)
    assert rollback.index(delete_live_data) < rollback.index(copy_restored_data)
    assert rollback.index(all_container_query) < rollback.index(inspect_container)
    assert rollback.index(inspect_container) < rollback.index(stop_services)
    assert "docker compose ps -q backend" not in rollback
    assert "恢复失败时服务保持停止" in rollback
    assert "修正问题后安全重试" in rollback


def test_nginx_allows_multipart_overhead_above_twenty_mibibytes():
    nginx = (ROOT / "docker" / "nginx.conf").read_text(encoding="utf-8")

    assert "client_max_body_size 22m;" in nginx
    assert "client_max_body_size 20m;" not in nginx


def test_nginx_preserves_the_public_host_port_for_same_origin_checks():
    nginx = (ROOT / "docker" / "nginx.conf").read_text(encoding="utf-8")

    assert "proxy_set_header Host $http_host;" in nginx
    assert "proxy_set_header Host $host;" not in nginx


def test_production_proxy_topology_uses_unspoofable_client_ip_for_rate_limits():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    backend_dockerfile = (ROOT / "docker" / "backend.Dockerfile").read_text(
        encoding="utf-8"
    )
    nginx = (ROOT / "docker" / "nginx.conf").read_text(encoding="utf-8")

    assert "ports" not in compose["services"]["backend"]
    assert '"--proxy-headers"' in backend_dockerfile
    assert '"--forwarded-allow-ips", "*"' in backend_dockerfile
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" not in nginx


def test_review_timeline_media_query_contains_the_narrow_three_column_rule():
    css = (ROOT / "frontend" / "src" / "styles" / "global.css").read_text(
        encoding="utf-8"
    )

    narrow_rule = (
        "@media (max-width: 899px) { .review-layout, .review-findings-layout "
        "{ grid-template-columns: 1fr; } .review-timeline { grid-template-columns: "
        "repeat(3, minmax(0, 1fr)); } }"
    )
    assert narrow_rule in css
    desktop_css = css[: css.index(narrow_rule)]
    assert re.search(
        r"\.review-timeline\s*\{[^}]*grid-template-columns:\s*repeat\(6,",
        desktop_css,
    )


def test_bundled_call_scenarios_cover_the_four_approved_categories_with_twelve_templates():
    scenarios = json.loads(
        (ROOT / "backend" / "app" / "data" / "call_scenarios.json").read_text(
            encoding="utf-8"
        )
    )
    templates = json.loads(
        (ROOT / "backend" / "app" / "data" / "call_templates.json").read_text(
            encoding="utf-8"
        )
    )

    assert [scenario["category"] for scenario in scenarios] == [
        "故障报修",
        "业务咨询",
        "服务投诉",
        "结果回访",
    ]
    assert len(templates) == 12
    assert {template["scenario_id"] for template in templates} == {
        scenario["id"] for scenario in scenarios
    }
    assert all(
        len([template for template in templates if template["scenario_id"] == scenario["id"]]) == 3
        for scenario in scenarios
    )
