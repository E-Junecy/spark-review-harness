"""适配层·Web 前端：把 HTTP 请求转成对 `AgentService` 的三个调用。

设计期写着"前端，先用最简单的 webui"，所以 `GET /` 真给了一页 HTML——只有 JSON
接口不算兑现。三个路由：`GET /`、`GET /sessions`、`POST /chat {session_id?, message}`。

无鉴权、单页 HTML，仅用于演示"前端属于适配层"与本地联调。前端请求是外部输入：
格式坏了回 400，不能把 handler 抛死（实测 PowerShell 的 `curl -d` 发中文 body 会变成非法 JSON，
原先整个 handler 崩掉、客户端只看到空响应）。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from adapters.wire import to_wire
from app.service import AgentService

PAGE = """<!doctype html><html lang=zh><meta charset=utf-8>
<title>spark harness</title>
<style>body{font:14px/1.6 system-ui;max-width:46rem;margin:2rem auto;padding:0 1rem}
pre{background:#f6f6f8;padding:.8rem;white-space:pre-wrap;border-radius:6px}
textarea{width:100%;height:5em}input{width:100%}</style>
<h3>在线 assistant + 本地 spark reviewer</h3>
<pre id=log>（发一条任务，工具调用会先过审核）</pre>
<label>session_id（留空则新建）<input id=sid></label>
<form id=f><textarea id=task placeholder="例如：创建 notes.txt，内容为 hello"></textarea>
<button>发送</button></form>
<script>
const log=document.getElementById('log'),sid=document.getElementById('sid'),task=document.getElementById('task');
async function send(e){e.preventDefault();log.textContent+='\\n> '+task.value+'\\n';
  const r=await fetch('/chat',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({session_id:sid.value||null,message:task.value})}),j=await r.json();
  if(!r.ok){log.textContent+='! '+j.detail;return}
  sid.value=j.session_id;
  for(const m of j.produced){
    const t=m.tool_calls?m.tool_calls.map(c=>c.function.name+' '+c.function.arguments).join('; '):m.content;
    log.textContent+='['+m.role+'] '+t+'\\n'}
  task.value=''}
document.getElementById('f').onsubmit=send;
</script>
"""


class WebApi:
    def __init__(self, service: AgentService, host: str = "127.0.0.1", port: int = 8000):
        self.service, self.host, self.port = service, host, port

    def server(self) -> ThreadingHTTPServer:
        """单独拆出来是为了测试能真起一个服务（临时端口），而不是模拟 handler。"""
        service, store = self.service, self.service.session_store

        class Handler(BaseHTTPRequestHandler):
            def reply(self, payload, code=200, content_type="application/json; charset=utf-8", raw=False):
                body = payload if raw else json.dumps(payload, ensure_ascii=False, default=str)
                body = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler 命名约定)
                if self.path == "/":
                    return self.reply(PAGE, content_type="text/html; charset=utf-8", raw=True)
                if self.path == "/sessions":
                    return self.reply([{"id": s.id, "messages": len(s.messages)} for s in service.list_sessions()])
                return self.reply({"detail": "unknown path"}, 404)

            def do_POST(self):  # noqa: N802
                if self.path != "/chat":
                    return self.reply({"detail": "unknown path"}, 404)
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                    message = str(body.get("message", "")).strip() if isinstance(body, dict) else ""
                    if not message:
                        return self.reply({"detail": "missing 'message'"}, 400)
                    session = store.load(body["session_id"]) if body.get("session_id") else service.new_session()
                except (json.JSONDecodeError, KeyError, OSError) as exc:
                    return self.reply({"detail": f"bad request: {exc}"}, 400)
                return self.reply({"session_id": session.id,
                                   "produced": [to_wire(m) for m in service.chat(session.id, message)]})

            def log_message(self, *_args):  # 安静：审计交给 jsonl 轨迹
                pass

        return ThreadingHTTPServer((self.host, self.port), Handler)

    def run(self) -> None:
        print(f"WebApi on http://{self.host}:{self.port}  (GET /, GET /sessions, POST /chat)")
        self.server().serve_forever()
