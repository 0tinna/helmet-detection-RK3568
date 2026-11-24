"""Execute the LAN event page at browser timer/fetch boundaries."""
import importlib
import json
import re
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node is required for browser-script regression')
class AlertViewerBrowserTests(unittest.TestCase):
    def run_page(self, assertions):
        try:
            page = importlib.import_module('helmet.app.alert_viewer').PAGE.decode('utf-8')
        except (ImportError, AttributeError) as error:
            self.fail('LAN event page is not implemented: ' + str(error))
        script = re.search(r'<script>([\s\S]*?)</script>', page).group(1)
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
let nextTimer=1; const timers=new Map();
class Node{
 constructor(tag='div'){this.tagName=tag;this.children=[];this.textContent='';this.hidden=false;
  this.className='';this.attributes={};this.listeners={};
  this.classList={add:value=>{if(!this.className.split(/\s+/).includes(value))this.className=(this.className+' '+value).trim()},
                   remove:value=>{this.className=this.className.split(/\s+/).filter(x=>x!==value).join(' ')}};}
 appendChild(child){child.parentNode=this;this.children.push(child);return child}
 replaceChildren(...children){this.children=children}
 addEventListener(name,fn){this.listeners[name]=fn}
 setAttribute(name,value){this.attributes[name]=value}
}
const selectors=['events','emptyState','connectionState','eventCount','latestState','storageState',
 'mailState','accessScope','dayFilter','applyFilter','loadMore','emptyTitle','emptyHint'];
const elements={};for(const id of selectors)elements['#'+id]=new Node();
const document={querySelector:selector=>elements[selector]||new Node(),
 createElement:tag=>new Node(tag),addEventListener:(name,fn)=>{document.ready=fn}};
const location={search:''};
const context={document,location,elements,assert,URLSearchParams,Intl,Date,console,Number,Math,encodeURIComponent,
 setTimeout:(fn,delay)=>{const id=nextTimer++;timers.set(id,{fn,at:Date.now()+delay});return id},
 clearTimeout:id=>timers.delete(id),
 fetch:async()=>{throw new Error('unhandled fetch')}};
vm.createContext(context);
'''
        combined = '(async()=>{\n' + script + '\n' + assertions + '\n})()'
        program = harness + '\nconst testPromise=vm.runInContext(' + json.dumps(
            combined) + ',context);\n' + (
            'testPromise.then(()=>process.exit(0),error=>{console.error(error);process.exit(1)});')
        result = subprocess.run([shutil.which('node'), '-'], input=program,
                                encoding='utf-8', errors='replace',
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_events_render_from_the_authenticated_api(self):
        self.run_page(r'''
let requested='';
fetch=async url=>{requested=url;return {ok:true,json:async()=>({events:[
 {event_id:'20261124T142530000000Z-ab12cd34',captured_wall_time:'2026-11-24T14:25:30.000+08:00',count:2,detections:[]},
 {event_id:'20261124T135800000000Z-ab12cd34',captured_wall_time:'2026-11-24T13:58:00.000+08:00',count:1,detections:[]}
],next_before:'20261124T135800000000Z-ab12cd34'})}};
await loadEvents();
assert.match(requested,/^\/api\/events\?limit=/);
assert.equal(elements['#events'].children.length,2);
assert.equal(elements['#emptyState'].hidden,true);
assert.match(elements['#eventCount'].textContent,/2/);
''')

    def test_empty_and_fetch_failure_states_are_truthful(self):
        self.run_page(r'''
fetch=async()=>({ok:true,json:async()=>({events:[],next_before:null})});
await loadEvents();
assert.equal(elements['#emptyState'].hidden,false);
assert.match(elements['#emptyTitle'].textContent,/尚未/);
fetch=async()=>{throw new Error('private network detail')};
await loadEvents();
assert.match(elements['#connectionState'].textContent,/重试/);
assert.equal(elements['#connectionState'].textContent.includes('private network detail'),false);
''')

    def test_lan_scope_and_mail_failure_are_visible(self):
        self.run_page(r'''
renderStatus({
 detection:{state:'running'},
 mail:{state:'failed'},
 viewer:{alive:true,scope:'lan'},
 store:{records:4,bytes:8192}
});
assert.match(elements['#accessScope'].textContent,/ONLINE/);
assert.match(elements['#mailState'].textContent,/失败/);
assert.match(elements['#storageState'].textContent,/4/);
renderStatus({detection:{state:'idle'},mail:{state:'not_configured'},
 viewer:{alive:false,scope:'lan'},store:{records:0,bytes:0}});
assert.match(elements['#accessScope'].textContent,/OFFLINE/);
assert.match(elements['#mailState'].textContent,/未配置/);
''')

    def test_day_filter_and_pagination_are_reflected_in_requests(self):
        self.run_page(r'''
const urls=[];
fetch=async url=>{urls.push(url);return {ok:true,json:async()=>({events:[],next_before:null})}};
elements['#dayFilter'].value='2026-11-24';
await applyFilters();
assert.match(urls[0],/day=2026-11-24/);
currentBefore='20261124T135800000000Z-ab12cd34';
await loadEvents(true);
assert.match(urls.at(-1),/before=20261124T135800000000Z-ab12cd34/);
''')

    def test_image_error_marks_the_card_without_breaking_the_page(self):
        self.run_page(r'''
const image=document.createElement('img');
const visual=document.createElement('div');
const card=document.createElement('article');card.appendChild(visual);visual.appendChild(image);
markImageError(image);
assert.match(card.className,/image-error/);
assert.equal(image.alt.includes('未戴安全帽'),true);
''')


if __name__ == '__main__':
    unittest.main()
