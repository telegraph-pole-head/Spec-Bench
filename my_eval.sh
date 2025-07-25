cat my_eval.sh
SIZE=7

MODEL_NAME=vicuna-${SIZE}b-v1.3
Vicuna_PATH=/data/nzy/models/$MODEL_NAME

TEMP=0.0

if [ -z "$GPU_DEVICES" ]; then
    GPU_DEVICES=4
fi

SEED=2024
MAX_NEW_TOKENS=1024

bench_NAME="spec_bench"
torch_dtype="float16" # ["float32", "float64", "float16", "bfloat16"]

# CUDA_VISIBLE_DEVICES=${GPU_DEVICES} python -m evaluation.inference_baseline --model-path $Vicuna_PATH --model-id ${MODEL_NAME}-vanilla-${torch_dtype}-temp-${TEMP} --bench-name $bench_NAME --temperature $TEMP --dtype $torch_dtype
# CUDA_VISIBLE_DEVICES=${GPU_DEVICES} python -m evaluation.inference_pld --model-path $Vicuna_PATH --model-id ${MODEL_NAME}-pld-${torch_dtype} --bench-name $bench_NAME --dtype $torch_dtype
# CUDA_VISIBLE_DEVICES=${GPU_DEVICES} python -m evaluation.inference_pld --model-path $Vicuna_PATH --model-id ${MODEL_NAME}-pld-${torch_dtype} --bench-name $bench_NAME --dtype $torch_dtype --use-csd-mgram
# CUDA_VISIBLE_DEVICES=${GPU_DEVICES} python -m evaluation.inference_pld --model-path $Vicuna_PATH --model-id ${MODEL_NAME}-pld-${torch_dtype} --bench-name $bench_NAME --dtype $torch_dtype --use-csd-mgram --fallback "model"
# CUDA_VISIBLE_DEVICES=${GPU_DEVICES} python -m evaluation.inference_recycling --model-path $Vicuna_PATH --model-id ${MODEL_NAME}-recycling --bench-name $bench_NAME --temperature $TEMP --dtype $torch_dtype
# CUDA_VISIBLE_DEVICES=${GPU_DEVICES} USE_LADE=1 python -m evaluation.inference_lookahead --model-path $Vicuna_PATH --model-id ${MODEL_NAME}-lade-level-5-win-7-guess-7-${torch_dtype} --level 5 --window 7 --guess 7 --bench-name $bench_NAME --dtype $torch_dtype

# SWIFT Hyperparameters
OPT_INTERVAL=1
BAYES_INTERVAL=25
MAX_OPT_ITER=1000
MAX_TOLERANCE_ITER=400
MAX_SCORE=0.93
CONTEXT_WINDOW=50
SKIP_RATIO=0.4

# CUDA_VISIBLE_DEVICES=${GPU_DEVICES} python -m evaluation.inference_swift --model-path $Vicuna_PATH --model-id ${MODEL_NAME} \
#   --temperature $TEMP --dtype $torch_dtype --bench-name $bench_NAME --max-new-tokens ${MAX_NEW_TOKENS} \
#   --seed $SEED --context-window ${CONTEXT_WINDOW} --opt-interval ${OPT_INTERVAL} --bayes-interval ${BAYES_INTERVAL} --max-opt-iter ${MAX_OPT_ITER} \
#   --max-tolerance-iter ${MAX_TOLERANCE_ITER} --max-score ${MAX_SCORE} --skip-ratio ${SKIP_RATIO} --optimization --bayes # --cache-hit

# CUDA_VISIBLE_DEVICES=${GPU_DEVICES} python -m evaluation.inference_swift_pld --model-path $Vicuna_PATH --model-id ${MODEL_NAME} \
#   --temperature $TEMP --dtype $torch_dtype --bench-name $bench_NAME --max-new-tokens ${MAX_NEW_TOKENS} \
#   --seed $SEED --context-window ${CONTEXT_WINDOW} --opt-interval ${OPT_INTERVAL} --bayes-interval ${BAYES_INTERVAL} --max-opt-iter ${MAX_OPT_ITER} \
#   --max-tolerance-iter ${MAX_TOLERANCE_ITER} --max-score ${MAX_SCORE} --skip-ratio ${SKIP_RATIO} --optimization --bayes # --cache-hit


SKIP_RATIO=0.4
OPT_INTERVAL_STEPS=128
DET=0.7
DRAFT_LEN=6

