"""Read-only direct LAN viewer for persisted helmet events."""
import ipaddress
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


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
<meta name="theme-color" content="#050a12">
<title>RK3568 · 安全帽告警网格</title>
<style>
:root{color-scheme:dark;--void:#050a12;--ink:#eafcff;--muted:#8daeba;
--panel:#091521;--panel-2:#0d1e2b;--line:#1c5263;--cyan:#51f6df;
--cyan-dim:#1a8f86;--red:#ff4f57;--red-deep:#3b1018;--amber:#ffc45c;
--green:#77ff9e;--grid:rgba(81,246,223,.10)}
*{box-sizing:border-box}html{background:var(--void);scroll-behavior:smooth}
body{margin:0;min-height:100vh;color:var(--ink);font:16px/1.55
system-ui,-apple-system,"Segoe UI","Noto Sans CJK SC",sans-serif;
background:
radial-gradient(circle at 12% -10%,rgba(81,246,223,.18),transparent 32rem),
radial-gradient(circle at 100% 18%,rgba(255,79,87,.12),transparent 30rem),
linear-gradient(145deg,#050a12,#07121c 54%,#040910);
overflow-x:hidden}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
background-image:linear-gradient(var(--grid) 1px,transparent 1px),
linear-gradient(90deg,var(--grid) 1px,transparent 1px);
background-size:38px 38px;mask-image:linear-gradient(to bottom,#000,transparent 82%)}
body::after{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
background:repeating-linear-gradient(to bottom,transparent 0 3px,
rgba(255,255,255,.018) 3px 4px);mix-blend-mode:screen}
a{color:var(--cyan)}.skip{position:fixed;left:12px;top:10px;z-index:20;
padding:10px 14px;background:var(--amber);color:#111;font-weight:900;
border-radius:3px;transform:translateY(-160%)}
.skip:focus{transform:none}
main{position:relative;max-width:1240px;margin:0 auto;padding:32px 18px 64px}
.hero{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:24px;
align-items:end;margin:4px 0 24px;padding-bottom:22px;
border-bottom:1px solid var(--line)}
.eyebrow{margin:0 0 8px;color:var(--cyan);
font:800 12px/1.4 ui-monospace,"SFMono-Regular",Consolas,monospace;
letter-spacing:.18em;text-transform:uppercase}
.eyebrow span{color:var(--red)}
h1{max-width:15ch;margin:0;font:900 clamp(38px,7vw,76px)/.92
ui-monospace,"SFMono-Regular",Consolas,monospace;letter-spacing:-.055em;
text-wrap:balance;text-shadow:0 0 26px rgba(81,246,223,.18)}
.subtitle{max-width:72ch;margin:14px 0 0;color:var(--muted);text-wrap:pretty}
.node{min-width:190px;padding:14px 16px;border:1px solid var(--line);
background:linear-gradient(135deg,rgba(13,30,43,.92),rgba(5,12,19,.92));
box-shadow:inset 0 0 24px rgba(81,246,223,.035)}
.node small{display:block;color:var(--muted);font:700 11px/1.3
ui-monospace,Consolas,monospace;letter-spacing:.1em}
.node strong{display:block;margin-top:6px;color:var(--cyan);
font:800 15px/1.35 ui-monospace,Consolas,monospace;overflow-wrap:anywhere}
.status-grid{display:grid;grid-template-columns:1.4fr repeat(3,1fr);gap:12px;
margin:22px 0}
.status-card{position:relative;min-width:0;min-height:126px;padding:16px;
border:1px solid var(--line);background:rgba(8,20,31,.9);border-radius:4px;
overflow:hidden}.status-card::before{content:"";position:absolute;left:0;
right:0;top:0;height:3px;background:var(--cyan-dim)}
.status-card>span{display:block;color:var(--muted);font:800 11px/1.3
ui-monospace,Consolas,monospace;letter-spacing:.12em;text-transform:uppercase}
.status-card strong{display:block;margin:10px 0 5px;font:900 clamp(19px,2.4vw,29px)
ui-monospace,Consolas,monospace;line-height:1.08;overflow-wrap:anywhere}
.status-card small{display:block;color:var(--muted);font-size:13px;
overflow-wrap:anywhere}
.status-card.danger{border-color:rgba(255,79,87,.72);
background:linear-gradient(135deg,var(--red-deep),#0b1721 70%);
box-shadow:0 0 32px rgba(255,79,87,.14)}
.status-card.danger::before{background:var(--red)}
.status-card.danger strong{color:var(--red)}
.status-card.watch{border-color:rgba(255,196,92,.62)}
.status-card.watch::before,.status-card.watch strong{background:var(--amber);
color:var(--amber)}.status-card.watch::before{background:var(--amber)}
.status-card.clear{border-color:rgba(119,255,158,.55)}
.status-card.clear::before{background:var(--green)}
.status-card.clear strong{color:var(--green)}
.status-card.unknown,.status-card.standby{opacity:.92}
.status-card.unknown strong{color:var(--amber)}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin:24px 0 18px;
padding:14px;border:1px solid var(--line);background:rgba(9,21,33,.82)}
.input-label{display:grid;gap:5px;color:var(--muted);font-size:13px;
font-weight:800}input,button{font:inherit}
input[type=date]{min-height:44px;padding:8px 11px;border:1px solid var(--line);
border-radius:3px;background:#07131d;color:var(--ink)}
button{min-height:44px;padding:9px 16px;border:1px solid var(--cyan);
border-radius:3px;background:#0b2a35;color:var(--ink);font-weight:900;
touch-action:manipulation;cursor:pointer}
button:hover{background:#103b49}button:active{transform:translateY(1px)}
button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid
var(--amber);outline-offset:3px}
.status-line{margin-left:auto;align-self:center;color:var(--muted);
font:700 13px/1.4 ui-monospace,Consolas,monospace;overflow-wrap:anywhere}
.section-head{display:flex;justify-content:space-between;gap:16px;align-items:end;
margin:28px 0 14px}.section-head h2{margin:0;
font:900 clamp(23px,3.6vw,38px)/1 ui-monospace,Consolas,monospace;
letter-spacing:-.035em}.section-head p{margin:0;color:var(--muted);font-size:13px;
text-align:right;max-width:48ch}
#events{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.event-card{min-width:0;border:1px solid #56303a;border-radius:6px;overflow:hidden;
background:linear-gradient(145deg,#211018,#0a1822 48%,#08131d);
box-shadow:0 22px 54px rgba(0,0,0,.3);break-inside:avoid}
.event-image{position:relative;overflow:hidden;background:#02070b;aspect-ratio:9/16;
max-height:560px}.event-image::after{content:"";position:absolute;inset:0;
pointer-events:none;background:linear-gradient(to top,rgba(2,7,11,.72),
transparent 24%),linear-gradient(120deg,transparent 78%,rgba(255,79,87,.14))}
.event-image img{display:block;width:100%;height:100%;object-fit:contain}
.danger-badge{position:absolute;z-index:1;left:12px;top:12px;padding:7px 9px;
background:var(--red);color:#fff;border-radius:2px;
font:900 11px/1 ui-monospace,Consolas,monospace;letter-spacing:.08em;
box-shadow:0 0 20px rgba(255,79,87,.42)}
.event-body{padding:16px}.event-body h2{margin:0 0 8px;
font:900 22px/1.15 ui-monospace,Consolas,monospace}
.timestamp{color:var(--cyan);font-variant-numeric:tabular-nums;font-weight:800}
.event-meta{margin:8px 0 0;color:var(--muted);font:600 12px/1.5
ui-monospace,Consolas,monospace;overflow-wrap:anywhere}
.event-id{word-break:break-all}
.image-error .event-image::before{content:"证据照片暂时无法加载";position:absolute;
inset:0;z-index:2;display:grid;place-items:center;color:var(--muted);
background:#071018}.image-error img{visibility:hidden}
.empty{padding:46px 22px;border:1px dashed var(--line);text-align:center;
background:rgba(8,20,31,.68)}.empty h2{margin:0 0 7px;
font:900 23px/1.2 ui-monospace,Consolas,monospace}.empty p{margin:0;
color:var(--muted)}
#loadMore{display:block;margin:22px auto 0}
.panel-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;
margin-top:28px}.panel{min-width:0;padding:16px;border:1px solid var(--line);
background:rgba(9,21,33,.82);overflow-wrap:anywhere}
.panel h2{margin:0 0 7px;color:var(--cyan);font:900 12px/1.3
ui-monospace,Consolas,monospace;letter-spacing:.1em;text-transform:uppercase}
.panel p{margin:0;color:var(--muted);font-size:14px}
footer{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);
color:var(--muted);font-size:12px}
@media(max-width:880px){.status-grid{grid-template-columns:1fr 1fr}
.hero{grid-template-columns:1fr}.node{max-width:100%}}
@media(max-width:680px){main{padding:22px 12px 46px}.status-grid{grid-template-columns:1fr}
#events{grid-template-columns:1fr}.event-image{max-height:none;aspect-ratio:4/5}
.panel-grid{grid-template-columns:1fr}.toolbar{align-items:stretch}
.input-label{flex:1 1 100%}input[type=date]{width:100%}
.status-line{width:100%;margin-left:0}.section-head{display:block}
.section-head p{text-align:left;margin-top:8px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}*,*::before,
*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;
transition-duration:.01ms!important}}
</style>
</head>
<body>
<a class="skip" href="#content">跳到主要内容</a>
<main id="content">
  <header class="hero">
    <div>
      <p class="eyebrow">RK3568 / OFFLINE EDGE <span>// SAFETY GRID</span></p>
      <h1>HELMET SAFETY GRID</h1>
      <p class="subtitle">离线检测节点正在独立记录未戴安全帽事件。本页只展示当前告警状态与已落盘的证据记录，不提供实时视频或设备控制。</p>
    </div>
    <div class="node" aria-label="局域网访问节点">
      <small>PRIVATE NETWORK NODE</small>
      <strong>8092 / READ ONLY</strong>
      <small>仅允许私有局域网设备读取</small>
    </div>
  </header>

  <section class="status-grid" aria-label="当前告警状态" aria-live="polite">
    <article id="currentCard" class="status-card unknown">
      <span>当前告警 / LIVE STATE</span>
      <strong id="currentAlert">正在同步</strong>
      <small id="currentDetail">等待检测节点状态。</small>
    </article>
    <article class="status-card">
      <span>最近一次报警</span>
      <strong id="latestEvent">尚无记录</strong>
      <small>以已落盘事件为准</small>
    </article>
    <article class="status-card">
      <span>证据记录</span>
      <strong id="eventCount">--</strong>
      <small id="storageState">正在读取存储</small>
    </article>
    <article class="status-card">
      <span>访问范围</span>
      <strong id="accessScope">LAN / SYNC</strong>
      <small>历史证据只读展示</small>
    </article>
  </section>

  <section class="toolbar" aria-label="报警记录筛选">
    <label class="input-label" for="dayFilter">按日期追溯
      <input id="dayFilter" type="date" name="event-day">
    </label>
    <button id="applyFilter" type="button">应用日期</button>
    <span id="connectionState" class="status-line" role="status"
      aria-live="polite">正在连接事件库</span>
  </section>

  <section aria-labelledby="archiveTitle">
    <div class="section-head">
      <h2 id="archiveTitle">可追溯报警记录</h2>
      <p>每条记录绑定报警照片、发生时间、事件编号与检测帧号。</p>
    </div>
    <div id="emptyState" class="empty" hidden>
      <h2 id="emptyTitle">尚未记录未戴安全帽事件</h2>
      <p id="emptyHint">只有在检测状态有效并确认新的未戴安全帽轨迹后，证据照片才会保存在这里。</p>
    </div>
    <section id="events" aria-label="可追溯报警记录" aria-live="polite"
      aria-busy="true"></section>
    <button id="loadMore" type="button" hidden>继续加载较早记录</button>
  </section>

  <section class="panel-grid" aria-label="系统说明">
    <div class="panel">
      <h2>Evidence retention</h2>
      <p>照片与元数据默认保留 7 天，并受 256 MiB、5000 条记录和 128 MiB 磁盘余量上限约束。</p>
    </div>
    <div class="panel">
      <h2>State truth</h2>
      <p>当前告警来自最新有效检测帧；历史记录只证明过去曾发生事件。空记录不等于现场安全。</p>
    </div>
    <div class="panel">
      <h2>Read-only boundary</h2>
      <p>此页不能启动检测、查看实时视频、修改网络或退出应用，关闭发送、删除和修改请求。</p>
    </div>
  </section>

  <footer>Safety evidence is tied to frame identity and capture time. Input interruption or stale frames produce an unknown state.</footer>
</main>
<script>
const pageParams=new URLSearchParams(location.search);
let currentBefore=pageParams.get('before')||'';
let currentDay=pageParams.get('day')||'';
let hasEvents=false;
let latestEventAt='';
let statusTimer=null;
function node(id){return document.querySelector('#'+id);}
function text(id,value){const target=node(id);if(target)target.textContent=value;}
function setConnection(value){
 text('connectionState',value);
 const target=node('connectionState');
 if(target){target.setAttribute('role','status');
  target.setAttribute('aria-live','polite');}
}
function formatTime(value){
 try{return new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',
  timeStyle:'medium',timeZone:'Asia/Shanghai'}).format(new Date(value));}
 catch(error){return String(value||'未知时间');}
}
function updateAddress(){
 const query=new URLSearchParams();
 if(currentDay)query.set('day',currentDay);
 if(currentBefore)query.set('before',currentBefore);
 try{if(typeof history!=='undefined'&&history.replaceState){
  history.replaceState(null,'',location.pathname+(query.toString()?'?'+query:''));
 }}catch(error){}
}
function markImageError(image){
 const card=image.parentNode&&image.parentNode.parentNode;
 if(card&&card.classList)card.classList.add('image-error');
 image.alt='未戴安全帽报警证据照片暂时无法加载';
}
function eventCard(event){
 const card=document.createElement('article');
 card.className='event-card';
 card.setAttribute('data-event-id',String(event.event_id||''));
 const visual=document.createElement('div');visual.className='event-image';
 const image=document.createElement('img');
 image.width=720;image.height=1280;image.loading='lazy';
 image.alt='未戴安全帽报警事件照片，'+formatTime(event.captured_wall_time);
 image.addEventListener('error',()=>markImageError(image));
 image.src='/events/'+encodeURIComponent(String(event.event_id||''))+'.jpg';
 const badge=document.createElement('span');badge.className='danger-badge';
 badge.textContent='DANGER // NO HAT';
 visual.appendChild(image);visual.appendChild(badge);
 const body=document.createElement('div');body.className='event-body';
 const title=document.createElement('h2');
 title.textContent='未戴安全帽 · '+Math.max(1,Number(event.count)||1)+' 个可见头部';
 const time=document.createElement('div');time.className='timestamp';
 time.textContent=formatTime(event.captured_wall_time);
 const meta=document.createElement('p');meta.className='event-meta event-id';
 meta.textContent=String(event.event_id||'未知事件')
  +' · 帧 '+String(event.frame_id??'未知');
 body.appendChild(title);body.appendChild(time);body.appendChild(meta);
 card.appendChild(visual);card.appendChild(body);
 return card;
}
function renderEvents(events,append){
 const root=node('events');
 if(!append){
  root.replaceChildren();
  if(!events.length){
   text('emptyTitle','尚未记录未戴安全帽事件');
   text('emptyHint','检测节点尚未确认新的未戴安全帽轨迹，或事件已按保留策略清理。');
  }else{
   latestEventAt=events[0].captured_wall_time||'';
   text('latestEvent',latestEventAt?formatTime(latestEventAt):'尚无记录');
  }
 }
 for(const event of events)root.appendChild(eventCard(event));
 hasEvents=append?hasEvents:events.length>0;
 const empty=node('emptyState');if(empty)empty.hidden=hasEvents;
 const more=node('loadMore');if(more)more.hidden=!currentBefore;
 root.setAttribute('aria-busy','false');
}
function currentState(detection){
 if(!detection.running)return {label:'STANDBY',detail:'检测尚未启动。',
  style:'standby'};
 if(!detection.valid)return {label:'UNKNOWN // NO FRESH FRAME',
  detail:'输入无有效帧，不能判断当前安全状态。',style:'unknown'};
 const state=String(detection.alarm_state||'unknown');
 if(state==='active')return {label:'DANGER // NO HAT',
  detail:'最新有效报警窗口检测到未戴安全帽头部。',style:'danger'};
 if(state==='pending'||state==='cooldown')return {
  label:'WATCH // ALERT WINDOW',detail:'危险目标正在进入报警判定窗口。',
  style:'watch'};
 if(state==='clear')return {label:'CLEAR // HAT DETECTED',
  detail:'最新有效报警窗口未报告持续未戴帽危险。',style:'clear'};
 return {label:'UNKNOWN // WAITING',detail:'检测状态暂不可确认。',
  style:'unknown'};
}
function renderStatus(status){
 const detection=status.detection||{};
 const state=currentState(detection);
 text('currentAlert',state.label);
 const frame=detection.frame_id??'--';
 const generation=typeof detection.generation==='string'
  ?detection.generation.slice(0,8):'--';
 text('currentDetail',state.detail+' 帧 '+frame+' · 运行 '+generation);
 const card=node('currentCard');
 if(card)card.className='status-card '+state.style;
 if(status.last_event_at)latestEventAt=status.last_event_at;
 text('latestEvent',latestEventAt?formatTime(latestEventAt):'尚无记录');
 const store=status.store||{};
 const records=Math.max(0,Number(store.records)||0);
 text('eventCount',records+' 条');
 const kib=Math.round((Number(store.bytes)||0)/1024);
 text('storageState',kib+' KiB · 保留 '+(Number(store.retention_days)||7)+' 天');
 const viewer=status.viewer||{};
 text('accessScope',viewer.alive?'LAN / ONLINE':'LAN / OFFLINE');
}
async function refreshStatus(){
 try{
  const response=await fetch('/api/status',{cache:'no-store'});
  if(!response.ok)throw new Error('status unavailable');
  renderStatus(await response.json());
 }catch(error){
  text('currentAlert','UNKNOWN // STATUS OFFLINE');
  text('currentDetail','状态接口暂不可用，不能推断当前安全状态。');
  text('accessScope','LAN / OFFLINE');
  const card=node('currentCard');
  if(card)card.className='status-card unknown';
 }finally{
  statusTimer=setTimeout(refreshStatus,5000);
 }
}
async function loadEvents(append=false){
 const root=node('events');if(root)root.setAttribute('aria-busy','true');
 const query=new URLSearchParams({limit:'20'});
 if(currentDay)query.set('day',currentDay);
 if(currentBefore)query.set('before',currentBefore);
 try{
  const response=await fetch('/api/events?'+query.toString(),{cache:'no-store'});
  if(!response.ok)throw new Error('events unavailable');
  const data=await response.json();
  const events=Array.isArray(data.events)?data.events:[];
  currentBefore=typeof data.next_before==='string'?data.next_before:'';
  renderEvents(events,append);updateAddress();
  setConnection('已连接事件库 · '+formatTime(new Date().toISOString()));
 }catch(error){
  if(root)root.setAttribute('aria-busy','false');
  setConnection('事件库暂不可用，正在重试');
 }
}
async function applyFilters(){
 const element=node('dayFilter');
 currentDay=element&&element.value?element.value:'';
 currentBefore='';hasEvents=false;updateAddress();
 await loadEvents(false);
}
function init(){
 const day=node('dayFilter');if(day)day.value=currentDay;
 const apply=node('applyFilter');
 if(apply)apply.addEventListener('click',applyFilters);
 const more=node('loadMore');
 if(more)more.addEventListener('click',()=>loadEvents(true));
 loadEvents(false);refreshStatus();
}
document.addEventListener('DOMContentLoaded',init);
</script>
</body>
</html>'''.encode('utf-8')


def _is_lan_client(address):
    try:
        value = ipaddress.ip_address(str(address))
    except ValueError:
        return False
    return (value.version == 4
            and (value.is_loopback
                 or any(value in network for network in LAN_NETWORKS)))


class LanEventServer:
    def __init__(self, store, status_provider,
                 host='0.0.0.0', port=8092, max_clients=8):
        if not callable(status_provider):
            raise ValueError('event status provider is required')
        if type(max_clients) is not int or max_clients <= 0:
            raise ValueError('LAN media client limit must be positive')
        self.store = store
        self.status_provider = status_provider
        self.closed = threading.Event()
        self.thread = None
        self.media_slots = threading.BoundedSemaphore(max_clients)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(3)

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
                    "form-action 'none'")
                self.send_header('Connection', 'close')
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.close_connection = True
                self.wfile.write(body)

            def json(self, code, value):
                self.reply(
                    code,
                    json.dumps(
                        value, ensure_ascii=False, allow_nan=False
                    ).encode('utf-8'),
                    'application/json; charset=utf-8',
                )

            def lan_status(self):
                try:
                    raw = owner.status_provider() if owner.status_provider else {}
                except BaseException:
                    raw = {}
                if not isinstance(raw, dict):
                    raw = {}
                detection = raw.get('detection')
                if not isinstance(detection, dict):
                    detection = {}
                try:
                    store = owner.store.status()
                except BaseException:
                    store = {'state': 'unavailable'}
                events_created = raw.get('events_created')
                if type(events_created) is not int or events_created < 0:
                    events_created = None
                last_event_at = raw.get('last_event_at')
                if not isinstance(last_event_at, str):
                    last_event_at = None
                return {
                    'store': store,
                    'detection': {
                        key: detection.get(key) for key in
                        ('state', 'running', 'valid', 'generation',
                         'alarm_state', 'frame_id', 'source_epoch')
                    },
                    'viewer': {
                        'alive': owner.thread is not None
                                 and owner.thread.is_alive()
                                 and not owner.closed.is_set(),
                        'port': owner.port,
                        'scope': 'lan',
                    },
                    'events_created': events_created,
                    'last_event_at': last_event_at,
                }

            def do_GET(self):
                try:
                    self.dispatch()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

            def do_POST(self):
                self.reply(405, b'Read-only event archive', headers={
                    'Allow': 'GET'})

            do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

            def dispatch(self):
                if not _is_lan_client(self.client_address[0]):
                    self.json(403, {'error': 'private LAN access required'})
                    return
                url = urlsplit(self.path)
                route = url.path
                query = parse_qs(url.query, max_num_fields=8)
                if route in ('/', '/events'):
                    self.reply(200, PAGE, 'text/html; charset=utf-8')
                    return
                if route == '/alerts':
                    self.reply(302, b'', headers={'Location': '/'})
                    return
                if route == '/healthz':
                    self.reply(200, b'ok')
                    return
                if route == '/api/events':
                    limits = query.get('limit', ['20'])
                    befores = query.get('before')
                    days = query.get('day')
                    if (len(limits) != 1
                            or (befores is not None and len(befores) != 1)
                            or (days is not None and len(days) != 1)):
                        self.json(400, {'error': 'invalid event query'})
                        return
                    try:
                        limit = int(limits[0])
                        events = owner.store.list_events(
                            limit=limit,
                            before=befores[0] if befores else None,
                            day=days[0] if days else None,
                        )
                    except (ValueError, TypeError):
                        self.json(400, {'error': 'invalid event query'})
                        return
                    self.json(200, {
                        'events': events,
                        'next_before': (
                            events[-1]['event_id']
                            if len(events) == limit and events else None
                        ),
                        'day': days[0] if days else None,
                    })
                    return
                if route == '/api/status':
                    self.json(200, self.lan_status())
                    return
                if route.startswith('/events/') and route.endswith('.jpg'):
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

    def url_path(self, lan_host, lan_port=8092, scheme='http'):
        if not _is_lan_client(lan_host):
            raise ValueError('LAN event host is invalid')
        if type(lan_port) is not int or not 1 <= lan_port <= 65535:
            raise ValueError('LAN event port is invalid')
        if scheme not in ('http', 'https'):
            raise ValueError('LAN event scheme is invalid')
        return '%s://%s:%d/' % (scheme, lan_host, lan_port)

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        if self.thread is not None and self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(2)
        self.server.server_close()
