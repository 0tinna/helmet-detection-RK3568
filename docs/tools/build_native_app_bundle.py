import argparse,hashlib,json,zipfile
from pathlib import Path
root=Path(__file__).resolve().parents[2]
parser=argparse.ArgumentParser(description='Package the native app without replacing historical bundles.')
parser.add_argument('--revision',default='',help='safe revision suffix, for example v2')
args=parser.parse_args()
if args.revision and (not args.revision.isalnum() or len(args.revision)>16): parser.error('revision must be 1..16 letters or digits')
suffix='-'+args.revision if args.revision else ''
out=root/('dist/helmet-native-app-20261115'+suffix+'.zip')
if out.exists(): raise FileExistsError('refusing to replace an existing native app bundle: '+str(out))
payload={}
for p in (root/'helmet').glob('*.py'): payload['src/'+p.name]=p
for p in (root/'helmet/app').rglob('*'):
 if p.is_file() and '__pycache__' not in p.parts: payload['src/app/'+p.relative_to(root/'helmet/app').as_posix()]=p
for p in (root/'helmet/models').glob('*.rknn'): payload['models/'+p.name]=p
payload['models/helmet-640-640.rknn']=root/'models/helmet-640-640.rknn'
payload['input/1.jpg']=root/'models/1.jpg'
for p in (root/'docs/app').glob('*.md'): payload['docs/'+p.name]=p
manifest={'kind':'native-board-app','version':'20261115'+suffix,'requires_preinstalled_os_runtime':True,'requires_browser':False,'physical_camera_tested':False,'human_touch_verified':False,'wifi_connect_tested':False,'files':{}}
with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as z:
 for name,p in sorted(payload.items()):
  b=p.read_bytes();manifest['files'][name]={'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)};z.writestr(name,b)
 z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
 z.writestr('README.txt','RK3568 native Qt app. Existing compatible Qt5/qmlscene, Python3/OpenCV/NumPy/RKNNLite and wpa_supplicant required.\nExtract below /userdata/helmet-app then run: python3 src/app/install.py --root /userdata/helmet-app --vendor-icons\nStart icons after vendor home reload or run: python3 src/app/launch.py --root /userdata/helmet-app\nNo browser/internet required for local replay/detection. WiFi switching and physical camera/touch need human validation.\n')
with zipfile.ZipFile(out) as z:
 for n,v in manifest['files'].items():
  assert hashlib.sha256(z.read(n)).hexdigest()==v['sha256']
report={'path':out.relative_to(root).as_posix(),'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'bytes':out.stat().st_size,'files':len(payload),'manifest':manifest}
(root/('docs/experiments/2026-11-15/native-app/bundle-manifest'+suffix+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:report[k] for k in ('path','sha256','bytes','files')},indent=2))
