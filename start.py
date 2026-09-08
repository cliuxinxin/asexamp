#!/usr/bin/env python3
"""Bootstrap and run the local TCG application using only standard-library tools."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import venv
import webbrowser

ROOT = Path(__file__).resolve().parent


def prepare_environment(environment: Path) -> tuple[Path, bool]:
    scripts = environment / ('Scripts' if os.name == 'nt' else 'bin')
    executable = scripts / ('python.exe' if os.name == 'nt' else 'python')
    repaired = not all(path.is_file() for path in (executable, environment / 'pyvenv.cfg', scripts / 'activate'))
    if repaired:
        print(f"正在创建或补全本地 Python 环境（Python {sys.version.split()[0]}）…", flush=True)
        # Finish activation scripts before bootstrapping pip. EnvBuilder's built-in
        # pip step captures stdout, hiding progress and leaving no activate script
        # when interrupted. Reuse the directory without clearing user files.
        venv.EnvBuilder(with_pip=False).create(environment)
    print('正在检查本地 pip…', flush=True)
    pip_check = subprocess.run([str(executable), '-m', 'pip', '--version'], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if pip_check.returncode != 0:
        print('正在初始化 pip（使用 Python 自带安装包，无需联网）；下方显示安装日志…', flush=True)
        subprocess.run([str(executable), '-u', '-m', 'ensurepip', '--upgrade', '--default-pip', '-v'], check=True)
        repaired = True
    return executable, repaired


def main() -> None:
    try:
        launch()
    except KeyboardInterrupt:
        print('\n启动准备已中断。重新运行 python3 start.py 可检查并继续补全环境。', flush=True)
        raise SystemExit(130) from None
    except subprocess.CalledProcessError as error:
        print('\n环境准备或依赖检查失败，请查看上方具体错误；修复后重新运行启动命令。', flush=True)
        raise SystemExit(error.returncode) from None


def launch() -> None:
    parser = argparse.ArgumentParser(description="TCG Case Agent 本地启动器")
    parser.add_argument("--log-level", choices=("debug", "info", "warning"), default="info", help="业务日志级别，默认 info")
    parser.add_argument("--access-log", action="store_true", help="额外显示 Uvicorn 全部访问日志（包含轮询）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--env-file", type=Path, help="指定固定 .env 文件；默认优先读取数据目录中的 .env")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--no-install", action="store_true", help="使用当前 Python 环境中已安装的依赖")
    parser.add_argument("--check", action="store_true", help="检查运行依赖后退出")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("需要 Python 3.11 或更新版本，推荐 Python 3.12。")
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间。")
    if args.env_file and not args.env_file.expanduser().is_file():
        parser.error("指定的 .env 文件不存在或不可读取。")
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        parser.error("缺少前端构建文件，请先在 frontend 目录运行 npm ci 和 npm run build。")

    executable = Path(sys.executable)
    if not args.no_install:
        environment = ROOT / ".venv"
        executable, repaired = prepare_environment(environment)
        requirements = ROOT / "requirements.txt"
        stamp = environment / "tcg-requirements.sha256"
        expected = hashlib.sha256(requirements.read_bytes()).hexdigest()
        if repaired or not stamp.exists() or stamp.read_text().strip() != expected:
            print("正在安装运行依赖；首次启动需要联网。", flush=True)
            subprocess.run([str(executable), "-m", "pip", "install", "-r", str(requirements)], check=True)
            stamp.write_text(expected + "\n")

    subprocess.run([str(executable), "-c", "import fastapi, uvicorn, langgraph.graph, langgraph.checkpoint.sqlite.aio, langchain, langchain_ollama, langchain_openai, docx, openpyxl, pypdf, cryptography"], check=True)
    if args.check:
        print("Python 依赖和前端文件检查通过。")
        return
    app_env = os.environ.copy()
    app_env["PYTHONPATH"] = str(ROOT / "backend")
    app_env["TCG_LOG_LEVEL"] = args.log_level.upper()
    app_env["TCG_DATA_DIR"] = str(args.data_dir.resolve())
    if args.env_file:
        app_env["TCG_ENV_FILE"] = str(args.env_file.expanduser().resolve())
    app_env["LANGSMITH_TRACING"] = "false"
    app_env["LANGCHAIN_TRACING_V2"] = "false"
    url = f"http://127.0.0.1:{args.port}"
    print(f"\nTCG Case Agent → {url}\n数据目录：{args.data_dir.resolve()}\n保持此终端运行；按 Ctrl+C 停止。\n", flush=True)
    print(f"业务日志：{args.data_dir.resolve() / 'logs' / 'tcg.log'}", flush=True)
    if not args.no_browser:
        timer = threading.Timer(2, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    try:
        subprocess.run([str(executable), "-m", "uvicorn", "tcg.main:app", "--host", "127.0.0.1", "--port", str(args.port), "--workers", "1", "--log-level", args.log_level, *( [] if args.access_log else ["--no-access-log"])], cwd=ROOT, env=app_env, check=True)
    except KeyboardInterrupt:
        pass
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from error


if __name__ == "__main__":
    main()
