"""Read-only authenticated LAN viewer for persisted helmet events."""
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit


COOKIE_NAME = 'helmet_alert_access'
INVITE_TTL = 2592000
LAN_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8',
    '172.16.0.0/12',
    '192.168.0.0/16',
))
PAGE = '''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#07131d">
<title>RK3568 · 现场安全事件</title>
<style>
:root{color-scheme:dark;--bg:#07131d;--panel:#0c1e2b;--line:#1f5264;--cyan:#52e7d6;
--red:#ff5c55;--amber:#ffbd59;--text:#e9f8fb;--muted:#91adba}
*{box-sizing:border-box}html,body{margin:0;min-height:100%}
body{background:radial-gradient(circle at 18% 0,#103447 0,transparent 36%),
linear-gradient(135deg,#07131d,#091925 58%,#071017);color:var(--text);
font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.12;
background-image:linear-gradient(rgba(82,231,214,.2) 1px,transparent 1px),
linear-gradient(90deg,rgba(82,231,214,.2) 1px,transparent 1px);
background-size:42px 42px;mask-image:linear-gradient(to bottom,#000,transparent 80%)}
main{position:relative;max-width:1180px;margin:auto;padding:24px 16px 56px}
header{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:24px}
.eyebrow{color:var(--cyan);font:700 12px/1.4 ui-monospace,monospace;letter-spacing:.16em;
text-transform:uppercase}.eyebrow span{color:var(--amber)}
h1{font-size:clamp(30px,6vw,58px);line-height:1;letter-spacing:-.04em;margin:9px 0 8px;
text-wrap:balance}.subtitle{color:var(--muted);margin:0;max-width:60ch;text-wrap:pretty}
.summary{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:10px;margin:22px 0}
.metric{border:1px solid var(--line);background:rgba(12,30,43,.82);padding:14px;border-radius:4px}
.metric b{display:block;color:var(--cyan);font-size:20px}.metric span{color:var(--muted);font-size:13px}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:20px 0}
label{font-weight:700}.input-label{display:grid;gap:6px;color:var(--muted);font-size:13px}
input,button{font:inherit}input[type=date]{background:#0a1a25;color:var(--text);
border:1px solid var(--line);border-radius:4px;padding:10px 12px}
button{border:1px solid var(--cyan);background:#0d2b38;color:var(--text);min-height:44px;
padding:10px 16px;border-radius:4px;font-weight:800;touch-action:manipulation;cursor:pointer}
button:hover{background:#123c4b}button:active{transform:translateY(1px)}
button:focus-visible,input:focus-visible{outline:3px solid var(--amber);outline-offset:2px}
.status-line{margin-left:auto;color:var(--muted);font-size:13px}
#events{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.event-card{min-width:0;border:1px solid #463137;background:linear-gradient(145deg,#25151c,#101d27);
border-radius:8px;overflow:hidden;box-shadow:0 16px 45px rgba(0,0,0,.22)}
.event-card article{padding:0}.event-image{position:relative;background:#03090d;aspect-ratio:9/16;
max-height:520px;overflow:hidden}.event-image img{display:block;width:100%;height:100%;
object-fit:contain}.danger-badge{position:absolute;left:12px;top:12px;background:var(--red);
color:#fff;padding:6px 9px;border-radius:3px;font:800 12px/1 ui-monospace,monospace;
letter-spacing:.08em}.event-body{padding:16px}.event-body h2{margin:0 0 5px;font-size:22px}
.timestamp{font-variant-numeric:tabular-nums;color:var(--cyan)}.event-meta{color:var(--muted);
font-size:13px;overflow-wrap:anywhere}.image-error .event-image:after{content:"照片暂时无法加载";
position:absolute;inset:0;display:grid;place-items:center;color:var(--muted)}
.image-error img{visibility:hidden}.empty{border:1px dashed var(--line);padding:42px 20px;
text-align:center;border-radius:8px}.empty h2{margin:0 0 6px}.empty p{margin:0;color:var(--muted)}
.panel-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:24px}
.panel{background:var(--panel);border:1px solid var(--line);padding:14px;border-radius:4px;
min-width:0;overflow-wrap:anywhere}.panel h2{font-size:13px;color:var(--muted);margin:0 0 5px;
text-transform:uppercase;letter-spacing:.08em}.panel p{margin:0;font-size:15px}
.fault{color:var(--amber)}.ok{color:var(--cyan)}
#loadMore{margin:20px auto 0}.skip{position:absolute;left:-9999px}
@media(max-width:760px){main{padding:18px 12px 40px}header{display:block}.summary{grid-template-columns:1fr 1fr}
#events{grid-template-columns:1fr}.panel-grid{grid-template-columns:1fr}.status-line{width:100%;
margin-left:0}.event-image{max-height:none;aspect-ratio:4/5}.metric b{font-size:18px}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{scroll-behavior:auto!important;
animation-duration:.01ms!important;animation-iteration-count:1!important;
transition-duration:.01ms!important}}
</style>
</head>
<body>
<main id="content">
<header>
  <div>
    <div class="eyebrow">RK3568 / SAFETY GRID <span>// EVENT ARCHIVE</span></div>
    <h1>现场安全事件</h1>
    <p class="subtitle">这里只展示已保存的未戴安全帽证据，仅限与开发板处于同一
    Wi-Fi/局域网的设备查看，不提供实时摄像头、公网入口或控制功能。
    告警表示模型未识别到安全帽，不作为个人身份判断。</p>
  </div>
</header>
<section class="summary" aria-label="事件摘要">
  <div class="metric"><b id="eventCount">--</b><span>当前页事件</span></div>
  <div class="metric"><b id="latestState">检测中</b><span>检测状态</span></div>
  <div class="metric"><b id="accessScope">仅限内网</b><span>访问范围</span></div>
  <div class="metric"><b id="storageState">--</b><span>事件存储</span></div>
</section>
<section class="toolbar" aria-label="事件筛选">
  <label class="input-label">按日期筛选
    <input id="dayFilter" type="date" name="event-day">
  </label>
  <button id="applyFilter" type="button">应用日期</button>
  <span id="connectionState" class="status-line" role="status" aria-live="polite">正在连接…</span>
</section>
<div id="emptyState" class="empty" hidden>
  <h2 id="emptyTitle">尚未发现未戴安全帽事件</h2>
  <p id="emptyHint">当检测确认新的未戴帽轨迹时，证据照片会出现在这里。</p>
</div>
<section id="events" aria-label="安全事件列表"></section>
<button id="loadMore" type="button" hidden>继续加载</button>
<section class="panel-grid" aria-label="系统状态">
  <div class="panel"><h2>邮件通知</h2><p id="mailState">尚未查询</p></div>
  <div class="panel"><h2>证据保留</h2><p>默认 7 天 · 自动清理</p></div>
  <div class="panel"><h2>访问边界</h2><p>仅历史照片 · 不公开实时画面</p></div>
</section>
</main>
<script>
const pageParams=new URLSearchParams(location.search);
let currentBefore=pageParams.get('before')||'';
let currentDay=pageParams.get('day')||'';
let hasEvents=false;
function text(id,value){const node=document.querySelector('#'+id);if(node)node.textContent=value;}
function formatTime(value){
 try{return new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'medium',
  timeZone:'Asia/Shanghai'}).format(new Date(value));}catch(error){return String(value||'未知时间');}
}
function markImageError(image){
 const card=image.parentNode&&image.parentNode.parentNode;
 if(card&&card.classList)card.classList.add('image-error');
 image.alt='未戴安全帽事件照片暂时无法加载';
}
function eventCard(event){
 const card=document.createElement('article');
 card.className='event-card';
 card.setAttribute('data-event-id',String(event.event_id||''));
 const article=document.createElement('article');
 const visual=document.createElement('div');visual.className='event-image';
 const image=document.createElement('img');
 image.width=720;image.height=1280;image.loading='lazy';
 image.alt='未戴安全帽事件照片 '+formatTime(event.captured_wall_time);
 image.addEventListener('error',()=>markImageError(image));
 image.src='/events/'+encodeURIComponent(String(event.event_id||''))+'.jpg';
 const badge=document.createElement('span');badge.className='danger-badge';
 badge.textContent='DANGER // '+Math.max(1,Number(event.count)||1)+' 人';
 visual.appendChild(image);visual.appendChild(badge);
 const body=document.createElement('div');body.className='event-body';
 const title=document.createElement('h2');title.textContent='未戴安全帽';
 const time=document.createElement('div');time.className='timestamp';
 time.textContent=formatTime(event.captured_wall_time);
 const meta=document.createElement('p');meta.className='event-meta';
 meta.textContent='事件 '+String(event.event_id||'未知')+' · 帧 '+String(event.frame_id??'未知');
 body.appendChild(title);body.appendChild(time);body.appendChild(meta);
 article.appendChild(visual);article.appendChild(body);card.appendChild(article);
 return card;
}
function renderEvents(events,append){
 const root=document.querySelector('#events');
 if(!append){
  root.replaceChildren();
  if(!events.length){
   text('emptyTitle','尚未发现未戴安全帽事件');
   text('emptyHint','当检测确认新的未戴帽轨迹时，证据照片会出现在这里。');
  }
 }
 for(const event of events)root.appendChild(eventCard(event));
 hasEvents=append?hasEvents:events.length>0;
 const empty=document.querySelector('#emptyState');
 if(empty)empty.hidden=hasEvents;
 text('eventCount',String(events.length)+' 条');
 const more=document.querySelector('#loadMore');
 if(more)more.hidden=!currentBefore;
}
function renderStatus(status){
 const detection=status.detection||{};
 text('latestState',detection.running?(detection.valid===false?'异常':'检测中'):
  (detection.state==='idle'?'待机':'未运行'));
 const mail=status.mail||{};
 const labels={sent:'已发送',sending:'发送中',retrying:'重试中',failed:'邮件发送失败',
  idle:'邮件链路就绪',disabled:'邮件未启用',not_configured:'邮件未配置',
  stopped:'邮件已停止'};
 text('mailState',labels[mail.state]||'邮件状态未知');
 const mailNode=document.querySelector('#mailState');
 if(mailNode)mailNode.className=mail.state==='failed'?'fault':'ok';
 const viewer=status.viewer||{};
 text('accessScope',viewer.alive?'LAN · ONLINE':'LAN · OFFLINE');
 const store=status.store||{};
 text('storageState',(Number(store.records)||0)+' 条 / '+
  Math.round((Number(store.bytes)||0)/1024)+' KiB');
}
async function refreshStatus(){
 try{
  const response=await fetch('/api/status',{cache:'no-store'});
  if(response.status===401||response.status===403){location.replace('/');return;}
  if(!response.ok)throw new Error('status unavailable');
  renderStatus(await response.json());
 }catch(error){text('latestState','状态暂不可用');}
}
async function loadEvents(append=false){
 const query=new URLSearchParams({limit:'20'});
 if(currentDay)query.set('day',currentDay);
 if(currentBefore)query.set('before',currentBefore);
 try{
  const response=await fetch('/api/events?'+query.toString(),{cache:'no-store'});
  if(response.status===401||response.status===403){location.replace('/');return;}
  if(!response.ok)throw new Error('events unavailable');
  const data=await response.json();
  const events=Array.isArray(data.events)?data.events:[];
  currentBefore=typeof data.next_before==='string'?data.next_before:'';
  renderEvents(events,append);
  text('connectionState','已连接 · '+formatTime(new Date().toISOString()));
 }catch(error){
  text('connectionState','连接暂时不可用，正在重试…');
 }
}
async function applyFilters(){
 const element=document.querySelector('#dayFilter');
 currentDay=element&&element.value?element.value:'';
 currentBefore='';
 hasEvents=false;
 await loadEvents(false);
}
function init(){
 const day=document.querySelector('#dayFilter');if(day)day.value=currentDay;
 const apply=document.querySelector('#applyFilter');
 if(apply)apply.addEventListener('click',applyFilters);
 const more=document.querySelector('#loadMore');
 if(more)more.addEventListener('click',()=>loadEvents(true));
 loadEvents(false);refreshStatus();
}
document.addEventListener('DOMContentLoaded',init);
</script>
</body>
</html>'''.encode('utf-8')


