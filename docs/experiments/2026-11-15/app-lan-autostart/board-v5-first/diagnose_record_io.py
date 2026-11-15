import json, os, pathlib, sys, threading, time, traceback
ROOT = pathlib.Path('/userdata/helmet-validation/20261115-native-app-v5-lan-1')
OUT = pathlib.Path('/userdata/helmet-validation/20261115-app-lan-validation-1')
sys.path.insert(0, str(ROOT / 'src'))
from app import resident
state = pathlib.Path('/userdata/helmet-validation/k5-io-1')
if state.exists(): raise FileExistsError('isolated diagnostic root exists')
stop = threading.Event()
events = []
class Probe(resident.Coordinator):
 def _stat(self, name):
  row = super()._stat(name)
  if row is not None and row.st_nlink != 1:
   events.append({'where':'_stat','name':name,'nlink':row.st_nlink,'inode':row.st_ino,'uid':row.st_uid,'mode':oct(row.st_mode),'t':time.monotonic()})
  return row
writer = resident.Coordinator(state).prepare()
reader = Probe(state).prepare()
writer.write_json('state.json', {'n':0})
counts = {'writes':0,'reads':0}
errors = []
def write():
 try:
  while not stop.is_set():
   counts['writes'] += 1
   writer.write_json('state.json', {'n':counts['writes']})
 except BaseException:
  errors.append({'where':'writer','traceback':traceback.format_exc()});stop.set()
thread = threading.Thread(target=write)
thread.start()
began=time.monotonic()
try:
 while time.monotonic()-began < 25 and not stop.is_set():
  try:
   reader.read_json('state.json'); counts['reads']+=1
  except BaseException:
   errors.append({'where':'reader','traceback':traceback.format_exc()});break
finally:
 stop.set();thread.join();reader.close();writer.close()
result={'kind':'isolated-private-record-I/O-diagnostic','npu_exercised':False,'elapsed':time.monotonic()-began,'counts':counts,'metadata_events':events,'errors':errors}
with (OUT/'record-io-diagnostic-1.json').open('x') as stream:json.dump(result,stream,indent=2)
print(json.dumps(result),flush=True)
