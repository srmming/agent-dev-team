#!/usr/bin/env python3
"""agent-dev-team 守卫（PreToolUse：Edit|Write|MultiEdit|NotebookEdit|Bash）。

- none（守卫关了）、normal（普通对话）：全部放行。
- subagent（子代理）、kuaixiu（对接窗口）：只拦上传类命令（git push / git merge / gh pr create / gh pr merge）。
- lead（负责人）：
  1. 不能用编辑工具改项目根目录里的文件；
  2. 命令行只拦「写」的动作（黑名单），其余放行；
  3. 上传关：合并 / 推送 / 开 PR 前，提交必须都在审核通过记录里（守卫自己不跑 Codex）；
  4. 半自动：不直接合并进默认分支、不直接推默认分支、不用 gh 合并 PR。
认命令时先剥掉包装器（env / sudo / rtk proxy / timeout ……），bash -c、eval、$( ) 等里面的命令也拆开查。

退出码 2 = 拦截，stderr 会反馈给 Claude。守卫自己出任何错都放行，不拦用户。
"""
import json
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import records  # noqa: E402
from common import auto_mode, git, project_root, role, state_home  # noqa: E402

PREFIX = "【Agent 开发团队 拦截】"
REVIEW_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review.py")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# 负责人命令行黑名单
WRITE_CMDS = {"rm", "mv", "cp", "tee", "touch", "truncate", "dd", "patch", "install", "ln", "chmod", "chown"}
GIT_WRITE = {"commit", "add", "checkout", "switch", "reset", "restore", "rebase", "cherry-pick",
             "stash", "apply", "am", "clean", "rm", "mv"}
FIND_WRITE = {"-delete", "-exec", "-execdir"}

# 拆命令用的表
OPERATORS = ("&>>", "<<<", "<<-", "&&", "||", ";;", "|&", "&>", ">>", ">&", ">|", "<<", "<&", "<>",
             ";", "|", "&", "<", ">")
WRITE_OPS = {">", ">>", "&>", "&>>", ">|", ">&", "<>"}
READ_OPS = {"<", "<<", "<<-", "<<<", "<&"}
SAFE_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}
KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "time"}
ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=")
# 包装器：剥掉它和它的选项，后面才是真正的命令。值是「后面跟一个值」的选项。
WRAPPER_OPTS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-P"},
    "rtk": set(),
    "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from",
             "-D", "--chdir", "-R", "--chroot", "-r", "--role", "-t", "--type", "-T", "--command-timeout",
             "-U", "--other-user"},
    "doas": {"-u", "-C"},
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "nohup": set(),
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "--class", "-n", "--classdata", "-p", "-P", "-u"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "gtimeout": {"-s", "--signal", "-k", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"},
    "caffeinate": {"-t", "-w"},
    "xargs": {"-I", "-n", "-P", "-L", "-s", "-E", "-d", "-a", "-J", "-R", "-S"},
}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "mksh", "ash", "fish"}
MAX_DEPTH = 5
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
MERGE_VALUE_OPTS = {"-m", "--message", "-F", "--file", "-s", "--strategy", "-X", "--strategy-option",
                    "--into-name", "--cleanup"}
PUSH_VALUE_OPTS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}

UNCLOSED = "命令里的引号或括号没有闭合，守卫没法检查。改写后再试。"


def block(msg):
    print(PREFIX + msg, file=sys.stderr)
    sys.exit(2)


# ---------- 拆命令 ----------

def _match_paren(s, i):
    """s[i] 是左括号，返回和它配对的右括号的位置。引号里的括号不算。"""
    depth, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                break
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                break
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("括号没有闭合")


def _close_backtick(s, i):
    """s[i] 是反引号，返回和它配对的反引号的位置。"""
    j = i + 1
    while j < len(s):
        if s[j] == "\\":
            j += 2
        elif s[j] == "`":
            return j
        else:
            j += 1
    raise ValueError("反引号没有闭合")