LOGIN_PAGE = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#07131d">
<title>RK3568 · 事件访问</title><style>
:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;
background:radial-gradient(circle at top,#12394b,#07131d 62%);color:#e9f8fb;
font:16px/1.6 system-ui,sans-serif}main{max-width:620px;margin:20px;padding:32px;
border:1px solid #1f5264;background:rgba(10,26,37,.9);border-radius:8px}
h1{font-size:clamp(28px,7vw,46px);line-height:1.05;text-wrap:balance}p{color:#9bb6c2}
code{color:#52e7d6;overflow-wrap:anywhere}.focus:focus-visible{outline:3px solid #ffbd59}
</style></head><body><main><h1>现场安全事件</h1>
<p>请让手机与开发板连接同一 Wi-Fi/局域网，再使用告警邮件中的访问链接进入。
此入口只展示已保存的历史告警照片，不提供实时摄像头、公网入口、检测控制或网络设置。</p>
<p>访问链接包含临时访问密钥，请勿转发给无关人员。若链接已过期，
请从新告警邮件获取最新链接。</p></main></body></html>'''.encode('utf-8')


def _utc_now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise ValueError('access time must be a datetime')
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _private_key_mode(mode):
    return stat.S_ISREG(mode) and stat.S_IMODE(mode) & 0o077 == 0


def _is_lan_client(address):
    try:
        value = ipaddress.ip_address(str(address))
    except ValueError:
        return False
    return (value.version == 4
            and (value.is_loopback
                 or any(value in network for network in LAN_NETWORKS)))


class AlertAccessKey:
    def __init__(self, secret):
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError('access key must contain at least 256 bits')
        self._secret = secret

    @classmethod
    def load_or_create(cls, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        if not path.exists():
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    raw = secrets.token_bytes(32)
                    os.write(descriptor, raw.hex().encode('ascii') + b'\n')
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                path.chmod(0o600)
            except FileExistsError:
                pass
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ValueError('access key must be a regular private file')
        if os.name != 'nt' and not _private_key_mode(details.st_mode):
            raise ValueError('access key permissions must be 0600')
        raw = path.read_bytes()
        if len(raw) > 256:
            raise ValueError('access key file is too large')
        try:
            secret = bytes.fromhex(raw.decode('ascii').strip())
        except (UnicodeError, ValueError) as error:
            raise ValueError('access key file is invalid') from error
        return cls(secret)

    def _signature(self, purpose, expires):
        message = ('v1:%s:%d' % (purpose, expires)).encode('ascii')
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def _token(self, purpose, now=None, ttl=INVITE_TTL):
        observed = _utc_now(now)
        expires = int(observed.timestamp() + ttl)
        return 'v1.%s.%d.%s' % (purpose, expires, self._signature(purpose, expires))

    def _verify(self, token, purpose, now=None):
        if not isinstance(token, str) or len(token) > 256:
            return False
        observed = _utc_now(now)
        try:
            version, actual_purpose, raw_expiry, signature = token.split('.')
            expires = int(raw_expiry)
        except (ValueError, TypeError):
            return False
        if (version != 'v1' or actual_purpose != purpose
                or expires < int(observed.timestamp())):
            return False
        return hmac.compare_digest(signature, self._signature(purpose, expires))

    def make_invite(self, now=None, ttl=INVITE_TTL):
        if type(ttl) is not int or ttl <= 0:
            raise ValueError('invite lifetime must be positive')
        return self._token('invite', now=now, ttl=ttl)

    def verify_invite(self, token, now=None):
        return self._verify(token, 'invite', now=now)

    def cookie_value(self, now=None):
        return self._token('cookie', now=now)

    def verify_cookie(self, value, now=None):
        return self._verify(value, 'cookie', now=now)

    def url_path(self, lan_host, lan_port=8092, scheme='http', now=None):
        if not _is_lan_client(lan_host):
            raise ValueError('LAN event host is invalid')
        if type(lan_port) is not int or not 1 <= lan_port <= 65535:
            raise ValueError('LAN event port is invalid')
        if scheme not in ('http', 'https'):
            raise ValueError('LAN event scheme is invalid')
        query = urlencode({'access': self.make_invite(now=now)})
        return '%s://%s:%d/alerts?%s' % (scheme, lan_host, lan_port, query)


class LanEventServer:
    def __init__(self, store, access_key, status_provider,
                 host='0.0.0.0', port=8092, max_clients=8):
        if type(max_clients) is not int or max_clients <= 0:
            raise ValueError('LAN media client limit must be positive')
        self.store = store
        self.access_key = access_key
        self.status_provider = status_provider
        self.closed = threading.Event()
        self.thread = None
        self.media_slots = threading.BoundedSemaphore(max_clients)
        self._failure_lock = threading.Lock()
        self._failures = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(3)

            def cookie_valid(self):
                raw = self.headers.get('Cookie', '')
                cookie = SimpleCookie()
                try:
                    cookie.load(raw)
                except Exception:
                    return False
                morsel = cookie.get(COOKIE_NAME)
                return (morsel is not None
                        and owner.access_key.verify_cookie(morsel.value))

            def reply(self, code, body, content_type='text/plain; charset=utf-8',
                      headers=None):
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header(
                    'Content-Security-Policy',
                    "default-src 'none'; img-src 'self' data:; "
                    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                    "form-action 'self'")
                self.send_header('Connection', 'close')
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.close_connection = True
                self.wfile.write(body)

            def json(self, code, value):
                self.reply(code, json.dumps(
                    value, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                    'application/json; charset=utf-8')

            def forbidden(self):
                self.json(403, {'error': 'event access required'})

            def record_failure(self):
                client = self.client_address[0]
                now = time.monotonic()
                with owner._failure_lock:
                    values = [value for value in owner._failures.get(client, [])
                              if now - value < 60]
                    values.append(now)
                    owner._failures[client] = values
                    return len(values) > 10

            def lan_status(self):
                try:
                    raw = owner.status_provider() if owner.status_provider else {}
                except BaseException:
                    raw = {}
                if not isinstance(raw, dict):
                    raw = {}
                detection = raw.get('detection')
                mail = raw.get('mail')
                viewer = raw.get('viewer')
                return {
                    'store': owner.store.status(),
                    'detection': ({key: detection.get(key) for key in
                                   ('state', 'running', 'valid')}
                                  if isinstance(detection, dict) else {}),
                    'mail': ({key: mail.get(key) for key in
                              ('configured', 'state', 'last_result', 'pending_batches',
                               'dropped_batches')}
                             if isinstance(mail, dict) else {}),
                    'viewer': ({
                        'alive': owner.thread is not None
                                 and owner.thread.is_alive()
                                 and not owner.closed.is_set(),
                        'port': owner.port,
                        'scope': 'lan',
                    } if isinstance(viewer, dict) else {
                        'alive': False,
                        'port': None,
                        'scope': 'lan',
                    }),
                }

            def do_GET(self):
                try:
                    self.dispatch()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

            def do_POST(self):
                self.reply(405, b'Read-only event archive')

            do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

            def dispatch(self):
                if not _is_lan_client(self.client_address[0]):
                    self.json(403, {'error': 'LAN access required'})
                    return
                url = urlsplit(self.path)
                route = url.path
                query = parse_qs(url.query, max_num_fields=8)
                if route == '/':
                    self.reply(200, LOGIN_PAGE, 'text/html; charset=utf-8')
                    return
                if route == '/healthz':
                    self.reply(200, b'ok')
                    return
                if route == '/alerts':
                    values = query.get('access')
                    if values is None or len(values) != 1:
                        blocked = self.record_failure()
                        self.json(429 if blocked else 403,
                                  {'error': 'valid event invitation required'})
                        return
                    if not owner.access_key.verify_invite(values[0]):
                        blocked = self.record_failure()
                        self.json(429 if blocked else 403,
                                  {'error': 'valid event invitation required'})
                        return
                    cookie = owner.access_key.cookie_value()
                    self.reply(302, b'', 'text/plain', {
                        'Location': '/events',
                        'Set-Cookie': '%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d'
                                      % (COOKIE_NAME, cookie, INVITE_TTL),
                    })
                    return
                if route == '/events':
                    if not self.cookie_valid():
                        self.forbidden()
                    else:
                        self.reply(200, PAGE, 'text/html; charset=utf-8')
                    return
                if route == '/api/events':
                    if not self.cookie_valid():
                        self.forbidden()
                        return
                    limits = query.get('limit', ['20'])
                    befores = query.get('before')
                    days = query.get('day')
                    if (len(limits) != 1 or (befores is not None and len(befores) != 1)
                            or (days is not None and len(days) != 1)):
                        self.json(400, {'error': 'invalid event query'})
                        return
                    try:
                        limit = int(limits[0])
                        events = owner.store.list_events(
                            limit=limit,
                            before=befores[0] if befores else None,
                            day=days[0] if days else None)
                    except (ValueError, TypeError):
                        self.json(400, {'error': 'invalid event query'})
                        return
                    self.json(200, {
                        'events': events,
                        'next_before': (events[-1]['event_id']
                                        if len(events) == limit and events else None),
                        'day': days[0] if days else None,
                    })
                    return
                if route == '/api/status':
                    if not self.cookie_valid():
                        self.forbidden()
                    else:
                        self.json(200, self.lan_status())
                    return
                if route.startswith('/events/') and route.endswith('.jpg'):
                    if not self.cookie_valid():
                        self.forbidden()
                        return
                    event_id = route[len('/events/'):-len('.jpg')]
                    if '/' in event_id or '\\' in event_id:
                        self.json(404, {'error': 'event media not found'})
                        return
                    path = owner.store.media_path(event_id)
                    if path is None:
                        self.json(404, {'error': 'event media not found'})
                        return
                    if not owner.media_slots.acquire(blocking=False):
                        self.reply(503, b'Event media busy')
                        return
                    try:
                        body = path.read_bytes()
                    except OSError:
                        self.json(404, {'error': 'event media not found'})
                        return
                    finally:
                        owner.media_slots.release()
                    self.reply(200, body, 'image/jpeg')
                    return
                self.json(404, {'error': 'not found'})

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]

    def start(self):
        if self.closed.is_set() or self.thread is not None:
            raise RuntimeError('LAN event viewer cannot be started twice')
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={'poll_interval': .05},
            name='helmet-alert-viewer',
            daemon=True,
        )
        self.thread.start()

    def url_path(self, lan_host, lan_port=8092, scheme='http', now=None):
        return self.access_key.url_path(
            lan_host, lan_port=lan_port, scheme=scheme, now=now)

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        if self.thread is not None and self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(2)
        self.server.server_close()
