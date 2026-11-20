#!/data/data/com.termux/files/usr/bin/sh
# Launch llama-server with the tightened memory profile measured on this phone:
# 1 slot (-np 1), 2048 ctx, q4_0 KV cache. Peak swap drops from ~1 GB to ~371 MB
# and decode is ~11% / prefill ~17% faster than the 4-slot F16-KV config.
#
# Used both by start-all.sh (initial load) and by the harness for model switches
# (it sets BONSAI_GGUF itself, which takes precedence over the default below).
# Override any of these by exporting them first.

DEMO="$HOME/Bonsai-demo"
LAUNCHER="$DEMO/scripts/start_llama_server.sh"

export BONSAI_CTX="${BONSAI_CTX:-2048}"
export BONSAI_KV4="${BONSAI_KV4:-1}"
: "${BONSAI_GGUF:=$DEMO/models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PTQ1_0.gguf}"
export BONSAI_GGUF

exec sh "$LAUNCHER" -np 1 "$@"
