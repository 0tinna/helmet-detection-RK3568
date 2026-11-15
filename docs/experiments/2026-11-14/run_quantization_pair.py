#!/usr/bin/env python3
"""Same-source FP16/INT8 performance and quality; preserve actual board policy."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='/userdata/helmet-validation/20261114')
    parser.add_argument('--run',required=True)
    args=parser.parse_args()
    root=Path(args.root).resolve();out=root/'results'/args.run
    if not root.is_relative_to(Path('/userdata/helmet-validation')):
        parser.error('isolated root required')
    out.mkdir(parents=True,exist_ok=False)
    gov=Path('/sys/class/devfreq/fde40000.npu/governor');old=gov.read_text().strip()
    manifest={'status':'running','started':datetime.now(timezone.utc).isoformat(),
              'original_governor':old,'models':{},'sources':{},'runs':[]}
    for name in ('benchmark.py','dump_dets.py','pc/eval_map.py','pc/dataset_tools.py','yolo_post.py'):
        manifest['sources'][name]=sha(root/'src'/name)
    def save(): (out/'run-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
    save()
    try:
        gov.write_text('performance')
        for precision in ('fp16','int8'):
            model=root/'models'/('helmet_416x416_same_source_'+precision+'.rknn')
            manifest['models'][precision]={'name':model.name,'sha256':sha(model),'bytes':model.stat().st_size}
            command=[sys.executable,str(root/'src/benchmark.py'),'--model',str(model),'--image',str(root/'input/1.jpg'),
                     '--loops','100','--warmup','10','--conf','.25','--json',str(out/('bench-'+precision+'.json'))]
            with(out/('bench-'+precision+'.log')).open('wb')as log:
                p=subprocess.run(command,cwd=root/'src',stdout=log,stderr=subprocess.STDOUT,timeout=180)
            if p.returncode:raise RuntimeError('benchmark failed:'+precision)
            result=json.loads((out/('bench-'+precision+'.json')).read_text())
            if len(result)!=1 or result[0]['loops']!=100:raise RuntimeError('benchmark incomplete')
            for dataset in ('shwd','hardhat'):
                stem=dataset+'-'+precision
                with(out/(stem+'.log')).open('wb')as log:
                    command=[sys.executable,str(root/'src/dump_dets.py'),'--model',str(model),'--data',str(root/dataset),
                             '--conf-min','.001','--op-conf','.25','--nms-iou','.45','--out',str(out/(stem+'.dump.json'))]
                    p=subprocess.run(command,cwd=root/'src',stdout=log,stderr=subprocess.STDOUT,timeout=300)
                    if p.returncode:raise RuntimeError('dump failed:'+stem)
                    p=subprocess.run([sys.executable,str(root/'src/pc/eval_map.py'),'--data',str(root/dataset),
                        '--dets',str(out/(stem+'.dump.json')),'--op-conf','.25','--out',str(out/(stem+'.metrics.json'))],
                        stdout=log,stderr=subprocess.STDOUT,timeout=120)
                    if p.returncode:raise RuntimeError('eval failed:'+stem)
                manifest['runs'].append({'precision':precision,'dataset':dataset,'status':'complete',
                    'dump_sha256':sha(out/(stem+'.dump.json')),'metrics_sha256':sha(out/(stem+'.metrics.json'))})
                save();print(stem+' complete',flush=True)
        manifest['status']='complete'
    except BaseException as e:
        manifest.update(status='failed',error=repr(e));raise
    finally:
        gov.write_text(old);manifest['restored_governor']=gov.read_text().strip()
        manifest['finished']=datetime.now(timezone.utc).isoformat();save()
        if manifest['restored_governor']!=old:raise RuntimeError('governor restore failed')


if __name__=='__main__':main()
