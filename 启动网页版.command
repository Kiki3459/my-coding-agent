#!/bin/zsh
# Local launcher: no installation and no automatic agent task execution.
cd -- "$(dirname -- "$0")" || exit 1
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
if [ -x ".venv/bin/python" ]; then
    .venv/bin/python -m mini_agent.web_server --open "$@"
else
    python3 -m mini_agent.web_server --open "$@"
fi
if [ "$?" -ne 0 ]; then
    echo "启动未完成，请查看上方提示。按 Enter 关闭窗口。"
    read -r
fi
