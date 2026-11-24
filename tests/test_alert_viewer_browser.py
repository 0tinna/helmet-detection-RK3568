"""Execute the direct LAN dashboard at browser timer/fetch boundaries."""
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
            page = importlib.import_module('helmet.app.alert_viewer').PAGE.decode(
                'utf-8')
        except (ImportError, AttributeError) as error:
            self.fail('LAN event page is not implemented: ' + str(error))
        script = re.search(r'<script>([\s\S]*?)</script>', page).group(1)
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
let nextTimer=1; const timers=new Map();
class Node{
 constructor(tag='div'){this.tagName=tag;this.children=[];this.textContent='';
  this.hidden=false;this.className='';this.attributes={};this.listeners={};
  this.classList={add:value=>{if(!this.className.split(/\s+/).includes(value))
    this.className=(this.className+' '+value).trim()},
   remove:value=>{this.className=this.className.split(/\s+/)
     .filter(x=>x!==value).join(' ')}};}
 appendChild(child){child.parentNode=this;this.children.push(child);return child}
 replaceChildren(...children){this.children=children}
 addEventListener(name,fn){this.listeners[name]=fn}
 setAttribute(name,value){this.attributes[name]=value}
 removeAttribute(name){delete this.attributes[name]}
 getAttribute(name){return this.attributes[name]??null}
}
const selectors=['events','emptyState','connectionState','currentAlert',
 'currentDetail','latestEvent','eventCount','storageState','accessScope',
 'dayFilter','applyFilter','loadMore','emptyTitle','emptyHint','currentCard'];
const elements={};for(const id of selectors)elements['#'+id]=new Node();
const document={querySelector:selector=>elements[selector]||new Node(),
 createElement:tag=>new Node(tag),createTextNode:value=>{const node=new Node('#text');
  node.textContent=String(value);return node},
 addEventListener:(name,fn)=>{document.ready=fn}};
const location={search:'',pathname:'/'};
const context={document,location,elements,assert,URLSearchParams,Intl,Date,console,
 Number,Math,encodeURIComponent,setTimeout:(fn,delay)=>{const id=nextTimer++;
  timers.set(id,{fn,at:Date.now()+delay});return id},
 clearTimeout:id=>timers.delete(id),
 fetch:async()=>{throw new Error('unhandled fetch')}};
vm.createContext(context);
'''
        combined = '(async()=>{\n' + script + '\n' + assertions + '\n})()'
        program = harness + '\nconst testPromise=vm.runInContext(' + json.dumps(
            combined) + ',context);\n' + (
            'testPromise.then(()=>process.exit(0),error=>{console.error(error);process.exit(1)});')
        result = subprocess.run(
            [shutil.which('node'), '-'], input=program, encoding='utf-8',
            errors='replace', capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_traceable_events_render_directly_from_the_api(self):
        self.run_page(r'''
let requested='';
fetch=async url=>{requested=url;return {ok:true,json:async()=>({events:[
 {event_id:'20261124T142530000000Z-ab12cd34',
  captured_wall_time:'2026-11-24T14:25:30.000+08:00',frame_id:77,count:2,detections:[]},
 {event_id:'20261124T135800000000Z-ab12cd34',
  captured_wall_time:'2026-11-24T13:58:00.000+08:00',frame_id:70,count:1,detections:[]}
],next_before:'20261124T135800000000Z-ab12cd34'})}};
await loadEvents();
assert.match(requested,/^\/api\/events\?limit=/);
assert.equal(elements['#events'].children.length,2);
assert.equal(elements['#emptyState'].hidden,true);
assert.match(elements['#events'].children[0].children[1].children[2].textContent,
 /77/);
assert.match(elements['#latestEvent'].textContent,/2026/);
assert.equal(elements['#connectionState'].attributes.role,'status');
assert.equal(elements['#connectionState'].attributes['aria-live'],'polite');
''')

    def test_empty_and_failure_states_are_truthful(self):
        self.run_page(r'''
fetch=async()=>({ok:true,json:async()=>({events:[],next_before:null})});
await loadEvents();
assert.equal(elements['#emptyState'].hidden,false);
assert.match(elements['#emptyTitle'].textContent,/尚未记录/);
assert.equal(elements['#emptyTitle'].textContent.includes('现场安全'),false);
fetch=async()=>{throw new Error('private network detail')};
await loadEvents();
assert.match(elements['#connectionState'].textContent,/重试/);
assert.equal(elements['#connectionState'].textContent.includes('private network detail'),false);
assert.equal(elements['#events'].attributes['aria-busy'],'false');
''')

    def test_current_alert_state_is_separate_from_the_archive(self):
        self.run_page(r'''
renderStatus({
 detection:{state:'healthy',running:true,valid:true,alarm_state:'active',
  frame_id:42,generation:'abcdef1234567890'},
 last_event_at:'2026-11-24T14:25:30.000+08:00',
 viewer:{alive:true,scope:'lan'},
 store:{records:4,bytes:8192,retention_days:7}
});
assert.match(elements['#currentAlert'].textContent,/DANGER/);
assert.match(elements['#currentAlert'].textContent,/NO HAT/);
assert.match(elements['#currentDetail'].textContent,/42/);
assert.match(elements['#latestEvent'].textContent,/2026/);
assert.match(elements['#eventCount'].textContent,/4/);
assert.match(elements['#storageState'].textContent,/8/);
assert.match(elements['#accessScope'].textContent,/ONLINE/);
assert.equal(elements['#currentCard'].className.includes('danger'),true);
renderStatus({detection:{running:true,valid:true,alarm_state:'clear'},
 viewer:{alive:true,scope:'lan'},store:{records:0,bytes:0}});
assert.match(elements['#currentAlert'].textContent,/CLEAR/);
renderStatus({detection:{running:false,valid:false,alarm_state:'unknown'},
 viewer:{alive:true,scope:'lan'},store:{records:0,bytes:0}});
assert.match(elements['#currentAlert'].textContent,/STANDBY/);
assert.equal(JSON.stringify(elements['#currentAlert']).includes('mail'),false);
''')

    def test_day_filter_and_pagination_are_reflected_in_requests(self):
        self.run_page(r'''
const urls=[];
fetch=async url=>{urls.push(url);return {ok:true,
 json:async()=>({events:[],next_before:null})}};
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