# CUDA_VISIBLE_DEVICES=${GPU_DEVICES} python -m evaluation.inference_clasp --model-path $Vicuna_PATH --model-id ${MODEL_NAME} \
#     --temperature $TEMP --dtype $torch_dtype --bench-name $bench_NAME --max-new-tokens ${MAX_NEW_TOKENS} \
#     --seed $SEED --skip-ratio $SKIP_RATIO --opt-interval $OPT_INTERVAL_STEPS --draft-exit-threshold $DET --draft-length-K $DRAFT_LEN -hc

CUDA_VISIBLE_DEVICES=${GPU_DEVICES} python -m evaluation.inference_casspec --model-path $Vicuna_PATH --model-id ${MODEL_NAME} \
    --temperature $TEMP --dtype $torch_dtype --bench-name $bench_NAME --max-new-tokens ${MAX_NEW_TOKENS} \
    --seed $SEED --skip-ratio $SKIP_RATIO --opt-interval $OPT_INTERVAL_STEPS --draft-exit-threshold $DET --draft-length-K $DRAFT_LEN


# echo "PLD"
# python evaluation/speed.py \
#     --base-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-vanilla-float16-temp-0.0.jsonl \
#     --file-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-pld-float16.jsonl \
#     --tokenizer-path $Vicuna_PATH

# echo "Recycling"
# python evaluation/speed.py \
#     --base-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-vanilla-float16-temp-0.0.jsonl \
#     --file-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-recycling.jsonl \
#     --tokenizer-path $Vicuna_PATH

# echo "SWIFT"
# python evaluation/speed.py \
#     --base-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-vanilla-float16-temp-0.0.jsonl \
#     --file-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-swift-float16-temp-0.0-top-p-0.85-seed-2024-max_new_tokens-1024-opt_interval-1-bayes_interval-25-max_opt-1000-max_tolerance-300-max_score-0.93-context_window-50-skip_ratio-${SKIP_RATIO}.jsonl \
#     --tokenizer-path $Vicuna_PATH

# echo "mySWIFT"
# python evaluation/speed.py \
#     --base-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-vanilla-float16-temp-0.0.jsonl \
#     --file-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-myswift-float16-temp-0.0-top-p-0.85-seed-2024-max_new_tokens-1024-opt_interval-1-bayes_interval-25-max_opt-1000-max_tolerance-300-max_score-0.93-context_window-50-skip_ratio-${SKIP_RATIO}.jsonl \
#     --tokenizer-path $Vicuna_PATH

# echo "Lade"
# python evaluation/speed.py \
#     --base-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-vanilla-float16-temp-0.0.jsonl \
#     --file-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-lade-level-5-win-7-guess-7-float16.jsonl \
#     --tokenizer-path $Vicuna_PATH

# echo "CLASP"
# python evaluation/speed.py \
#     --base-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-vanilla-float16-temp-0.0.jsonl \
#     --file-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-clasp-float16-temp-0.0-topp-0.85-seed-2024-maxntok-1024-I${OPT_INTERVAL_STEPS}-K${DRAFT_LEN}-DET${DET}-skip${SKIP_RATIO}.jsonl \
#     --tokenizer-path $Vicuna_PATH

# echo "CLASP-PLD"
# python evaluation/speed.py \
#     --base-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-vanilla-float16-temp-0.0.jsonl \
#     --file-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-clasp-float16-temp-0.0-topp-0.85-seed-2024-maxntok-1024-I${OPT_INTERVAL_STEPS}-K${DRAFT_LEN}-DET${DET}-skip${SKIP_RATIO}-hc.jsonl \
#     --tokenizer-path $Vicuna_PATH

echo "CASSPEC"
python evaluation/speed.py \
    --base-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-vanilla-float16-temp-0.0.jsonl \
    --file-path data/spec_bench/model_answer/vicuna-${SIZE}b-v1.3-casspec-float16-temp-0.0-I${OPT_INTERVAL_STEPS}-K${DRAFT_LEN}-DET${DET}-skip${SKIP_RATIO}.jsonl \
    --tokenizer-path $Vicuna_PATH

# python evaluation/equal.py \
#     --file-path data/spec_bench/model_answer/ \
#     --jsonfile1 vicuna-${SIZE}b-v1.3-vanilla-float16-temp-0.0.jsonl \
#     --jsonfile2 vicuna-${SIZE}b-v1.3-clasp-float16-temp-0.0-topp-0.85-seed-2024-maxntok-1024-I${OPT_INTERVAL_STEPS}-K${DRAFT_LEN}-DET${DET}-skip${SKIP_RATIO}.jsonl
    # --jsonfile2 vicuna-${SIZE}b-v1.3-pld-float16.jsonl



