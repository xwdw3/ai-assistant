"""Web 入口：把「规划 → 执行」包成单页应用，监听 $PORT。

只用标准库，不引入额外依赖。核心逻辑仍然在 agent.py，这里只负责：
    1. 接收任务、校验参数
    2. 把 on_event 的进度用 NDJSON 流式推给浏览器
    3. 按访客提供的 API Key 建客户端，互不串台
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agent
from agent import AgentError, Budget, ConfigError, execute, make_plan

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")
MAX_TASK_LEN = 2000       # 任务描述上限，挡住超长请求
MAX_BODY = 64 * 1024      # 请求体上限
MAX_PLAN_STEPS = 12       # 计划步骤上限，防止模型输出超长数组
MAX_CONCURRENT_RUNS = 2   # 同时执行的任务数，保住服务端额度不被打满

# 并发闸门：拿不到就直接 429，而不是排队堆积
_slots = threading.Semaphore(MAX_CONCURRENT_RUNS)


def _bind_client(api_key: str) -> None:
    """为当前线程绑定客户端。访客没填 Key 时回落到服务端配置。"""
    key = (api_key or "").strip()
    if not key:
        agent.bind_client(None)
        agent.get_client()  # 没配 Key 会在这里抛 ConfigError，提前暴露
        return
    if key == agent.API_KEY:  # 和服端一致就复用单例，省一次握手
        agent.bind_client(None)
        return
    agent.bind_client(agent.build_client(key))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AIAssistant/1.0"

    # ---- 基础设施 ----

    def log_message(self, fmt: str, *args) -> None:  # 收敛访问日志，别刷屏
        sys.stderr.write("[web] %s %s\n" % (self.address_string(), fmt % args))

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        """读并解析请求体，出错时已经回过响应，返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json({"ok": False, "error": "Content-Length 不合法"}, 400)
            return None
        if length <= 0:
            self._json({"ok": False, "error": "请求体为空"}, 400)
            return None
        if length > MAX_BODY:
            self._json({"ok": False, "error": "请求体过大"}, 413)
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            return None
        if not isinstance(data, dict):
            self._json({"ok": False, "error": "请求体必须是 JSON 对象"}, 400)
            return None
        return data

    # ---- 流式输出 ----

    def _start_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")  # 让反代别缓存流
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_chunk(self, data: bytes) -> bool:
        try:
            self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False  # 浏览器关了页面，别把异常抛出去

    def _end_stream(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- 路由 ----

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            # 页面里已用 data URI 声明图标，这里回 204 只是为了消掉浏览器的 404 噪音
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/healthz":
            self._json({"ok": True, "model": agent.MODEL})
        elif path == "/api/config":
            # 只告诉前端「服务端有没有配 Key」，绝不回传 Key 本身
            self._json({"ok": True, "server_key": bool(agent.API_KEY), "model": agent.MODEL})
        elif path.startswith("/api/"):
            self._json({"ok": False, "error": "接口不存在：" + path}, 404)
        else:
            self._not_found(path)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/plan":
            self._handle_plan()
        elif path == "/api/run":
            self._handle_run()
        else:
            self._json({"ok": False, "error": "接口不存在：" + path}, 404)

    def _serve_file(self, file_path: Path, content_type: str) -> None:
        try:
            body = file_path.read_bytes()
        except OSError:
            self._json({"ok": False, "error": "页面文件缺失"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self, path: str) -> None:
        """浏览器直接访问错误路径时给个能看懂的页面，而不是一段裸 JSON。"""
        # 回跳链接要带上原查询串，否则会丢掉 sharecode 被网关拦成 403
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        back = "/" + (f"?{query}" if query else "")
        safe_path = path.replace("<", "&lt;").replace(">", "&gt;")
        page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>页面不存在</title>
<style>
 body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
   background:#0e1117;color:#e6e9ef;font:15px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
 .box{{max-width:460px;padding:36px 32px;text-align:center}}
 h1{{font-size:44px;margin:0;color:#7c9cff;letter-spacing:2px}}
 p{{color:#8b95a7;margin:14px 0}}
 code{{background:#161b24;border:1px solid #252c3a;border-radius:6px;padding:2px 8px;color:#e6e9ef}}
 a{{display:inline-block;margin-top:18px;background:#7c9cff;color:#0b1020;text-decoration:none;
   font-weight:600;padding:10px 22px;border-radius:10px}}
</style></head>
<body><div class="box">
 <h1>404</h1>
 <p>没有这个页面：<code>{safe_path}</code></p>
 <p>正确的入口只有一个，就是应用的根地址。</p>
 <a href="{back}">回到首页</a>
</div></body></html>"""
        body = page.encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- 业务 ----

    def _validate_task(self, data: dict) -> str | None:
        task = str(data.get("task") or "").strip()
        if not task:
            self._json({"ok": False, "error": "任务不能为空"}, 400)
            return None
        if len(task) > MAX_TASK_LEN:
            self._json({"ok": False, "error": f"任务描述太长（上限 {MAX_TASK_LEN} 字）"}, 400)
            return None
        return task

    def _handle_plan(self) -> None:
        data = self._read_json()
        if data is None:
            return
        task = self._validate_task(data)
        if task is None:
            return

        try:
            _bind_client(str(data.get("apiKey") or ""))
        except ConfigError as exc:
            self._json({"ok": False, "kind": "config", "error": str(exc)}, 400)
            return

        budget = Budget()
        try:
            plan = make_plan(task, budget=budget)[:MAX_PLAN_STEPS]
            self._json({"ok": True, "plan": plan, "usage": budget.summary()})
        except ConfigError as exc:
            self._json({"ok": False, "kind": "config", "error": str(exc)}, 400)
        except AgentError as exc:
            self._json({"ok": False, "kind": "agent", "error": str(exc)}, 502)
        except Exception as exc:
            traceback.print_exc()
            self._json(
                {"ok": False, "kind": "internal", "error": f"{type(exc).__name__}: {exc}"}, 500
            )
        finally:
            agent.bind_client(None)

    def _handle_run(self) -> None:
        data = self._read_json()
        if data is None:
            return
        task = self._validate_task(data)
        if task is None:
            return

        raw_plan = data.get("plan")
        if not isinstance(raw_plan, list) or not raw_plan:
            self._json({"ok": False, "error": "缺少执行计划，请先生成计划"}, 400)
            return
        plan = [str(s).strip() for s in raw_plan if str(s).strip()][:MAX_PLAN_STEPS]
        if not plan:
            self._json({"ok": False, "error": "计划内容为空"}, 400)
            return

        try:
            _bind_client(str(data.get("apiKey") or ""))
        except ConfigError as exc:
            self._json({"ok": False, "kind": "config", "error": str(exc)}, 400)
            return

        if not _slots.acquire(blocking=False):
            agent.bind_client(None)
            self._json(
                {"ok": False, "error": "当前正在执行的任务较多，请稍后再试"}, 429
            )
            return

        alive = True
        try:
            budget = Budget()
            self._start_stream()

            def emit(payload: dict) -> None:
                nonlocal alive
                if not alive:
                    return
                line = json.dumps(payload, ensure_ascii=False) + "\n"
                alive = self._write_chunk(line.encode("utf-8"))

            emit({"type": "start", "model": agent.MODEL})
            try:
                result = execute(
                    task,
                    plan,
                    on_event=lambda msg: emit({"type": "event", "text": msg}),
                    budget=budget,
                )
                emit({"type": "done", "result": result, "usage": budget.summary()})
            except ConfigError as exc:
                emit({"type": "error", "kind": "config", "message": str(exc)})
            except AgentError as exc:
                emit(
                    {
                        "type": "error",
                        "kind": "agent",
                        "message": str(exc),
                        "usage": budget.summary(),
                    }
                )
            except Exception as exc:
                traceback.print_exc()
                emit(
                    {
                        "type": "error",
                        "kind": "internal",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
            self._end_stream()
        finally:
            _slots.release()
            agent.bind_client(None)


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"AI 个人助理已启动：http://{HOST}:{PORT}（模型 {agent.MODEL}）", flush=True)
    if not agent.API_KEY:
        print("提示：服务端未配置 DEEPSEEK_API_KEY，访客需在页面上填写自己的 Key。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
