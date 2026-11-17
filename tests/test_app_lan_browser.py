"""Execute the actual phone page script at its timer/image/network boundaries."""
import importlib
import json
import re
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node is required for browser-script regression')
class AppLanBrowserTests(unittest.TestCase):
    def run_page(self, assertions):
        try:
            page = importlib.import_module('helmet.app.lan').PAGE.decode('utf-8')
        except ImportError:
            self.fail('the App LAN phone page is not implemented')
        script = re.search(r'<script>([\s\S]*?)</script>', page).group(1)
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
let now=100, nextTimer=1; const timers=new Map(), images=[];
const preview={style:{visibility:'hidden'},getContext:()=>({clearRect(){},drawImage(){}})};
const elements={preview,state:{textContent:'',className:''},stats:{textContent:''},placeholder:{hidden:false}};
class Picture{constructor(){this.naturalWidth=1280;this.naturalHeight=960;images.push(this)}
 set src(value){this.url=value} get src(){return this.url}}
const context={document:{querySelector:selector=>elements[selector.slice(1)]},Image:Picture,
 performance:{now:()=>now},AbortController,console,
 setTimeout:(fn,delay)=>{let id=nextTimer++;timers.set(id,{fn,at:now+delay});return id},
 clearTimeout:id=>timers.delete(id),fetch:()=>new Promise(()=>{})};
vm.createContext(context);
function advance(amount){now+=amount;for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.fn()}}
const packet={valid:true,generation:'a'.repeat(32),source_epoch:0,frame_id:7,
 captured_at:1,remaining_ttl_ms:1000,frame_token:'frame-one',running:true,
 state:'healthy',selection:{model:'helmet_416x416.rknn',source:'/dev/video10',profile:'responsive'}};
'''
        program = harness + '\nvm.runInContext(' + json.dumps(script) + ',context);\n' + assertions
        result = subprocess.run([shutil.which('node'), '-'], input=program, text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_display_expires_while_a_later_network_request_never_finishes(self):
        self.run_page(r'''
context.showPacket(packet,now);images.at(-1).onload();
assert.equal(preview.style.visibility,'visible');
advance(1001);
assert.equal(preview.style.visibility,'hidden');
assert.equal(elements.placeholder.hidden,false);
''')

    def test_binary_image_url_is_bound_to_the_exact_server_frame_token(self):
        self.run_page(r'''
context.showPacket(packet,now);
assert.equal(images.at(-1).src,'/frame.jpg?frame_token=frame-one');
assert.equal(JSON.stringify(packet).includes('jpeg_base64'),false);
images.at(-1).onload();
assert.equal(preview.style.visibility,'visible');
''')

    def test_late_image_decode_cannot_resurrect_expired_or_invalidated_frame(self):
        self.run_page(r'''
context.showPacket(packet,now);let delayed=images.at(-1);advance(1001);delayed.onload();
assert.equal(preview.style.visibility,'hidden');
context.showPacket(packet,now);delayed=images.at(-1);context.clearPreview();delayed.onload();
assert.equal(preview.style.visibility,'hidden');
''')

    def test_network_roundtrip_is_subtracted_from_the_server_remaining_ttl(self):
        self.run_page(r'''
const began=now;advance(800);context.showPacket(packet,began);images.at(-1).onload();
assert.equal(preview.style.visibility,'visible');advance(201);
assert.equal(preview.style.visibility,'hidden');
const count=images.length;context.showPacket(packet,now-1001);
assert.equal(images.length,count);assert.equal(preview.style.visibility,'hidden');
''')

    def test_new_source_epoch_discards_old_decode_before_the_new_image_is_ready(self):
        self.run_page(r'''
context.showPacket(packet,now);let old=images.at(-1);old.onload();
context.showPacket({...packet,source_epoch:1,frame_id:8},now);const current=images.at(-1);
assert.equal(preview.style.visibility,'hidden');old.onload();
assert.equal(preview.style.visibility,'hidden');current.onload();
assert.equal(preview.style.visibility,'visible');
context.showPacket({...packet,generation:'b'.repeat(32)},now);
assert.equal(preview.style.visibility,'hidden');current.onload();
assert.equal(preview.style.visibility,'hidden');
''')


if __name__ == '__main__':
    unittest.main()
