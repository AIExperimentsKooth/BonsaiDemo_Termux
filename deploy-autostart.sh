#!/bin/sh
# Deploy the auto-start files to the phone, restart both services, verify.
# Run from the project root:  sh bonsai-harness/deploy-autostart.sh
# Override the host:          BONSAI_HOST=u0_a431@192.168.0.61 sh ...
set -e

PHONE="${BONSAI_HOST:-u0_a431@192.168.0.61}"
PHONE_IP="${PHONE#*@}"
KEY="bonsai-harness/id_termux"
SSH="ssh -i $KEY -p 8022 -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no $PHONE"

echo "== copying files =="
scp -i "$KEY" -P 8022 -o StrictHostKeyChecking=no \
    bonsai-harness/start-all.sh bonsai-harness/start_llama_tight.sh \
    bonsai-harness/bashrc-snippet.sh \
    "$PHONE:~/agent-harness/"

echo "== installing hooks =="
$SSH 'chmod +x ~/agent-harness/start-all.sh ~/agent-harness/start_llama_tight.sh
      touch ~/.bashrc
      if grep -q "agent-harness/start-all.sh" ~/.bashrc; then
          echo "bashrc hook already present"
      else
          printf "\n" >> ~/.bashrc
          cat ~/agent-harness/bashrc-snippet.sh >> ~/.bashrc
          echo "bashrc hook appended"
      fi
      if [ ! -f ~/.bash_profile ]; then
          printf "[ -f ~/.bashrc ] && . ~/.bashrc\n" > ~/.bash_profile
          echo "created .bash_profile"
      elif ! grep -q ".bashrc" ~/.bash_profile; then
          printf "[ -f ~/.bashrc ] && . ~/.bashrc\n" >> ~/.bash_profile
          echo ".bash_profile now sources .bashrc"
      else
          echo ".bash_profile already sources .bashrc"
      fi
      rm -f ~/agent-harness/bashrc-snippet.sh
      echo "--- .bashrc ---"; cat ~/.bashrc
      echo "--- .bash_profile ---"; cat ~/.bash_profile'

echo "== stopping existing processes =="
$SSH 'for pid in $(ps -A -o pid,args | grep -E "[l]lama-server|[p]ython3 app.py" | awk "{print \$1}"); do
          echo "killing $pid"; kill "$pid" 2>/dev/null || true
      done
      sleep 3
      ps -A -o pid,args | grep -E "[l]lama-server|[p]ython3 app.py" || echo "both stopped"
      rmdir ~/.bonsai-autostart.lock 2>/dev/null || true'

echo "== starting via start-all.sh =="
$SSH 'cd ~/agent-harness && sh start-all.sh; sleep 3; ps -A -o pid,args | grep -E "[l]lama-server|[p]ython3 app.py"; echo "--- autostart.log ---"; tail -5 ~/autostart.log'

echo "== waiting for llama-server /health =="
i=0
ok=0
while [ $i -lt 90 ]; do
    code=$($SSH 'curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/health' 2>/dev/null || echo 000)
    if [ "$code" = "200" ]; then
        echo "llama-server healthy after ~$((i * 5))s"
        ok=1
        break
    fi
    i=$((i + 1))
    sleep 5
done
[ $ok -eq 1 ] || echo "WARNING: llama-server not healthy after 450s"

echo "== llama-server cmdline =="
$SSH 'ps -A -o args | grep "[l]lama-server" | head -2'

echo "== harness models endpoint =="
curl -s -m 10 "http://$PHONE_IP:8081/api/models"; echo
