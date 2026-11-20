# BonsaiDemo_Termux

An agentic web harness for running **Ternary Bonsai 2 27B** (GGUF, `PTQ1_0`) on an
Android phone through Termux, plus the launcher that starts it all automatically
whenever Termux opens.

The harness is a small stdlib-only Python server. It exposes a chat UI that can drive
the model through file/bash tools, and it can swap the loaded GGUF for a different one
without touching the phone.

## What's in here

| File | Purpose |
| --- | --- |
| `app.py` | Stdlib-only HTTP server: chat UI, agent loop (`list_dir`, `read_file`, `write_file`, `patch`, `run_bash`, `finish`), model registry and model switching. |
| `index.html` | The web UI (chat, model selector, tool log). |
| `start_llama_tight.sh` | Wrapper around the Bonsai demo launcher that pins the memory-tight profile: `-np 1`, `-c 2048`, `q4_0` KV cache. Single source of truth for the flags. |
| `start-all.sh` | Idempotent starter for llama-server + harness. Safe to run repeatedly. |
| `bashrc-snippet.sh` | The `~/.bashrc` hook that makes the stack start on Termux launch. |
| `deploy-autostart.sh` | Copies the above to the phone over SSH, installs the hook, restarts both services, waits for health. Run from the machine holding the SSH key. |
| `stub_model.py` | Tiny fake OpenAI-compatible server, for exercising the harness without a real model. |

## Requirements

- **Termux** on an arm64 Android device, with `python3` and `curl`, and `sshd` if you
  want to drive it over the LAN (`pkg install python openssh`).
- **The Bonsai demo tree** at `~/Bonsai-demo` — this repo does *not* vendor it. It
  provides `scripts/start_llama_server.sh`, `scripts/common.sh` and the CPU
  `bin/cpu/llama-server` binary (plus `scripts/build_termux.sh` to build it). The
  launcher is our flag-owner; `start_llama_tight.sh` just wraps it.
- **The GGUF weights**, e.g.
  `~/Bonsai-demo/models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PTQ1_0.gguf` (~5.7 GB).

## Install

Put the harness on the phone:

```sh
mkdir -p ~/agent-harness
# copy app.py, index.html, start-all.sh, start_llama_tight.sh, bashrc-snippet.sh
chmod +x ~/agent-harness/start-all.sh ~/agent-harness/start_llama_tight.sh
```

Then either run it by hand:

```sh
cd ~/agent-harness && sh start-all.sh
```

or from another machine, deploy and restart everything over SSH:

```sh
BONSAI_HOST=u0_a431@192.168.0.61 sh deploy-autostart.sh
```

The model takes roughly **3 minutes** to load into RAM on this phone. The harness
answers immediately but reports `current: null` until llama-server is healthy.

## Autostart

`bashrc-snippet.sh` is appended to `~/.bashrc`; `~/.bash_profile` sources `~/.bashrc`
so login shells find it too. The hook is interactive-only and runs once per shell
session:

```sh
if [ -z "${BONSAI_AUTOSTART:-}" ]; then
    case $- in
        *i*) export BONSAI_AUTOSTART=1
             [ -x "$HOME/agent-harness/start-all.sh" ] && "$HOME/agent-harness/start-all.sh" ;;
    esac
fi
```

That means opening Termux starts the stack, while `ssh host 'some command'` starts
nothing. `start-all.sh` only starts what is missing, uses `setsid nohup` so the
processes outlive the shell, and takes a `mkdir` lock so two shells opening at once
cannot double-start. Its log is `~/autostart.log`.

To start at **device boot** instead (or as well), install the Termux:Boot app and drop
a script in `~/.termux/boot/` that calls `start-all.sh`.

## Configuration

`start_llama_tight.sh` honours environment overrides:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BONSAI_GGUF` | base 27B GGUF | Model to load. The harness sets this per switch, so it wins over the default. |
| `BONSAI_CTX` | `2048` | Context size. |
| `BONSAI_KV4` | `1` | `1` = `q4_0` KV cache, `0` = f16. |

The harness takes the usual flags: `--host`, `--port` (default 8081),
`--base-url` (llama-server, default `http://127.0.0.1:8080`), `--workspace`,
`--server-script`, `--server-log`, `--models` (JSON list), `--no-bash`,
`--max-steps`, `--max-tokens`, `--timeout`, `--load-timeout`, `--unload-timeout`.

## Tools

The agent calls exactly one tool per turn and changes files with `patch` rather
than `write_file`, so a one-line fix does not mean resending the whole file:

```json
{"tool":"patch","path":"app.py","find":"old exact text","replace":"new text"}
{"tool":"patch","path":"app.py","edits":[{"find":"a","replace":"b"},{"find":"c","replace":""}]}
```

- `find` is matched **exactly** and must occur once; more than once is refused as
  ambiguous unless you pass `"replace_all":true`, and `"replace":""` deletes.
- All edits are validated before anything is written, so a bad block cannot leave a
  half-patched file.
- A miss explains itself: the error names the line where the first line of `find`
  does appear, which usually points at indentation or whitespace drift.
- Binary files, missing files and paths outside the workspace are refused.

## API

| Route | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Web UI |
| `/api/info` | GET | Effective settings (workspace, limits) |
| `/api/models` | GET | Registry, `present` flags, `current`, `busy`, `switching` |
| `/api/run` | POST | Run an agent turn (streams SSE) |
| `/api/switch` | POST | `{"model": "<id>"}` — unload the current GGUF, then load the new one (SSE: `unloading` → `unloaded` → `loading` → `switch_ready`) |

A switch is refused while a turn is streaming (`busy`) or while another switch runs,
and it unloads the old server **before** starting the new one — loading two 5.5 GB
models at once does not fit in the phone's 8 GB.

## Why the tight profile

With 4 slots, 4096 context and an f16 KV cache, llama-server's *anonymous* buffers
(compute + KV) grew to ~0.9 GB and Android's zram swapped ~1 GB of them during decode.
The model weights themselves never swap — they are a read-only mmap, `RssFile` stays
flat at ~4.8 GB.

Measured on a CPU-only 8-core device (2-token `/completion`):

| | 4 slots / 4096 ctx / f16 KV | 1 slot / 2048 ctx / q4_0 KV |
| --- | --- | --- |
| Decode | 54.45 s/token (0.0184 t/s) | 48.99 s/token (0.0204 t/s) |
| Prefill | 23.10 s/token (0.0433 t/s) | 19.73 s/token (0.0507 t/s) |
| Peak `VmSwap` | ~1022 MB | ~371 MB |
| `RssAnon` idle | 887 MB | 202 MB |

That is ~11% faster decode and ~17% faster prefill, but only ~0.02 t/s. This is a
fundamentally slow architecture on CPU — the effective read bandwidth is ~113 MB/s
(5.53 GB / 49 s), far below the phone's RAM bandwidth, so the `PTQ1_0`
dequant/matmul path is the bottleneck, not memory pressure. Shrinking the footprint
buys a little; it does not make this model fast.

## Notes

- CPU-only: the build reports `no usable GPU found`, so `-ngl 0` is expected.
- No root: `/proc/vmstat`, `/proc/swaps` and the zram device are unreadable, so
  swap behaviour has to be inferred from per-process `/proc/<pid>/status`
  (`VmSwap`, `RssAnon`, `RssFile`).
- Do not use `--no-mmap` (weights become anonymous and get zram-compressed) or
  `--mlock` (needs `CAP_IPC_LOCK`, unavailable).
- This repo deliberately excludes the SSH key used for deployment.
