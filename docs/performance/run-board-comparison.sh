#!/bin/sh
# Run only in the isolated board validation directory. Restore NPU policy.
set -eu
RUN_DIR=/userdata/helmet-validation/20261113
NPU_GOV=/sys/class/devfreq/fde40000.npu/governor
ORIGINAL_GOV=$(cat "$NPU_GOV")
restore_governor() {
    RUN_STATUS=$?
    printf '%s\n' "$ORIGINAL_GOV" > "$NPU_GOV"
    RESTORED_GOV=$(cat "$NPU_GOV")
    if [ "$RESTORED_GOV" != "$ORIGINAL_GOV" ]; then
        printf 'NPU governor restoration failed\n' >&2
        exit 1
    fi
    printf 'restored_npu_governor=%s\n' "$RESTORED_GOV" >> "$RUN_DIR/results/environment.txt"
    return "$RUN_STATUS"
}
trap restore_governor EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

{
    uname -a
    python3 --version
    printf 'original_npu_governor=%s\n' "$ORIGINAL_GOV"
    printf 'npu_frequency_before='; cat /sys/class/devfreq/fde40000.npu/cur_freq
    printf 'temperature_before='; cat /sys/class/thermal/thermal_zone0/temp
    sha256sum "$RUN_DIR"/models/*.rknn
} > "$RUN_DIR/results/environment.txt"

cd "$RUN_DIR/src"
python3 benchmark.py --model-dir "$RUN_DIR/models" \
    --image /userdata/models/1.jpg --loops 50 --warmup 5 --conf 0.25 \
    --json "$RUN_DIR/results/comparison.json" > "$RUN_DIR/results/benchmark.log" 2>&1
python3 validate_comparison.py "$RUN_DIR/results/comparison.json"

{
    printf 'benchmark_npu_governor='; cat "$NPU_GOV"
    printf 'npu_frequency_after='; cat /sys/class/devfreq/fde40000.npu/cur_freq
    printf 'temperature_after='; cat /sys/class/thermal/thermal_zone0/temp
} >> "$RUN_DIR/results/environment.txt"
cat "$RUN_DIR/results/benchmark.log"
