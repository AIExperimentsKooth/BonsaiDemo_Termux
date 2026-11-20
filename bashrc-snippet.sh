# Start the Bonsai llama-server + harness whenever Termux is launched.
# Interactive shells only, and at most once per shell session (a login shell
# reads this file via both .bash_profile and bash's own .bashrc lookup).
if [ -z "${BONSAI_AUTOSTART:-}" ]; then
    case $- in
        *i*)
            export BONSAI_AUTOSTART=1
            [ -x "$HOME/agent-harness/start-all.sh" ] && "$HOME/agent-harness/start-all.sh"
            ;;
    esac
fi
