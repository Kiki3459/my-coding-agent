"""Short, deterministic approval descriptions; never execute the command."""

import shlex


def describe_operation(tool: str, arguments: dict) -> str:
    if tool in {"edit", "write"}:
        path = str(arguments.get("path", "未指定文件"))
        action = "精确替换文件中的指定内容" if tool == "edit" else "写入文件内容（已有文件将被覆盖）"
        return f"{action}：{path}。"
    if tool != "bash":
        return f"调用 {tool} 工具执行本次操作。"

    command = str(arguments.get("command", ""))
    # Expansion, scripts and redirection can change the operation substantially.
    # Do not describe them as a harmless invocation of the first executable.
    if any(char in command for char in ("$", "`", "<", ">", "\n", "(", ")")):
        return "执行 Shell 命令（包含脚本、展开或重定向），具体操作见下方完整命令。"
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return "执行 Shell 命令，具体操作见下方完整命令。"
    segments, current = [], []
    for token in tokens:
        if token in {";", "&&", "||", "|", "&"}:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    descriptions = list(dict.fromkeys(_describe_command(parts) for parts in segments))
    if not descriptions:
        return "执行 Shell 命令，具体操作见下方完整命令。"
    prefix = "本次请求：" if len(segments) == 1 else "本次组合命令涉及："
    suffix = "；其余操作请查看完整命令" if len(descriptions) > 4 else ""
    return prefix + "；".join(descriptions[:4]) + suffix + "。"


def _describe_command(parts: list[str]) -> str:
    name = parts[0].rsplit("/", 1)[-1]
    args = parts[1:]
    if name in {"python", "python3", "python3.12"}:
        if args[:2] in (["-m", "unittest"], ["-m", "pytest"]):
            return "运行 Python 测试，检查代码是否通过测试"
        if args[:2] == ["-m", "py_compile"]:
            return "检查 Python 文件语法"
        return "运行 Python 程序或脚本"
    if name in {"pytest", "py.test"}:
        return "运行 Python 测试，检查代码是否通过测试"
    if name in {"gcc", "g++", "clang", "clang++"}:
        return "检查 C/C++ 代码语法" if "-fsyntax-only" in args else "编译 C/C++ 代码"
    if name == "find":
        if any(arg.startswith(("-exec", "-ok", "-delete", "-fprint", "-fls")) for arg in args):
            return "查找文件并执行附加操作，可能修改文件或运行命令"
        return "查找符合条件的文件或目录"
    if name == "sed":
        return "使用 sed 处理文本，可能写回文件或执行附加操作"
    if name == "git":
        return {"status": "查看 Git 工作区状态", "diff": "查看代码修改差异",
                "log": "查看 Git 提交历史"}.get(args[0] if args else "", "执行 Git 仓库操作")
    return {
        "ls": "查看目录中的文件", "pwd": "查看当前目录路径",
        "cd": "切换命令的工作目录", "cat": "读取并输出文件内容",
        "head": "查看文件开头", "tail": "查看文件末尾",
        "grep": "搜索匹配文本", "rg": "搜索代码或文件",
        "sort": "排序文本（指定输出文件时会写入文件）",
        "echo": "输出提示文本", "printf": "输出格式化文本",
        "rm": "删除指定文件或目录", "mv": "移动或重命名文件，可能覆盖目标",
        "cp": "复制文件或目录，可能覆盖目标", "mkdir": "创建目录",
        "touch": "创建文件或更新时间戳", "chmod": "修改文件权限",
        "curl": "发送网络请求，可能下载或上传数据",
        "wget": "发送网络请求并下载内容",
    }.get(name, f"运行 {name[:60]} 命令（具体行为见完整参数）")
