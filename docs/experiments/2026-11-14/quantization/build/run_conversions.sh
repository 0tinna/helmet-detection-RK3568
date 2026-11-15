#!/bin/bash
set -euo pipefail
cd /home/ubuntu/conversion
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
grep -m1 '^flags' /proc/cpuinfo | grep -q ' avx ' || { echo 'AVX CPUID prerequisite missing'; exit 2; }
grep -m1 '^flags' /proc/cpuinfo | grep -q ' xsave ' || { echo 'XSAVE CPUID prerequisite missing'; exit 2; }
ulimit -c 0
venv/bin/python convert_pair.py > fp16.log 2>&1
touch fp16-complete
venv/bin/python convert_pair.py --int8 --dataset calibration/dataset.txt > int8.log 2>&1
touch int8-complete