def tokenize(cmd):
    """把命令拆成 (类型, 文本) 列表：w = 词，o = 引号外的操作符，s = 引号或反引号里要另外检查的命令。
    引号里的 | ; > 等只算词的一部分，不算操作符。heredoc 的正文和注释直接跳过。
    引号、括号没闭合时抛 ValueError。"""
    toks, buf, has, i, n = [], [], False, 0, len(cmd)
    heredocs, want = [], None  # 等着跳过正文的 heredoc；刚看到 << 还没读到结束符

    def flush():
        nonlocal buf, has, want
        if has:
            word = "".join(buf)
            toks.append(("w", word))
            if want is not None:
                heredocs.append((word, want))
                want = None
        buf, has = [], False

    while i < n:
        c = cmd[i]
        if c == "\\" and i + 1 < n:
            if cmd[i + 1] != "\n":  # 反斜杠加换行是续行，直接跳过
                buf.append(cmd[i + 1])
                has = True
            i += 2
        elif c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                raise ValueError("单引号没有闭合")
            buf.append(cmd[i + 1:j])
            has = True
            i = j + 1
        elif c == "$" and cmd.startswith("$'", i):  # $'...' 里可以有 \'
            j = i + 2
            while j < n and cmd[j] != "'":
                j += 2 if cmd[j] == "\\" else 1
            if j >= n:
                raise ValueError("单引号没有闭合")
            buf.append(cmd[i + 2:j])
            has = True
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and cmd[j] != '"':
                if cmd[j] == "\\" and j + 1 < n:
                    buf.append(cmd[j + 1])
                    j += 2
                elif cmd[j] == "`":
                    k = _close_backtick(cmd, j)
                    toks.append(("s", cmd[j + 1:k]))
                    buf.append(cmd[j:k + 1])
                    j = k + 1
                elif cmd.startswith("$(", j):
                    k = _match_paren(cmd, j + 1)
                    inner = cmd[j + 2:k]
                    if not inner.startswith("("):  # $(( )) 是算术，不是命令
                        toks.append(("s", inner))
                    buf.append(cmd[j:k + 1])
                    j = k + 1
                else:
                    buf.append(cmd[j])
                    j += 1
            if j >= n:
                raise ValueError("双引号没有闭合")
            has = True
            i = j + 1
        elif c == "`":
            k = _close_backtick(cmd, i)
            toks.append(("s", cmd[i + 1:k]))
            buf.append(cmd[i:k + 1])
            has = True
            i = k + 1
        elif cmd.startswith("$((", i) or (cmd.startswith("((", i) and not has):
            # 算术：里面的 > < 不是重定向，整段当成一个词
            k = _match_paren(cmd, i + (1 if c == "$" else 0))
            buf.append(cmd[i:k + 1])
            has = True
            i = k + 1
        elif c == "#" and not has:  # 注释，跳到行尾
            j = cmd.find("\n", i)
            i = n if j < 0 else j
        elif c in " \t\r":
            flush()
            i += 1
        elif c == "\n":
            flush()
            toks.append(("o", c))
            i += 1
            while heredocs:  # heredoc 正文单独成一个 h 记号：只有喂给 shell 时才当命令查
                delim, strip_tabs = heredocs.pop(0)
                body = []
                while i < n:
                    j = cmd.find("\n", i)
                    line = cmd[i:n if j < 0 else j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
                    body.append(line)
                toks.append(("h", "\n".join(body)))
        elif c in "()":
            flush()
            toks.append(("o", c))
            i += 1
        elif c in ";|&<>":
            flush()
            op = next(o for o in OPERATORS if cmd.startswith(o, i))
            toks.append(("o", op))
            i += len(op)
            if op in ("<<", "<<-"):
                want = op == "<<-"
        else:
            buf.append(c)
            has = True
            i += 1
    flush()
    return toks


def parse(cmd, depth=0):
    """按引号外的操作符拆成一段段命令。
    返回 (段列表, 有没有写文件的重定向)；每段是 (词列表, 喂给它的 heredoc / here-string 正文列表)。"""
    toks = tokenize(cmd)
    segs, nested, writes, cond, i = [([], [])], [], False, False, 0
    pending = []  # 还没读到正文的 heredoc 属于哪一段
    while i < len(toks):
        kind, text = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ("o", "")
        if kind == "w":
            if text == "[[":
                cond = True
            elif text == "]]":
                cond = False
            segs[-1][0].append(text)
            i += 1
        elif kind == "s":
            if depth < MAX_DEPTH:
                sub, sub_writes = parse(text, depth + 1)
                nested += sub
                writes = writes or sub_writes
            i += 1
        elif kind == "h":
            if pending:
                pending.pop(0)[1].append(text)
            i += 1
        elif cond and text in ("<", ">", "&&", "||", "(", ")"):
            i += 1  # [[ a > b && c ]] 里是比较和逻辑运算，不是重定向
        elif text in WRITE_OPS:
            target = nxt[1] if nxt[0] == "w" else ""
            fd_copy = text == ">&" and (target.isdigit() or target == "-")  # 例如 2>&1
            if target not in SAFE_TARGETS and not fd_copy:
                writes = True
            i += 2 if nxt[0] == "w" else 1
        elif text in READ_OPS:
            if text in ("<<", "<<-"):
                pending.append(segs[-1])
            elif text == "<<<" and nxt[0] == "w":
                segs[-1][1].append(nxt[1])
            i += 2 if nxt[0] == "w" else 1  # 读入重定向，不算写文件
        else:
            segs.append(([], []))  # ; | && || & 换行 括号：后面是另一条命令
            i += 1
    return [s for s in segs + nested if s[0]], writes


def _skip_opts(words, value_opts):
    """去掉开头的选项（value_opts 里的选项连同后面的值一起去掉）。遇到 -- 去掉它就停。"""
    while words and words[0].startswith("-") and words[0] != "-":
        if words[0] == "--":
            return words[1:]
        words = words[2:] if words[0] in value_opts else words[1:]
    return words


def unwrap(words):
    """剥掉命令前面不算数的东西，剥到真正的命令为止。返回 (命令的词列表, 前面设的环境变量)。
    包括：VAR=值；do / then / ! / { / time 这类关键字；env、sudo、rtk（含 rtk proxy）、command、exec、
    nohup、timeout、nice、xargs 等包装器和它们的选项。"""
    words, assigns = list(words), {}
    while words:
        w = words[0]
        m = ASSIGN_RE.match(w)
        if m:
            assigns[m.group(1)] = w[len(m.group(0)):]
            words = words[1:]
            continue
        if w in KEYWORDS:
            words = _skip_opts(words[1:], set()) if w == "time" else words[1:]
            continue
        base = os.path.basename(w)
        if base not in WRAPPER_OPTS or len(words) == 1:
            break
        if base == "command" and any(x in ("-v", "-V") for x in words[1:3]):
            break  # command -v xxx 只是查命令在不在
        rest = words[1:]
        if base == "env":
            while rest and (rest[0].startswith("-") or ASSIGN_RE.match(rest[0])):
                a = rest[0]
                if a in ("-S", "--split-string") and len(rest) > 1:
                    rest = rest[1].split() + rest[2:]  # env -S "git push" 把字符串拆成命令
                    continue
                if a.startswith("--split-string="):
                    rest = a.split("=", 1)[1].split() + rest[1:]
                    continue
                m = ASSIGN_RE.match(a)
                if m:
                    assigns[m.group(1)] = a[len(m.group(0)):]
                    rest = rest[1:]
                elif a == "--":
                    rest = rest[1:]
                    break
                else:
                    rest = rest[2:] if a in WRAPPER_OPTS["env"] else rest[1:]
        elif base == "rtk":
            rest = rest[1:] if rest and rest[0] == "proxy" else rest
        else:
            rest = _skip_opts(rest, WRAPPER_OPTS[base])
            if base in ("timeout", "gtimeout") and rest:
                rest = rest[1:]  # 时长
        words = rest
    return words, assigns


def shell_script(words, bodies):
    """bash / sh -c "..."、eval "..."、喂给 shell 的 heredoc：返回里面要另外检查的命令文字列表。"""
    base = os.path.basename(words[0])
    if base == "eval":
        return [" ".join(words[1:])]
    if base not in SHELLS:
        return []
    args, want_c = words[1:], False
    while args:
        a = args[0]
        if a in ("-o", "+o", "-O", "+O"):
            args = args[2:]
            continue
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            want_c = want_c or "c" in a[1:]
            args = args[1:]
            continue
        if a.startswith("--") or a.startswith("+"):
            args = args[1:]
            continue
        break
    if want_c:
        return args[:1]
    if not args or args[0] in ("-", "/dev/stdin"):
        return list(bodies)  # 没给脚本文件：从标准输入读命令
    return []


def find_exec(words):
    """find 的 -exec / -execdir / -ok 后面跟的命令。"""
    out, i = [], 1
    while i < len(words):
        if words[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            while j < len(words) and words[j] not in (";", "+"):
                j += 1
            if j > i + 1:
                out.append(words[i + 1:j])
            i = j
        i += 1
    return out


def segments(cmd, depth=0):
    """返回 [(命令的词列表, 前面设的环境变量)]，包括 $( )、反引号、括号、bash -c、eval、
    喂给 shell 的 heredoc、find -exec 里的命令；和有没有写文件的重定向。拆不了抛 ValueError。"""
    segs, writes = parse(cmd)
    out = []
    for raw, bodies in segs:
        words, assigns = unwrap(raw)
        if not words:
            continue
        out.append((words, assigns))
        if depth >= MAX_DEPTH:
            continue
        for script in shell_script(words, bodies):
            sub, sub_writes = segments(script, depth + 1)
            out += sub
            writes = writes or sub_writes
        if os.path.basename(words[0]) == "find":
            for inner in find_exec(words):
                w, a = unwrap(inner)
                if w:
                    out.append((w, a))
    return out, writes


def commands(cmd):
    """返回命令里每一段的词列表（已剥掉包装器），和有没有写文件的重定向。"""
    segs, writes = segments(cmd)
    return [w for w, _ in segs], writes


def git_parts(words):
    """words[0] 是 git。返回 (子命令, 子命令后面的参数, 仓库位置或 None)。
    仓库位置来自 -C（可叠加）、--git-dir、--work-tree。"""
    rest, cdir, where = words[1:], None, None
    while rest and rest[0].startswith("-"):
        a = rest[0]
        if a == "-C" and len(rest) > 1:
            cdir = os.path.join(cdir, rest[1]) if cdir else rest[1]
        elif a in ("--git-dir", "--work-tree") and len(rest) > 1:
            where = where or rest[1]
        elif a.startswith("--git-dir=") or a.startswith("--work-tree="):
            where = where or a.split("=", 1)[1]
        rest = rest[2:] if a in GIT_VALUE_OPTS else rest[1:]
    if where:
        cdir = os.path.join(cdir, where) if cdir else where
    return (rest[0] if rest else ""), rest[1:], cdir


def positional(args, value_opts):
    """去掉选项（和选项带的值），只留位置参数。"""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out += args[i + 1:]
            break
        if a.startswith("-"):
            i += 2 if a in value_opts else 1
        else:
            out.append(a)
            i += 1
    return out


# ---------- 负责人：命令行黑名单 ----------

def _sed_inplace(words):
    i = 1
    while i < len(words):
        w = words[i]
        if w == "--":
            break
        if w.startswith("--in-place"):
            return True
        if w.startswith("-") and not w.startswith("--"):
            for k, ch in enumerate(w[1:], 1):
                if ch == "i":
                    return True
                if ch in "efl":  # 这几个选项带值：值在后面紧跟着，或者是下一个词
                    if k == len(w) - 1:
                        i += 1
                    break
        i += 1
    return False


def _perl_inplace(words):
    i = 1
    while i < len(words):
        w = words[i]
        if w == "--" or not w.startswith("-"):
            break  # 后面是脚本和脚本自己的参数
        for ch in w[1:]:
            if ch == "i":
                return True
            if ch in "eE":
                i += 1  # 下一个词是代码
                break
            if ch in "MmIxFdDVC":
                break  # 后面紧跟的是这个选项的值
        i += 1
    return False


def check_words(words):
    """检查一段命令（已去掉前缀）。返回拦截原因或 None。"""
    first = os.path.basename(words[0])
    if first in WRITE_CMDS:
        return f"负责人不能运行 `{first}`（会改动文件）。负责人只想、只调度；把这个改动写成派活单，派给子代理。"
    if first == "sed" and _sed_inplace(words):
        return "负责人不能用 `sed -i` 直接改文件。把这个改动派给子代理。"
    if first == "perl" and _perl_inplace(words):
        return "负责人不能用 `perl -i` 直接改文件。把这个改动派给子代理。"
    if first == "git":
        sub = git_parts(words)[0]
        if sub in GIT_WRITE:
            return f"负责人不能运行 `git {sub}`（不 commit、不切分支、不改工作区）。派给子代理。"
    if first == "find" and any(w in FIND_WRITE for w in words):
        return "负责人不能用 find 改动或删除文件（-delete / -exec）。派给子代理。"
    return None


def check_lead_bash(cmd):
    """负责人的命令行黑名单：只拦写的动作。返回拦截原因或 None。"""
    try:
        cmds, writes = commands(cmd)
    except ValueError:
        return UNCLOSED
    if writes:
        return "负责人不能用命令行写文件（检测到重定向）。把这个改动派给子代理；只想丢掉输出可以写到 /dev/null。"
    for words in cmds:
        reason = check_words(words)
        if reason:
            return reason
    return None


# ---------- 上传类命令 ----------

GH_API_MERGE = re.compile(r"(^|/)pulls/[^/]+/merge/?$|(^|/)merges/?$")
GH_API_REFS = re.compile(r"(^|/)git/refs(/|$)")


def _gh_api_kind(args):
    """gh api 的写操作：合并 PR、改分支指向。返回 pr-merge / api-refs 或 None。"""
    paths = positional(args, {"-X", "--method", "-H", "--header", "-f", "--raw-field", "-F", "--field",
                              "--input", "-q", "--jq", "-t", "--template", "--hostname", "--cache",
                              "-p", "--preview"})
    method = ""
    for i, a in enumerate(args):
        if a in ("-X", "--method") and i + 1 < len(args):
            method = args[i + 1].upper()
        elif a.startswith("--method="):
            method = a.split("=", 1)[1].upper()
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:].upper()
    has_fields = any(a in ("-f", "-F", "--field", "--raw-field", "--input") or a.startswith("--field=")
                     or a.startswith("--raw-field=") for a in args)
    writes = method in ("POST", "PUT", "PATCH", "DELETE") or (not method and has_fields)
    if not writes:
        return None
    for p in paths:
        p = p.split("?", 1)[0]
        if GH_API_MERGE.search(p):
            return "pr-merge"
        if GH_API_REFS.search(p):
            return "api-refs"
    return None


def ship_action(words):
    """这段命令是不是上传类。返回 (种类, 参数, 仓库位置) 或 None。
    种类：merge / push / pr-create / pr-merge / api-refs（gh api 直接改远端分支）。"""
    if not words:
        return None
    first = os.path.basename(words[0])
    if first == "git":
        sub, args, cdir = git_parts(words)
        if sub in ("merge", "push"):
            return sub, args, cdir
    elif first == "gh":
        if words[1:3] in (["pr", "create"], ["pr", "merge"]):
            return "pr-" + words[2], words[3:], None
        if words[1:2] == ["api"]:
            kind = _gh_api_kind(words[2:])
            if kind:
                return kind, words[2:], None
    return None


def has_ship(cmd):
    try:
        cmds, _ = commands(cmd)
    except ValueError:
        return False  # 拆不了的命令 shell 自己也跑不了
    return any(ship_action(words) for words in cmds)


def _cd(cur, words):
    """跟着命令里的 cd / pushd 走，后面的 git 才能在对的仓库里查。看不懂的路径返回 None。"""
    args = [w for w in words[1:] if w != "--" and not (w.startswith("-") and len(w) > 1)]
    if not args:
        return os.path.expanduser("~")
    if len(args) != 1 or "$" in args[0] or "`" in args[0] or args[0] == "-" or cur is None:
        return None
    path = os.path.normpath(os.path.join(cur, os.path.expanduser(args[0])))
    return path if os.path.isdir(path) else None


def current_branch(cwd):
    return git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or ""


def remotes(cwd):
    names = (git(cwd, "remote") or "").split()
    return sorted(names, key=lambda r: r != "origin")  # origin 排最前


def default_branch(cwd):
    for r in remotes(cwd):
        head = git(cwd, "symbolic-ref", "--short", f"refs/remotes/{r}/HEAD")
        if head and "/" in head:
            return head.split("/", 1)[1]
    for b in ("main", "master"):
        if git(cwd, "rev-parse", "--verify", "--quiet", "refs/heads/" + b):
            return b
    return None


def push_refspecs(args):
    return [s.lstrip("+") for s in positional(args, PUSH_VALUE_OPTS)[1:]]


def _push_all(args):
    return any(a in ("--all", "--branches", "--mirror") for a in args)


def _push_delete(args):
    return "--delete" in args or "-d" in args


def _push_dst(spec, cur):
    """一个 refspec 推到远端的哪个分支：「本地:远端」取冒号后面，HEAD 就是当前分支。"""
    dst = spec.split(":", 1)[1] if ":" in spec else spec
    if dst == "HEAD":
        return cur
    return dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst


def check_semi(cwd, kind, args):
    """半自动：不直接合并进默认分支、不直接推默认分支、不用 gh 合并 PR。没有远端的本地仓库不受限制。"""
    how = "开 PR 等用户点合并。做法：推任务分支，再 gh pr create。用户说「全自动」可以切回去。"
    if kind in ("pr-merge", "api-refs"):
        return "半自动模式：PR 由用户自己点合并，负责人不合并 PR、不直接改远端分支。用户说「全自动」可以切回去。"
    if kind not in ("merge", "push") or not remotes(cwd):
        return None
    default = default_branch(cwd)
    if not default:
        return None
    cur = current_branch(cwd)
    if kind == "merge":
        if cur == default and positional(args, MERGE_VALUE_OPTS):
            return f"半自动模式：不直接合并进 {default}，{how}"
        return None
    if _push_delete(args):
        return None
    specs = push_refspecs(args)
    if _push_all(args):
        hit = True
    elif specs:
        hit = default in [_push_dst(s, cur) for s in specs]
    elif "--tags" in args:
        hit = False  # 只推标签
    else:
        up = git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or ""
        hit = cur == default or up.split("/", 1)[-1] == default
    if hit:
        return f"半自动模式：不直接推 {default}，{how}"
    return None


def upload_base(cwd, tip):
    """要上传的提交从哪里算起：上游分支 > 远端的 HEAD > 远端的 main / master。都没有返回 None。"""
    up = git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", tip + "@{u}")
    if up:
        return up
    names = remotes(cwd)
    for r in names:
        head = git(cwd, "symbolic-ref", "--short", f"refs/remotes/{r}/HEAD")
        if head:
            return head
    for r in names:
        for b in ("main", "master"):
            if git(cwd, "rev-parse", "--verify", "--quiet", f"refs/remotes/{r}/{b}"):
                return f"{r}/{b}"
    return None


def resolves(cwd, ref):
    return bool(git(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}"))


def clean_merge(cwd, sha):
    """合并提交的内容是不是两边自动合出来的（没夹带别的改动）。git 太旧没法判断时按是处理。"""
    parents = (git(cwd, "rev-list", "--parents", "-n", "1", sha) or "").split()[1:]
    if len(parents) != 2:
        return False  # 章鱼合并或查不到：要审
    try:
        r = subprocess.run(["git", "merge-tree", "--write-tree", *parents], cwd=cwd,
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return False
    if r.returncode not in (0, 1):
        return True  # git 太旧，不支持 --write-tree：沿用「合并提交不用审」
    tree = git(cwd, "rev-parse", sha + "^{tree}")
    return r.returncode == 0 and bool(tree) and r.stdout.split()[:1] == [tree]


def unreviewed(cwd, rng, passed):
    """范围里还没通过审核的提交。两边自动合出来的合并提交不用审；夹带改动的合并提交要审。
    git 查不出来时返回 None（调用的地方按查不了处理，不当成通过）。"""
    revs = git(cwd, "rev-list", rng, "--")
    if revs is None:
        return None
    merges = set((git(cwd, "rev-list", "--merges", rng, "--") or "").split())
    return [s for s in revs.split() if s not in passed and not (s in merges and clean_merge(cwd, s))]


def review_msg(cwd, what, branch, base, count):
    return (
        f"有 {count} 个提交还没通过 Codex 审核，不能{what}。先跑审核（放后台跑，只看结论）：\n"
        f"python3 {shlex.quote(REVIEW_PY)} --branch {shlex.quote(branch)} --base {shlex.quote(base)}"
        f" --cwd {shlex.quote(project_root(cwd))}\n"
        "审核通过后再重试这条命令；不通过就把问题清单派回子代理修，修完重审。"
    )


CANT_CHECK = "守卫查不出这次要{what}哪些提交，不能放行。"


def _push_tips(cwd, args):
    """git push 要推的本地提交：refspec 冒号前面的部分；--all 是所有分支，--tags 加上所有标签。"""
    if _push_all(args):
        tips = (git(cwd, "for-each-ref", "--format=%(refname:short)", "refs/heads") or "").split()
    else:
        tips = [s.split(":", 1)[0] for s in push_refspecs(args)]
    if "--tags" in args or "--mirror" in args:
        tips += (git(cwd, "for-each-ref", "--format=%(refname)", "refs/tags") or "").split()
    if not tips and not _push_all(args) and "--tags" not in args:
        tips = ["HEAD"]
    return tips


def check_reviewed(cwd, kind, args):
    """上传关：要合并 / 上传的提交必须都在审核通过记录里。只查记录，不跑 Codex。"""
    passed = records.passed_commits(cwd)
    if kind == "api-refs":
        return "守卫查不了用 gh api 直接改远端分支要上传哪些提交。改用 git push，守卫会先查审核记录。"
    if kind == "merge":
        for branch in positional(args, MERGE_VALUE_OPTS):
            if not resolves(cwd, branch):
                continue  # 分支不存在，git 自己会报错
            missing = unreviewed(cwd, f"HEAD..{branch}", passed)
            if missing is None:
                return CANT_CHECK.format(what="合并")
            if missing:
                return review_msg(cwd, "合并", branch, current_branch(cwd) or "HEAD", len(missing))
        return None
    tips = ["HEAD"]
    if kind == "push":
        if _push_delete(args):
            return None
        tips = _push_tips(cwd, args)
    elif kind == "pr-create":
        for i, a in enumerate(args):
            if a in ("--head", "-H") and i + 1 < len(args):
                tips = [args[i + 1].split(":")[-1]]
            elif a.startswith("--head="):
                tips = [a.split("=", 1)[1].split(":")[-1]]
    for tip in tips:
        if not tip:
            continue  # 「:分支」是删远端分支，没有要上传的提交
        if any(c in tip for c in "*?["):
            return "守卫查不了带通配符的推送。一个分支一个分支地推。"
        if not resolves(cwd, tip):
            continue  # 本地没有这个分支或提交，git 自己会报错
        base = upload_base(cwd, tip)
        if not base:
            continue  # 纯本地仓库或第一次推送，查不了
        missing = unreviewed(cwd, f"{base}..{tip}", passed)
        what = "推送" if kind == "push" else "开 PR / 合并 PR"
        if missing is None:
            return CANT_CHECK.format(what=what)
        if missing:
            branch = git(cwd, "rev-parse", "--abbrev-ref", tip) or tip
            return review_msg(cwd, what, branch, base, len(missing))
    return None


def check_ship(cwd, cmd, auto):
    """负责人的上传关和半自动限制。返回拦截原因或 None。"""
    try:
        segs, _ = segments(cmd)
    except ValueError:
        return None  # 拆不了的命令已经被黑名单拦了
    codex_off = os.environ.get("AGENT_DEV_TEAM_CODEX", "").lower() == "off"
    cur = cwd
    for words, assigns in segs:
        if words[0] in ("cd", "pushd"):
            cur = _cd(cur, words)
            continue
        action = ship_action(words)
        if not action:
            continue
        kind, args, cdir = action
        if auto == "semi" and kind in ("pr-merge", "api-refs"):
            return check_semi(cwd, kind, args)
        env_dir = assigns.get("GIT_DIR") or assigns.get("GIT_WORK_TREE")
        if cdir and env_dir:
            loc = os.path.join(os.path.expanduser(env_dir), os.path.expanduser(cdir))
        else:
            loc = os.path.expanduser(cdir or env_dir or "")
        if cur is None and not os.path.isabs(loc):
            return ("守卫看不出这条命令在哪个目录里跑（前面的 cd 用了变量或看不懂的路径）。"
                    "改用 git -C <绝对路径> 再试。")
        where = os.path.join(cur or "/", loc) if loc else cur
        if git(where, "rev-parse", "--git-dir") is None:
            continue  # 不在仓库里，git 自己也跑不了
        reason = check_semi(where, kind, args) if auto == "semi" else None
        if not reason and not codex_off:
            reason = check_reviewed(where, kind, args)
        if reason:
            return reason
    return None


# ---------- 负责人：编辑工具 ----------

def _inside(path, root):
    return (path + os.sep).startswith(root.rstrip(os.sep) + os.sep)


def check_lead_edit(cwd, path):
    """负责人不能改项目根目录里的文件；项目外的（状态目录、临时目录、~/.claude 下的记忆）放行。"""
    if not path:
        return None
    root = os.path.realpath(project_root(cwd))
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))
    if not _inside(full, root):
        return None
    # 项目根目录正好包住了状态目录或 ~/.claude（比如 cwd 是家目录）时，这两处仍然放行
    for safe in (state_home(), os.path.join(os.path.expanduser("~"), ".claude")):
        safe = os.path.realpath(safe)
        if _inside(full, safe) and not _inside(root, safe):
            return None
    return ("负责人不写代码，也不直接改项目文件。把这个改动写成派活单，派给子代理；"
            "如果用户想亲自一起改，用 kuaixiu 技能开对接窗口，或者让用户说「普通对话」退出负责人模式。")


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(data, dict):
        sys.exit(0)
    r = role(data)
    if r in ("none", "normal"):
        sys.exit(0)
    tool = data.get("tool_name", "")
    inp = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()

    if tool == "Bash":
        cmd = inp.get("command", "") or ""
        if r == "subagent":
            if has_ship(cmd):
                block("子代理不能 push / 合并 / 开 PR。提交到自己的分支，把结果交回负责人。"
                      "要跟上主分支或解决合并冲突，用 git rebase。")
        elif r == "kuaixiu":
            if has_ship(cmd):
                block("对接窗口不能 push / 合并 / 开 PR。提交到当前分支后交回负责人，由负责人验收、过 Codex 审核再合并。"
                      "要跟上主分支，用 git rebase。")
        else:
            reason = check_lead_bash(cmd) or check_ship(cwd, cmd, auto_mode(data))
            if reason:
                block(reason)
    elif tool in EDIT_TOOLS and r == "lead":
        reason = check_lead_edit(cwd, inp.get("file_path") or inp.get("notebook_path") or "")
        if reason:
            block(reason)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)  # 守卫自己出错一律放行
