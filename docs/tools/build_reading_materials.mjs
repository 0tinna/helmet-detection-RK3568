import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Dependency-free exports. Markdown is the maintained source of both views.
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const out = path.join(repo, 'docs/deliverables');
await fs.mkdir(out, { recursive: true });
const esc = s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const anchor = s => s.toLowerCase().replace(/[^\p{L}\p{N}\s_-]/gu, '').replace(/\s/g, '-');
function link(href, source) {
    if (/^(?:https?:|mailto:|data:|#)/i.test(href)) return href;
    const [file, fragment] = href.split('#');
    const target = path.resolve(path.dirname(source), file);
    return path.relative(out, target).replaceAll('\\', '/') + (fragment ? '#' + fragment : '');
}
function inline(text, source) {
    const tokens = [];
    const token = html => `\u0001${tokens.push(html) - 1}\u0001`;
    let s = text.replace(/<br\s*\/?>/gi, () => token('<br>'));
    s = s.replace(/`([^`]+)`/g, (_, x) => token(`<code>${esc(x)}</code>`));
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, href) => token(`<a href="${esc(link(href, source))}">${esc(label)}</a>`));
    s = esc(s).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    return s.replace(/\u0001(\d+)\u0001/g, (_, n) => tokens[+n]);
}
function render(md, source) {
    const lines = md.replaceAll('\r', '').split('\n');
    let html = '', para = [], list = [], code = null, lang = '';
    const flush = () => {
        if (para.length) { html += `<p>${inline(para.join(' '), source)}</p>\n`; para = []; }
        if (list.length) { html += `<ul>${list.map(s => `<li>${inline(s, source)}</li>`).join('')}</ul>\n`; list = []; }
    };
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (line.startsWith('```')) {
            flush();
            if (code === null) { code = []; lang = line.slice(3).trim(); }
            else {
                html += `<pre${lang === 'mermaid' ? ' class="diagram-source"' : ''}><code>${esc(code.join('\n'))}</code></pre>\n`;
                code = null;
            }
            continue;
        }
        if (code !== null) { code.push(line); continue; }
        if (!line.trim()) { flush(); continue; }
        const heading = line.match(/^(#{1,6})\s+(.+)$/);
        if (heading) {
            flush(); const level = heading[1].length;
            html += `<h${level} id="${esc(anchor(heading[2]))}">${inline(heading[2], source)}</h${level}>\n`; continue;
        }
        if (line.startsWith('|') && /^\|[\s:|\-]+\|\s*$/.test(lines[i + 1] || '')) {
            flush();
            const cells = row => row.trim().slice(1, -1).split('|').map(x => x.trim());
            const header = cells(line); i += 2;
            const body = [];
            while (i < lines.length && lines[i].startsWith('|')) { body.push(cells(lines[i])); i++; }
            i--;
            html += `<div class="table-scroll"><table><thead><tr>${header.map(x => `<th>${inline(x, source)}</th>`).join('')}</tr></thead><tbody>${body.map(row => `<tr>${row.map(x => `<td>${inline(x, source)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>\n`; continue;
        }
        const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)$/);
        if (item) { if (para.length) flush(); list.push(item[1]); continue; }
        if (list.length) flush();
        para.push(line.replace(/^>\s?/, ''));
    }
    flush();
    if (code !== null) throw new Error('Unclosed Markdown fence');
    return html;
}
const manualFile = path.join(repo, 'docs/项目蓝图与完整手册.md');
const guideFile = path.join(repo, 'docs/阅读导航.md');
const talkFile = path.join(repo, 'docs/proposal/开题PPT逐页内容与演讲稿.md');
const [manual, guide, talk, preview] = await Promise.all([
    fs.readFile(manualFile, 'utf8'), fs.readFile(guideFile, 'utf8'), fs.readFile(talkFile, 'utf8'),
    fs.readFile(path.join(repo, 'docs/experiments/2026-11-14/web-board-preview.jpg')),
]);
const base = `*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:#172333;background:#f7f8fa;font-family:"Microsoft YaHei","Noto Sans CJK SC",Arial,sans-serif}a{color:#145080;text-underline-offset:3px}button,input{font:inherit}button{cursor:pointer}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{border:1px solid #d9dfe6;padding:11px 13px;text-align:left;vertical-align:middle}th{background:#173951;color:white}tbody tr:nth-child(even){background:#f1f5f7}.table-scroll{overflow-x:auto;margin:20px 0}p,li{line-height:1.85}pre{white-space:pre-wrap;word-break:break-word;background:#f1f4f6;padding:18px;line-height:1.6}code{font-family:Consolas,"Microsoft YaHei",monospace;font-size:.9em}h1,h2,h3,h4{line-height:1.4;color:#101b29}h2{margin-top:42px;scroll-margin-top:25px}h3{margin-top:28px}li+li{margin-top:8px}`;
const manualNav = [...manual.matchAll(/^##\s+(.+)$/gm)].map(m => `<a href="#${esc(anchor(m[1]))}">${esc(m[1])}</a>`).join('');
await fs.writeFile(path.join(out, '项目手册.html'), `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RK3568项目蓝图与完整手册</title><style>${base}
aside{position:fixed;top:0;left:0;bottom:0;width:270px;background:#edf1f4;padding:28px 20px;overflow:auto}aside>a{display:block;padding:7px 0;font-size:14px}aside h2{margin:0 0 15px;font-size:20px}main{max-width:1170px;margin-left:270px;padding:50px 70px 90px;background:white}h1{font-size:32px}main p,main li{font-size:16px;overflow-wrap:anywhere}header{margin-bottom:28px}.actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.actions a,.actions button{padding:8px 12px;border:1px solid #bdc9d1;background:white;color:#173951;text-decoration:none}.diagram{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;margin:25px 0}.diagram div{border-top:3px solid #b58229;padding:16px 6px;line-height:1.8}.diagram span{display:block;color:#5a6875;font-size:14px}.diagram-source{font-size:13px}.source-note{color:#607080;font-size:13px!important}details{margin:20px 0}summary{cursor:pointer;font-size:20px;font-weight:bold}@media(max-width:980px){aside{position:static;width:auto;max-height:270px}main{margin:0;padding:25px}.diagram{grid-template-columns:1fr 1fr}}@media print{aside,.actions{display:none}body{background:white}main{margin:0;padding:0;max-width:none}h2,h3{break-after:avoid}tr{break-inside:avoid}pre{font-size:10px}a{color:inherit;text-decoration:none}@page{size:A4;margin:18mm}}
</style></head><body><aside><h2>项目阅读目录</h2><a href="#reader-guide">第一次阅读路线</a><a href="#system-overview">系统关系图</a>${manualNav}</aside><main><header><div class="actions"><button onclick="window.print()">打印或另存为PDF</button><a href="开题演示.html">开题演示</a><a href="../项目蓝图与完整手册.md">Markdown正文</a></div><p class="source-note">版本以当前Markdown正文和对应实验清单为准。页面不从互联网加载字体、脚本或图片。</p></header><section id="reader-guide"><h1>从这里开始了解项目</h1>${render(guide, guideFile)}</section><section id="system-overview"><h2>系统关系图</h2><div class="diagram"><div><strong>板屏入口</strong><span>Qt App、本机令牌服务<br>开始/停止、用户手动WiFi</span></div><div><strong>原帧与检测</strong><span>ID、时间、源代次<br>最新帧邮箱、NPU、共享解码</span></div><div><strong>状态与提示</strong><span>原帧绑定、TTL、故障<br>持续报警、冷却、解除</span></div><div><strong>输出与接入</strong><span>Qt板屏、手机内网8091<br>共用有效JPEG、截图与CLI日志</span></div></div></section>${render(manual, manualFile)}</main></body></html>`, 'utf8');

const slideSections = talk.split(/\n## 第\d+页 /).slice(1, 13);
if (slideSections.length !== 12) throw new Error('Expected 12 authored slides');
const slides = slideSections.map((section, i) => {
    const title = section.slice(0, section.indexOf('\n')).trim();
    const body = section.split('**页面正文**')[1]?.split(/\*\*(?:讲稿|建议视觉|可编辑架构图内容)/)[0].trim();
    const notes = section.split(/\*\*讲稿[^*]*\*\*/)[1]?.split('**依据**')[0].trim();
    const sources = section.split('**依据**')[1]?.split('\n\n')[0].trim() || '';
    if (!body || !notes) throw new Error(`Missing body/notes on slide ${i + 1}`);
    const image = i === 1 ? `<figure><img src="data:image/jpeg;base64,${preview.toString('base64')}" alt="真实NPU录像回放检测预览"><figcaption>前期实测预览　真实NPU录像回放</figcaption></figure>` : '';
    const diagram = i === 4 ? `<div class="pipeline" aria-label="系统流水线"><div><b>板屏App</b><span>本机服务 开始/停止<br>用户手动WiFi</span></div><div><b>共用检测</b><span>原帧 ID 时间 代次<br>最新邮箱 NPU 解码</span></div><div><b>有效性</b><span>原帧绑定<br>TTL 故障状态</span></div><div><b>输出</b><span>App预览 报警截图<br>CLI录像 帧/资源日志</span></div></div>` : '';
    return `<section class="slide${i === 0 ? ' cover' : ''}${i === 1 ? ' with-image' : ''}" id="slide-${i + 1}" aria-label="第${i + 1}页 ${esc(title)}"><div class="slide-top">离线安全帽检测与状态闭环　开题陈述</div><h1>${esc(title)}</h1><div class="slide-content">${render(body, talkFile)}${image}${diagram}</div><footer><span>前期证据与后续验收分别陈述</span><span>${String(i + 1).padStart(2, '0')} / 12</span></footer><aside class="notes"><h2>演讲备注</h2>${render(notes, talkFile)}<p class="sources">依据 ${inline(sources, talkFile)}</p></aside></section>`;
});
await fs.writeFile(path.join(out, '开题演示.html'), `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RK3568安全帽检测开题演示</title><style>${base}
body{background:#1b2430;overflow:hidden}.stage{height:100vh;display:flex;align-items:center;justify-content:center;padding:35px 30px 68px}.deck{position:relative;width:1280px;height:720px;transform-origin:center}.slide{display:none;position:absolute;inset:0;background:#fcfcfa;padding:43px 60px 50px;overflow:hidden}.slide.active{display:block}.slide-top{color:#546675;letter-spacing:1px;font-size:17px}.slide h1{font-size:40px;margin:18px 0 27px;font-weight:700;color:#15334a}.slide-content{font-size:24px;max-height:490px}.slide-content p{margin:17px 0;line-height:1.65}.slide-content li{line-height:1.7;margin:15px 0}.slide-content ul{padding-left:28px;margin:0}.slide-content table{font-size:22px}.slide-content th,.slide-content td{padding:12px 14px}.slide-content .table-scroll{margin:18px 0}.slide footer{position:absolute;left:60px;right:60px;bottom:27px;display:flex;justify-content:space-between;font-size:15px;color:#637383}.cover h1{font-size:42px;margin-top:70px}.cover .slide-content p:first-child{font-size:44px;font-weight:700;line-height:1.5;max-width:1100px}.cover .slide-content p:last-child{font-size:24px;color:#687986;margin-top:50px}.with-image .slide-content>ul{width:54%;font-size:23px}.with-image figure{position:absolute;right:60px;top:175px;width:450px;margin:0}.with-image img{width:100%;height:auto;object-fit:contain}.with-image figcaption{font-size:17px;color:#61717d;margin-top:14px}.pipeline{display:grid;grid-template-columns:repeat(4,1fr);gap:24px;margin-top:40px}.pipeline div{border-top:4px solid #b88735;padding-top:20px}.pipeline b{font-size:28px}.pipeline span{display:block;font-size:21px;line-height:1.75;margin-top:16px}.controls{position:fixed;bottom:12px;left:0;right:0;display:flex;justify-content:center;align-items:center;gap:9px;color:white;font-size:14px;z-index:30}.controls button,.controls a{border:1px solid #718090;background:#28384a;color:white;padding:8px 13px;text-decoration:none;border-radius:3px}.counter{min-width:65px;text-align:center}.notes{display:none;position:fixed;inset:100px 140px 85px;background:#fffdf5;color:#192c3a;padding:28px 35px;overflow:auto;z-index:20;box-shadow:0 8px 50px #0009}.notes h2{margin:0 0 18px}.notes p{font-size:23px;line-height:1.8}.notes .sources{font-size:15px}.show-notes .slide.active .notes{display:block}.controls button:focus-visible,.controls a:focus-visible{outline:3px solid #edbf62}@media print{body{overflow:visible;background:white}.stage{display:block;height:auto;padding:0}.deck{transform:none!important;width:100%;height:auto}.slide{display:block!important;position:relative;width:100%;height:190mm;padding:12mm 15mm;break-after:page}.controls,.notes{display:none!important}.slide footer{bottom:5mm}.with-image figure{right:15mm;width:110mm;top:46mm}.slide-content{font-size:20px}.slide-content table{font-size:18px}.cover .slide-content p:first-child{font-size:38px}@page{size:A4 landscape;margin:8mm}}
</style></head><body><main class="stage"><div class="deck">${slides.join('')}</div></main><nav class="controls" aria-label="演示控制"><button id="prev">上一页</button><span class="counter" id="counter">1 / 12</span><button id="next">下一页</button><button id="notes">备注 N</button><button id="full">全屏</button><button id="print">打印</button><a href="项目手册.html">手册</a></nav><script>
const slides=[...document.querySelectorAll('.slide')],deck=document.querySelector('.deck');let page=0;
function show(n){page=Math.max(0,Math.min(slides.length-1,n));slides.forEach((s,i)=>s.classList.toggle('active',i===page));document.querySelector('#counter').textContent=(page+1)+' / '+slides.length;document.querySelector('#prev').disabled=page===0;document.querySelector('#next').disabled=page===slides.length-1;history.replaceState(null,'','#slide-'+(page+1))}
function fit(){const scale=Math.min((innerWidth-60)/1280,(innerHeight-100)/720);deck.style.transform='scale('+Math.max(.1,scale)+')'}
document.querySelector('#prev').onclick=()=>show(page-1);document.querySelector('#next').onclick=()=>show(page+1);document.querySelector('#notes').onclick=()=>document.body.classList.toggle('show-notes');document.querySelector('#full').onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen()}catch(e){console.warn(e.message)}};document.querySelector('#print').onclick=()=>window.print();
addEventListener('resize',fit);addEventListener('keydown',e=>{if(e.target.matches('input,textarea'))return;if(['ArrowRight','PageDown',' '].includes(e.key)){e.preventDefault();show(page+1)}else if(['ArrowLeft','PageUp'].includes(e.key)){e.preventDefault();show(page-1)}else if(e.key==='Home'){e.preventDefault();show(0)}else if(e.key==='End'){e.preventDefault();show(slides.length-1)}else if(e.key.toLowerCase()==='n')document.body.classList.toggle('show-notes');else if(e.key==='Escape')document.body.classList.remove('show-notes')});
const initial=Number(location.hash.match(/^#slide-(\d+)$/)?.[1]||1)-1;show(initial);fit();
</script></body></html>`, 'utf8');
console.log(JSON.stringify({outputs:['docs/deliverables/项目手册.html','docs/deliverables/开题演示.html'],slides:slides.length,manualBytes:Buffer.byteLength(manual),offlineResources:true}, null, 2));
