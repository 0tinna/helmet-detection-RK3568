#!/usr/bin/env python3
"""Same NV12 test input, H.264 hardware/software CPU cost on RK3568.

Measured child CPU includes GStreamer source, encoding and muxing, not just
the encoder. Files are independently decoded/count-checked with FFprobe.
"""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import subprocess
import time


def run_one(output, encoder, width, height, frames, fps):
    target = output/(encoder+'.mp4')
    command = ['gst-launch-1.0', '-q', '-e', 'videotestsrc', 'num-buffers=%d' % frames,
        'pattern=ball', '!', 'video/x-raw,format=NV12,width=%d,height=%d,framerate=%d/1' %
        (width, height, fps), '!']
    command += (['mpph264enc', 'bps=2000000', 'gop=30'] if encoder=='hardware-mpp' else
                ['x264enc', 'bitrate=2000', 'speed-preset=ultrafast', 'tune=zerolatency',
                 'key-int-max=30', 'threads=1'])
    command += ['!', 'h264parse', '!', 'mp4mux', '!', 'filesink', 'location='+str(target)]
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started=time.monotonic()
    with (output/(encoder+'.log')).open('wb') as log:
        process=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=120)
    elapsed=time.monotonic()-started
    usage_after=resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu=(usage_after.ru_utime+usage_after.ru_stime)-(usage_before.ru_utime+usage_before.ru_stime)
    if process.returncode:
        raise RuntimeError('encoding failed: '+encoder)
    probe=subprocess.run(['ffprobe','-v','error','-count_frames','-show_entries',
        'stream=codec_name,nb_read_frames,duration,width,height','-of','json',str(target)],
        capture_output=True,text=True,timeout=30)
    if probe.returncode:
        raise RuntimeError('clip cannot be decoded: '+encoder)
    details=json.loads(probe.stdout)
    stream=details['streams'][0]
    if stream['codec_name']!='h264' or int(stream['nb_read_frames'])!=frames:
        raise RuntimeError('codec/frame verification failed: '+encoder)
    return {'encoder':encoder,'command':command,'wall_seconds':elapsed,'child_cpu_seconds':cpu,
            'one_core_cpu_percent':100*cpu/elapsed,'cpu_seconds_per_frame':cpu/frames,
            'pipeline_frames_per_second':frames/elapsed,'input':{'width':width,'height':height,
            'frames':frames,'fps':fps,'format':'NV12','pattern':'ball'},'ffprobe':details,
            'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'bytes':target.stat().st_size}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True)
    parser.add_argument('--frames',type=int,default=300)
    parser.add_argument('--width',type=int,default=1280)
    parser.add_argument('--height',type=int,default=720)
    parser.add_argument('--fps',type=int,default=30)
    args=parser.parse_args()
    output=Path(args.out).resolve()
    if not output.is_relative_to(Path('/userdata/helmet-validation')):
        parser.error('output must be below isolated validation root')
    output.mkdir(parents=True,exist_ok=False)
    rows=[]
    for encoder in ('hardware-mpp','software-x264'):
        rows.append(run_one(output,encoder,args.width,args.height,args.frames,args.fps))
    result={'status':'complete','runs':rows,'physical_camera_tested':False,
            'comparison':'same synthetic raw NV12 input; requested2Mbps; software ultrafast one-thread',
            'cpu_seconds_reduction_fraction':1-rows[0]['child_cpu_seconds']/rows[1]['child_cpu_seconds'],
            'limits':'Pipeline CPU cost, not isolated encoder; bitrate/visual quality may differ; no production-camera claim'}
    (output/'encoding-comparison.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
