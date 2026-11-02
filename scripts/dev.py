#!/usr/bin/env python3
"""Check local dependencies, prepare the database, and run the web/API pair."""

from __future__ import annotations

import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "services" / "api"
API_PYTHON = API_DIR / ".venv" / "bin" / "python"
WEB_DIR = ROOT / "apps" / "web"
API_URL = "http://127.0.0.1:8001"
WEB_URL = "http://127.0.0.1:5174"
COMPOSE = ["docker", "compose", "-f", "infra/compose.yaml"]


class StartupError(Exception):
    pass


def request(path: str, *, timeout: float = 2.0) -> tuple[int | None, bytes]:
    try:
        with urlopen(f"{API_URL}{path}", timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
    except (URLError, TimeoutError, OSError):
        return None, b""


def wait_for_api(process: subprocess.Popen[bytes] | None = None) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise StartupError("FastAPI 在启动检查通过前退出；请查看上方 API 日志。")
        status, _ = request("/health")
        if status == 200:
            account_status, body = request("/api/v1/auth/me")
            if account_status == 200:
                try:
                    identity = json.loads(body)
                except json.JSONDecodeError as error:
                    raise StartupError("账套接口返回了无法识别的数据。") from error
                if identity.get("account_set_id"):
                    return identity
            elif account_status is not None:
                raise StartupError(
                    f"FastAPI 已启动，但账套接口返回 HTTP {account_status}。"
                    "请检查 services/api/.env 中的 DEMO_MODE=true 和数据库连接。"
                )
        time.sleep(0.5)
    raise StartupError("等待 FastAPI 健康检查超时；请查看上方 API 日志。")


def wait_for_web(process: subprocess.Popen[bytes] | None = None) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise StartupError("Vue 前端在启动检查通过前退出；请查看上方前端日志。")
        try:
            with urlopen(WEB_URL, timeout=2) as response:
                if response.status == 200:
                    return
        except (HTTPError, URLError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    raise StartupError("等待 Vue 前端超时；请查看上方前端日志。")


def port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def stop_children(children: list[subprocess.Popen[bytes]]) -> None:
    for child in children:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for child in children:
        if child.poll() is None:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()


def run_command(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, env=env)
    if result.returncode:
        raise StartupError(f"命令失败（退出码 {result.returncode}）：{' '.join(command)}")


def main() -> int:
    os.chdir(ROOT)
    children: list[subprocess.Popen[bytes]] = []
    try:
        if shutil.which("docker") is None:
            raise StartupError("找不到 Docker 命令，请先安装 Docker Desktop 或 Docker Engine。")
        if not API_PYTHON.is_file():
            raise StartupError("找不到 services/api/.venv，请先按 README 安装 API 依赖。")
        if shutil.which("npm") is None or not (WEB_DIR / "node_modules").is_dir():
            raise StartupError("前端依赖未安装；请先在 apps/web 运行 npm install。")

        # Docker Desktop also exposes a Windows-only `desktop-linux` context
        # through the shared Docker configuration. In WSL, use the integrated
        # Unix socket instead of that npipe endpoint.
        docker_env = os.environ.copy()
        docker_env["DOCKER_CONTEXT"] = "default"
        docker = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            env=docker_env,
        )
        if docker.returncode:
            detail = docker.stderr.strip().splitlines()
            message = detail[0] if detail else "未知连接错误"
            raise StartupError(
                "无法通过 WSL 的 Docker Desktop 集成连接 Docker Engine："
                f"{message}。请检查 Docker Desktop 的 WSL Integration。"
            )
        print(f"[1/6] Docker Engine 正常（版本 {docker.stdout.strip()}）", flush=True)

        sys.path.insert(0, str(API_DIR))
        sys.path.insert(0, str(ROOT / "packages"))
        try:
            from sqlalchemy import create_engine, text
            from sqlalchemy.engine import make_url
            from app.core.config import settings
        except ImportError as error:
            raise StartupError("API 环境缺少依赖；请按 README 安装 services/api 依赖。") from error

        database_url = make_url(settings.database_url)
        if not database_url.password:
            raise StartupError("services/api/.env 的 DATABASE_URL 没有数据库密码。")
        if database_url.username != "zhangqing" or database_url.database != "zhangqing":
            raise StartupError("DATABASE_URL 应连接本地账套数据库 zhangqing（用户 zhangqing）。")
        if database_url.host not in {"127.0.0.1", "localhost"}:
            raise StartupError("本地开发 DATABASE_URL 应连接 127.0.0.1 上的 PostgreSQL。")
        if database_url.port not in {None, 5433}:
            raise StartupError(
                f"当前 Compose 将 PostgreSQL 暴露在 5433，但 DATABASE_URL 使用 {database_url.port}；"
                "请修正 services/api/.env。"
            )

        compose_env = os.environ.copy()
        compose_env["DOCKER_CONTEXT"] = "default"
        compose_env["POSTGRES_PASSWORD"] = database_url.password
        run_command(COMPOSE + ["up", "-d", "postgres"], cwd=ROOT, env=compose_env)
        print("[2/6] PostgreSQL 容器已启动", flush=True)

        if not port_is_open(database_url.host or "127.0.0.1", database_url.port or 5432):
            raise StartupError(
                f"PostgreSQL 端口 {database_url.port or 5432} 未开放；"
                "检查 Docker 容器状态及 services/api/.env 的 DATABASE_URL 端口。"
            )
        engine = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
        try:
            with engine.connect() as connection:
                connection.execute(text("select 1")).scalar_one()
        except Exception as error:
            detail = str(getattr(error, "orig", error)).lower()
            if "password authentication failed" in detail:
                raise StartupError(
                    "PostgreSQL 端口可达，但数据库认证失败。"
                    "请让 services/api/.env 的 DATABASE_URL 密码与现有 PostgreSQL 数据卷一致。"
                ) from error
            raise StartupError(
                "PostgreSQL 端口可达，但 DATABASE_URL 无法登录。"
                "请检查 services/api/.env 的数据库用户名、密码和数据库名。"
            ) from error
        finally:
            engine.dispose()
        print(
            f"[3/6] 数据库端口与认证正常（{database_url.host}:{database_url.port or 5432}，SELECT 1 通过）",
            flush=True,
        )

        run_command([str(API_PYTHON), "-m", "alembic", "upgrade", "head"], cwd=API_DIR)
        run_command([str(API_PYTHON), "-m", "app.seed_demo"], cwd=API_DIR)
        print("[4/6] 数据库迁移与本地测试账套就绪", flush=True)

        api_identity: dict
        if port_is_open("127.0.0.1", 8001):
            status, _ = request("/health")
            if status != 200:
                raise StartupError("8001 端口已被其他程序占用；请关闭占用程序后重试。")
            api_identity = wait_for_api()
            print("[5/6] 复用已运行的 FastAPI；健康检查与账套数据库检查通过", flush=True)
        else:
            api_process = subprocess.Popen(
                [str(API_PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8001"],
                cwd=API_DIR,
                start_new_session=True,
            )
            children.append(api_process)
            api_identity = wait_for_api(api_process)
            print("[5/6] FastAPI 已启动；健康检查与账套数据库检查通过", flush=True)

        web_process: subprocess.Popen[bytes] | None = None
        if port_is_open("127.0.0.1", 5174):
            try:
                with urlopen(WEB_URL, timeout=2) as response:
                    if response.status != 200:
                        raise StartupError("5174 端口已被其他程序占用；请关闭占用程序后重试。")
            except StartupError:
                raise
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                raise StartupError("5174 端口已被其他程序占用；请关闭占用程序后重试。") from error
            print("[6/6] 复用已运行的 Vue 前端", flush=True)
        else:
            web_process = subprocess.Popen(
                ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5174", "--strictPort"],
                cwd=WEB_DIR,
                start_new_session=True,
            )
            children.append(web_process)
            wait_for_web(web_process)
            print("[6/6] Vue 前端已启动，HTTP 检查通过", flush=True)

        try:
            with urlopen(f"{WEB_URL}/api/v1/auth/me", timeout=3) as response:
                proxy_identity = json.loads(response.read())
        except Exception as error:
            raise StartupError("Vue 页面可访问，但经前端代理读取账套失败。") from error
        if proxy_identity.get("account_set_id") != api_identity.get("account_set_id"):
            raise StartupError("Vue API 代理没有返回当前账套信息。")

        print("\n开发环境已就绪")
        print(f"企业 / 账套：{api_identity.get('tenant_id')} / {api_identity.get('account_set_id')}")
        print(f"前端：http://127.0.0.1:5174")
        print(f"API：http://127.0.0.1:8001")
        if children:
            print("按 Ctrl+C 停止本命令启动的 FastAPI 和 Vue；PostgreSQL 容器会继续运行。", flush=True)
            while True:
                for child in children:
                    if child.poll() is not None:
                        raise StartupError(f"服务进程意外退出（退出码 {child.returncode}）。")
                time.sleep(0.5)
        return 0
    except KeyboardInterrupt:
        print("\n正在停止本命令启动的 FastAPI 和 Vue…", flush=True)
        return 0
    except StartupError as error:
        print(f"\n启动检查失败：{error}", file=sys.stderr, flush=True)
        return 1
    finally:
        stop_children(children)


if __name__ == "__main__":
    raise SystemExit(main())
