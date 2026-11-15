import sys
import numpy as np
from rknnlite.api import RKNNLite

MODEL = sys.argv[1] if len(sys.argv) > 1 else '/userdata/models/helmet-640-640.rknn'

print('=== load model:', MODEL)
rknn = RKNNLite(verbose=False)
ret = rknn.load_rknn(MODEL)
print('load_rknn ret =', ret)
if ret != 0:
    print('FAILED to load rknn (platform mismatch?)')
    sys.exit(1)

ret = rknn.init_runtime()
print('init_runtime ret =', ret)
if ret != 0:
    print('FAILED to init runtime')
    sys.exit(2)

img = np.zeros((1, 640, 640, 3), dtype=np.uint8)
try:
    outs = rknn.inference(inputs=[img], data_format='nhwc')
    print('inference(nhwc) OK')
except Exception as e:
    print('nhwc failed:', e)
    outs = rknn.inference(inputs=[img])
    print('inference(default) OK')

for i, o in enumerate(outs):
    print('out[%d] shape=%s dtype=%s min=%.4f max=%.4f' %
          (i, o.shape, o.dtype, float(o.min()), float(o.max())))

rknn.release()
print('=== done')
