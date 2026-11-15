"""User-visible native App presentation contracts.

These are source/JavaScript-boundary tests. Real compositor, camera and
physical-display behavior remain board acceptance items.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MAIN_QML = REPO / 'helmet/app/qml/Main.qml'
NODE = shutil.which('node')


def qml_text():
    return MAIN_QML.read_text(encoding='utf-8')


def qml_function(name):
    text = qml_text()
    found = re.search(r'function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', text)
    if not found:
        raise AssertionError('QML behavior missing: ' + name)
    depth, quote, escaped = 1, None, False
    index = found.end()
    while depth and index < len(text):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
        elif char in ('"', "'", '`'):
            quote = char
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
        index += 1
    if depth:
        raise AssertionError('unterminated QML function: ' + name)
    return text[found.start():index]


class QmlPresentationContractTests(unittest.TestCase):
    def test_window_uses_the_physical_screen_full_screen(self):
        text = qml_text()
        self.assertIn('import QtQuick.Window 2.15', text)
        for pattern in (
            r'\bwidth\s*:\s*Screen\.width\b',
            r'\bheight\s*:\s*Screen\.height\b',
            r'\bvisibility\s*:\s*Window\.FullScreen\b',
        ):
            with self.subTest(pattern=pattern):
                self.assertRegex(text, pattern)

    def test_detection_placeholder_is_hidden_while_detection_runs(self):
        self.assertRegex(
            qml_text(),
            r'Label\s*\{[^}]*text\s*:\s*"点击开始检测"[^}]*'
            r'visible\s*:\s*!detection\.running',
        )

    def test_preview_refresh_polling_matches_real_detection_rate(self):
        self.assertRegex(
            qml_text(),
            r'Timer\s*\{\s*interval\s*:\s*125\s*;[^}]*onTriggered\s*:\s*refreshFrame\(\)',
        )

    def test_preview_uses_two_ready_validated_image_buffers(self):
        text = qml_text()
        self.assertRegex(text, r'\bImage\s*\{\s*id\s*:\s*previewA\b')
        self.assertRegex(text, r'\bImage\s*\{\s*id\s*:\s*previewB\b')
        self.assertRegex(text, r'onStatusChanged\s*:\s*settlePreview\(previewA,\s*status\)')
        self.assertRegex(text, r'onStatusChanged\s*:\s*settlePreview\(previewB,\s*status\)')
        self.assertIn('preview = image', text)
        self.assertIn('property bool previewVisible: false', text)
        self.assertIn('image.opacity = 1', text)
        self.assertIn('function finishPreviewSwap', text)
        self.assertRegex(
            text,
            r'Timer\s*\{\s*id\s*:\s*previewHideTimer\s*;\s*interval\s*:\s*100\s*;'
            r'[^}]*onTriggered\s*:\s*finishPreviewSwap\(',
        )

    @unittest.skipUnless(NODE, 'Node unavailable for shipped QML JavaScript checks')
    def test_replacing_the_source_does_not_hide_the_current_image_first(self):
        script = """
const assert = require('assert');
var Image={Ready:1,Error:2};
var detection={running:true,valid:true,generation:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',source_epoch:4,selection:{}};
var window={visible:true,showFullScreen(){this.visible=true}};
var tabs={currentIndex:0};
var appStopping=false,capturePending=false,framePending=false,frameSerial=0,frameRequest=null;
var frameLoadPending=false,previewLoadTarget=null,previewSwapPending=false,previewSwapTarget=null,previewSwapSerial=0;
var previewHideTimer={restart(){this.restarted=true},stop(){this.stopped=true}};
var previewEpoch=0,frameExpires=0,minimumCaptureAt=0,lastStatus=Date.now();
var previewFrameId=null,previewCapturedAt=0,previewGeneration='',previewSourceEpoch=null,previewSelection={};
var frameRequests=0,frameDecodes=0,previewPresentations=0,requests=[];
var previewVisible=true;
var previewA={visible:true,opacity:1,z:0,source:'data:image/jpeg;base64,OLD',status:Image.Ready};
var previewB={visible:true,opacity:0,z:0,source:'',status:Image.Null};
var preview=previewA;
function request(method,route,payload,callback){
  requests.push({method,route,payload,callback});
  return {abort(){}};
}
function captureShow(){}
""" + qml_function('invalidatePreview') + '\n' + qml_function('refreshFrame') + '\n' \
        + qml_function('settlePreview') + '\n' + qml_function('finishPreviewSwap') + """
refreshFrame();
assert.equal(requests.length,1);
requests[0].callback(true,{valid:true,generation:detection.generation,source_epoch:4,
  captured_at:Date.now(),remaining_ttl_ms:900,jpeg_base64:'NEW',frame_id:8});
assert.equal(previewA.source,'data:image/jpeg;base64,OLD');
assert.equal(previewA.opacity,1,
  'source replacement must leave the old image visible until Image.Ready validates the new frame');
assert.equal(previewB.source,'data:image/jpeg;base64,NEW');
assert.equal(previewB.opacity,0);
assert.equal(frameLoadPending,true);
refreshFrame();
assert.equal(requests.length,1,'a decode must finish before the same buffer is overwritten');
previewB.status=Image.Ready;
settlePreview(previewB,Image.Ready);
assert.equal(frameLoadPending,false);
assert.equal(previewSwapPending,true);
assert.equal(previewA.opacity,1,'the old image stays composited until after the new layer is painted');
assert.equal(previewB.opacity,1);
assert.equal(previewB.z,2);
assert.equal(previewSwapTarget,previewA);
assert.equal(preview,previewB);
assert.equal(previewFrameId,8);
refreshFrame();
assert.equal(requests.length,2,'the covered old layer may preload the next frame during handoff');
assert.equal(previewB.opacity,1,'preloading must leave the new top layer visible');
finishPreviewSwap(previewA,previewSwapSerial);
assert.equal(previewSwapPending,false);
assert.equal(previewA.opacity,0);
assert.equal(previewB.opacity,1);
assert.equal(previewA.z,0);
assert.equal(previewB.z,0);
"""
        completed = subprocess.run(
            [NODE, '-'],
            input="const vm=require('vm');\n"
                  "var context={require:require,console:console};\n"
                  "context.global=context;\n"
                  "vm.createContext(context);\n"
                  "vm.runInContext(" + json.dumps(script) + ",context);\n",
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == '__main__':
    unittest.main()
