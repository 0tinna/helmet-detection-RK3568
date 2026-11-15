#!/usr/bin/env python3
"""Helmet CLI. Keys: q/ESC quit, s saves a fresh result, p pauses/resumes.

The RKNN package is loaded only when a real inference session starts.
"""
import argparse
import sys


def build_parser():
    parser = argparse.ArgumentParser(description='RK3568 helmet detection pipeline')
    parser.add_argument('--source', default='/dev/video10',
                        help='camera /dev/videoN, video, image/image folder, glob, or RTSP URL')
    parser.add_argument('--model', default='/userdata/models/helmet_640x640.rknn')
    parser.add_argument('--conf', type=float, default=0.35)
    parser.add_argument('--nms', type=float, default=0.45)
    parser.add_argument('--skip', type=int, default=0, help='detect every N+1 consumed frames')
    parser.add_argument('--cam-w', type=int, default=640)
    parser.add_argument('--cam-h', type=int, default=480)
    parser.add_argument('--no-display', action='store_true')
    parser.add_argument('--save-dir', default='')
    parser.add_argument('--save-interval', type=float, default=0, help='save fresh results every N seconds')
    parser.add_argument('--alarm-beep', action='store_true')
    parser.add_argument('--duration', type=float, default=0, help='stop after N seconds, 0 runs continuously')
    parser.add_argument('--replay-fps', type=float, default=10, help='controlled image/video replay rate')
    parser.add_argument('--result-ttl', type=float, default=1, help='maximum capture-to-output age in seconds')
    parser.add_argument('--shutdown-timeout', type=float, default=2)
    parser.add_argument('--alarm-dwell', type=float, default=0.5)
    parser.add_argument('--alarm-cooldown', type=float, default=10)
    parser.add_argument('--alarm-clear', type=float, default=1)
    parser.add_argument('--audio-timeout', type=float, default=3)
    parser.add_argument('--jsonl', default='', help='exclusive per-frame JSONL output path')
    parser.add_argument('--session-json', default='', help='session counters, quantiles and runtime samples')
    parser.add_argument('--stats-window', type=float, default=5)
    parser.add_argument('--sample-interval', type=float, default=1)
    parser.add_argument('--web-host', default='127.0.0.1', help='preview bind address; LAN use 0.0.0.0')
    parser.add_argument('--web-port', type=int, default=0, help='HTTP preview port, 0 disables')
    parser.add_argument('--clip-dir', default='', help='event clips and coverage metadata directory')
    parser.add_argument('--clip-pre', type=float, default=5)
    parser.add_argument('--clip-post', type=float, default=5)
    parser.add_argument('--clip-fps', type=float, default=10)
    parser.add_argument('--clip-encoder', choices=['opencv', 'mpp'], default='opencv')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    sinks = []
    try:
        if not 0 <= args.web_port <= 65535:
            raise ValueError('web port must be 0..65535')
        # Validate pipeline arguments before opening sockets/output directories.
        pipeline = Pipeline(args)
        if args.web_port or args.clip_dir:
            try:
                from .output_sinks import WebPreview, EventClipRecorder
            except ImportError:
                from output_sinks import WebPreview, EventClipRecorder
            if args.web_port:
                sinks.append(WebPreview(args.web_host, args.web_port,
                                        status_provider=pipeline.status_snapshot,
                                        result_ttl=args.result_ttl))
            if args.clip_dir:
                sinks.append(EventClipRecorder(args.clip_dir, args.clip_pre,
                                               args.clip_post, args.clip_fps,
                                               args.clip_encoder))
            def publish(packet):
                for sink in sinks:
                    sink.on_packet(packet)
            def event(event):
                for sink in sinks:
                    sink.on_event(event)
            pipeline.output_callback, pipeline.event_callback = publish, event
        print('Model: %s  Source: %s  Skip: %d  Conf: %.2f' %
              (args.model, args.source, args.skip, args.conf))
        summary = pipeline.run()
        for sink in sinks:
            sink.close()
        sinks.clear()
    except (ValueError, PipelineError, OSError, RuntimeError) as error:
        print('Pipeline error: ' + str(error), file=sys.stderr)
        return 1
    finally:
        for sink in sinks:
            try:
                sink.close()
            except Exception as error:
                print('Output shutdown error: ' + str(error), file=sys.stderr)
    print('Session state: %s; captured=%d detected=%d displayed=%d; detection FPS=%.2f' %
          (summary['state'], summary['counters']['captured'], summary['counters']['detected'],
           summary['counters']['displayed'], summary['full_session_fps']['detected']))
    return 0


# Re-export the previous public engine/source/judgment names for Python callers.
try:
    from .pipeline_runtime import Pipeline, PipelineError, RKNNEngine, parse_model_size, judge_wearing, draw_results
    from .sources import VideoSource, ImageFolderSource, make_source
except ImportError:
    from pipeline_runtime import Pipeline, PipelineError, RKNNEngine, parse_model_size, judge_wearing, draw_results
    from sources import VideoSource, ImageFolderSource, make_source


if __name__ == '__main__':
    raise SystemExit(main())
