"""Read-only LAN view of the native App's already validated frame package."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


PAGE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>安全帽检测 · 内网查看</title><style>
*{box-sizing:border-box}body{margin:0;background:#10202b;color:#edf6fc;font:16px system-ui,sans-serif}
main{max-width:1000px;margin:auto;padding:20px}h1{font-size:25px;margin:0 0 10px}
p{line-height:1.7;color:#bed1de}.fault{color:#f6c788}.viewer{position:relative;background:#08131c;
min-height:220px;border-radius:10px;overflow:hidden}img{width:100%;max-height:70vh;object-fit:contain;
display:block;visibility:hidden}#placeholder{position:absolute;inset:0;display:grid;place-items:center;
padding:24px;color:#a5bfce}#placeholder[hidden]{display:none}pre{white-space:pre-wrap;overflow-wrap:anywhere;
background:#182d3b;padding:14px;border-radius:8px;font-size:14px;line-height:1.6}
</style></head><body><main><h1>安全帽检测 · 内网查看</h1>
<p id="state">正在连接开发板…</p><div class="viewer"><img id="preview" alt="当前有效检测画面">
<div id="placeholder">待机或暂无新鲜画面</div></div><pre id="stats"></pre>
<p>在开发板 App 选择摄像头和模型，再点击“开始检测”。回到桌面后检测继续。
此页显示同一检测实例；空检测不能说明全员戴帽，断流或过期时清除旧画面。</p></main>
<script>
const preview=document.querySelector('#preview'), placeholder=document.querySelector('#placeholder');
let previewSerial=0, sourceKey=null, expires=0, expiryTimer=null;
function clearPreview(){previewSerial++;expires=0;clearTimeout(expiryTimer);
 preview.style.visibility='hidden';placeholder.hidden=false;}
function expirePreview(){if(expires&&performance.now()>=expires){clearPreview();
 document.querySelector('#state').textContent='画面已过期，等待新鲜结果';}}
function showPacket(packet,started){
 const remaining=Number(packet.remaining_ttl_ms)-(performance.now()-started);
 if(packet.valid!==true||!packet.jpeg_base64||!Number.isFinite(remaining)||remaining<=0){clearPreview();return;}
 const key=packet.generation+'|'+packet.source_epoch;
 if(key!==sourceKey){clearPreview();sourceKey=key;}
 const ticket=++previewSerial, deadline=performance.now()+remaining, picture=new Image();
 picture.onload=()=>{if(ticket!==previewSerial||key!==sourceKey||performance.now()>=deadline)return;
  preview.src=picture.src;preview.style.visibility='visible';placeholder.hidden=true;
  expires=deadline;clearTimeout(expiryTimer);expiryTimer=setTimeout(expirePreview,Math.max(0,deadline-performance.now()));};
 picture.onerror=()=>{if(ticket===previewSerial)clearPreview();};
 picture.src='data:image/jpeg;base64,'+packet.jpeg_base64;
}
async function poll(){const started=performance.now(),abort=new AbortController();
 const timeout=setTimeout(()=>abort.abort(),1500);
 try{const response=await fetch('/api/preview',{cache:'no-store',signal:abort.signal});const packet=await response.json();
  const status=packet.state||'idle';document.querySelector('#state').textContent=
   packet.running?(packet.valid?'检测中':'检测不可用 / '+(packet.reason||status)):'待机：请在板端开始检测';
  document.querySelector('#state').className=packet.valid?'':'fault';
  document.querySelector('#stats').textContent=JSON.stringify({状态:status,帧号:packet.frame_id,
   检测FPS:packet.fps&&packet.fps.detected,配置:packet.selection},null,2);
  if(response.ok)showPacket(packet,started);else clearPreview();
 }catch(error){clearPreview();document.querySelector('#state').textContent='连接暂不可用，正在重试';}
 finally{clearTimeout(timeout);setTimeout(poll,125);}}
poll();
</script></body></html>'''.encode('utf-8')


class LanPreview:
    def __init__(self, controller, host='0.0.0.0', port=8091, max_clients=4):
        self.controller = controller
        self.host = host
        self.closed = threading.Event()
        self.thread = None
        self.stream_slots = threading.BoundedSemaphore(max_clients)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def reply(self, code, body, content_type, package=None):
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Connection', 'close')
                if package is not None:
                    for name, value in owner.frame_headers(package).items():
                        self.send_header(name, value)
                self.end_headers()
                self.close_connection = True
                self.wfile.write(body)

            def do_POST(self):
                self.reply(405, b'Read-only preview', 'text/plain')

            do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

            def do_GET(self):
                route = self.path.split('?', 1)[0]
                try:
                    if route == '/':
                        self.reply(200, PAGE, 'text/html; charset=utf-8')
                    elif route in ('/status.json', '/api/preview', '/frame.jpg'):
                        status, package = owner.snapshot()
                        if route == '/frame.jpg':
                            self.reply(200 if package else 503,
                                       package['jpeg'] if package else b'Fresh frame unavailable',
                                       'image/jpeg' if package else 'text/plain', package)
                        else:
                            if route == '/api/preview' and package is not None:
                                status['jpeg_base64'] = base64.b64encode(package['jpeg']).decode('ascii')
                            body = json.dumps(status, ensure_ascii=False, allow_nan=False).encode('utf-8')
                            code = 503 if route == '/api/preview' and package is None else 200
                            self.reply(code, body, 'application/json; charset=utf-8', package)
                    elif route == '/stream.mjpg':
                        self.stream()
                    else:
                        self.reply(404, b'Not found', 'text/plain')
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

            def stream(self):
                if not owner.stream_slots.acquire(blocking=False):
                    self.reply(503, b'Preview client limit reached', 'text/plain')
                    return
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.close_connection = True
                    previous = None
                    last_data = time.monotonic()
                    while not owner.closed.is_set():
                        _, package = owner.snapshot()
                        if package is not None:
                            key = (package['generation'], package['source_epoch'], package['frame_id'])
                            if key != previous:
                                jpeg = package['jpeg']
                                headers = {'Content-Type': 'image/jpeg', 'Content-Length': str(len(jpeg)),
                                           **owner.frame_headers(package)}
                                raw = b'--frame\r\n' + ''.join(name + ': ' + value + '\r\n'
                                                                  for name, value in headers.items()).encode('ascii')
                                self.wfile.write(raw + b'\r\n' + jpeg + b'\r\n')
                                self.wfile.flush()
                                previous, last_data = key, time.monotonic()
                        if time.monotonic() - last_data > 2:
                            return
                        owner.closed.wait(.125)
                finally:
                    owner.stream_slots.release()

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]

    def start(self):
        if self.closed.is_set() or self.thread is not None:
            raise RuntimeError('LAN preview cannot be started twice')
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .05}, name='helmet-app-lan', daemon=True)
        self.thread.start()

    def snapshot(self):
        # Both observations remain bound to the controller's existing owner,
        # generation, epoch and original capture time; no second JPEG cache.
        with self.controller.lock:
            raw = self.controller.status()
            package = None if self.closed.is_set() else self.controller.preview_package()
            status = {key: raw[key] for key in ('state', 'reason', 'running', 'selection', 'fps',
                                              'alarm_state', 'counters', 'generation', 'source_epoch')
                      if key in raw}
            status.update(valid=package is not None, frame_id=None, remaining_ttl_ms=0)
            if package is None:
                status['alarm_state'] = 'unknown'
                if raw.get('valid'):
                    status.update(state='degraded', reason='fresh detection frame unavailable')
            else:
                status.update({key: value for key, value in package.items() if key != 'jpeg'})
            return status, package

    @staticmethod
    def frame_headers(package):
        return {'X-Helmet-Frame-Id': str(package['frame_id']),
                'X-Helmet-Generation': package['generation'],
                'X-Helmet-Source-Epoch': str(package['source_epoch']),
                'X-Helmet-Captured-At': '%.9f' % package['captured_at'],
                'X-Helmet-Remaining-Ttl-Ms': '%.3f' % package['remaining_ttl_ms']}

    def close(self):
        self.closed.set()
        if self.thread is not None and self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(2)
        self.server.server_close()
