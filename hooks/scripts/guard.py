#!/usr/bin/env python3
"""agent-dev-team 守卫（PreToolUse：Edit|Write|MultiEdit|NotebookEdit|Bash）。

- none（守卫关了）、normal（普通对话）：全部放行。
- subagent（子代理）、kuaixiu（对接窗口）：命令里可能有上传动作就拦。
- lead（负责人）：
  1. 不能用编辑工具改项目根目录里的文件；
  2. 命令行只拦「写」的动作（黑名单），其余放行；
  3. 上传关，看不懂就拦：上传命令要单独一条、写成最简单的形式；要上传的提交必须能由完整的
     审核单元拼出来（见 records.py）。守卫自己不跑 Codex；
  4. 半自动：不直接合并进主分支、不直接推主分支、不用 gh 合并 PR。

上传动作 = git push / git merge / gh pr create / gh pr merge / gh api 的写操作。

退出码 2 = 拦截，stderr 会反馈给 Claude。普通命令遇到守卫自己出错一律放行；
负责人的上传命令遇到守卫出错、git 出错、查不出要上传什么，一律拦。
"""
import json
import os
import re
import shlex
import signal
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
KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "time", "coproc", "noglob", "nocorrect"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
# 包装器：剥掉它和它的选项，后面才是真正的命令。值是「后面跟一个值」的选项。
# 只有黑名单靠这张表；认上传动作不靠它（见 scan_ship）。
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
MAX_DEPTH = 8
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}

CANT_PARSE = "守卫拆不开这条命令（引号、括号没有闭合，或者一层套一层套得太深），没法检查。改写后再试。"


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


def _backtick_text(s, i, k):
    """反引号 s[i]..s[k] 里面的命令。里面的 \\` \\$ \\\\ 要先还原：\\`...\\` 是再套一层命令。"""
    return re.sub(r"\\([`$\\])", r"\1", s[i + 1:k])


def _arith_end(s, i):
    """s 从 i 开始是 "(("。确实是算术 (( ... )) 就返回最后一个右括号的位置；
    两层括号不是一起收尾的（其实是两层子 shell，比如 ((git push); x)）或没闭合，返回 None。"""
    try:
        inner, outer = _match_paren(s, i + 1), _match_paren(s, i)
    except ValueError:
        return None
    return outer if outer == inner + 1 else None


def _subs(text):
    """一段 shell 会展开的文字（算术、不带引号的 heredoc 正文）里套着的 $( ) 和反引号命令。"""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == "`":
            k = _close_backtick(text, i)
            out.append(_backtick_text(text, i, k))
            i = k + 1
        elif text.startswith("$(", i):
            k = _match_paren(text, i + 1)
            out += _paren_scripts(text, i + 1, k)
            i = k + 1
        else:
            i += 1
    return out


def _paren_scripts(s, i, k):
    """s[i] 是 $( 的左括号，k 是配对的右括号。返回里面要另外检查的命令文字。
    算术 $(( ... )) 只查里面套着的命令；其余整段都是命令。"""
    if s.startswith("((", i) and _arith_end(s, i) == k:
        return _subs(s[i + 2:k - 1])
    return [s[i + 1:k]]


ANSI_C = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
ANSI_C_RE = re.compile(r"\\(x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|U[0-9a-fA-F]{1,8}|[0-7]{1,3}|c.|.)", re.S)


def _ansi_c(text):
    """$'...' 里的转义还原成真正的字符（\\x70 就是 p），免得用转义拼出命令名。"""
    def one(m):
        e = m.group(1)
        try:
            if e[0] in "xuU" and len(e) > 1:
                return chr(int(e[1:], 16))
            if e[0] in "01234567":
                return chr(int(e, 8) & 0xFF)
        except ValueError:
            pass
        return "" if e[0] == "c" and len(e) > 1 else ANSI_C.get(e, e)
    return ANSI_C_RE.sub(one, text)


def _brace_word(s, i):
    """s[i] 是词开头的 {。同一个词里后面有 }（{a,b}、{}、{1..3}）就是花括号展开或占位，整个算一个词；
    没有（{git）就是 zsh 里不带空格的 { 命令 }。"""
    for c in s[i + 1:]:
        if c == "}":
            return True
        if c in " \t\r\n;|&<>()":
            return False
    return False


class Word(str):
    """拆出来的一个词。dyn = shell 还会再展开它（变量、命令替换、通配符、花括号展开），守卫看到的不是最后的样子。
    单引号里、反斜杠后面的 $ * ? 是原样的字，不算；双引号里只有 $ 和反引号算。"""
    dyn = False


def tokenize(cmd):
    """把命令拆成 (类型, 文本) 列表：w = 词，o = 引号外的操作符，s = 套在里面、要另外检查的命令，
    h = heredoc 的正文。引号里的 | ; > 等只算词的一部分，不算操作符。注释直接跳过。
    引号、括号没闭合时抛 ValueError。"""
    toks, buf, mask, has, quoted, i, n = [], [], [], False, False, 0, len(cmd)
    heredocs, want = [], None  # 等着读正文的 heredoc；刚看到 << 还没读到结束符
    parens = braces = 0  # 现在在几层括号里；当前这个词里还没配上对的 { 有几个

    def put(text, live="q"):
        """往当前这个词里加一段。live：这一段里 shell 还会展开的字（没有就是 q）。"""
        nonlocal has
        buf.append(text)
        mask.append(live)
        has = True

    def flush():
        nonlocal buf, mask, has, quoted, want, braces
        if has:
            word = Word("".join(buf))
            word.dyn = bool(DYN_RE.search("".join(mask)))
            toks.append(("w", word))
            if want is not None:
                heredocs.append((word, quoted) + want)
                want = None
        buf, mask, has, quoted, braces = [], [], False, False, 0

    while i < n:
        c = cmd[i]
        arith = _arith_end(cmd, i) if cmd.startswith("((", i) and not has else None
        if c == "\\" and i + 1 < n:
            if cmd[i + 1] != "\n":  # 反斜杠加换行是续行，直接跳过
                put(cmd[i + 1])
                quoted = True
            i += 2
        elif c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                raise ValueError("单引号没有闭合")
            put(cmd[i + 1:j])
            quoted = True
            i = j + 1
        elif c == "$" and cmd.startswith("$'", i):  # $'...' 里可以有 \'
            j = i + 2
            while j < n and cmd[j] != "'":
                j += 2 if cmd[j] == "\\" else 1
            if j >= n:
                raise ValueError("单引号没有闭合")
            put(_ansi_c(cmd[i + 2:j]))
            quoted = True
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and cmd[j] != '"':
                if cmd[j] == "\\" and j + 1 < n:
                    put(cmd[j + 1])
                    j += 2
                elif cmd[j] == "`":
                    k = _close_backtick(cmd, j)
                    toks.append(("s", _backtick_text(cmd, j, k)))
                    put(cmd[j:k + 1], "$")
                    j = k + 1
                elif cmd.startswith("$(", j):
                    k = _match_paren(cmd, j + 1)
                    toks += [("s", t) for t in _paren_scripts(cmd, j + 1, k)]
                    put(cmd[j:k + 1], "$")
                    j = k + 1
                else:
                    put(cmd[j], "$" if cmd[j] == "$" else "q")  # 双引号里只有 $ 还会展开
                    j += 1
            if j >= n:
                raise ValueError("双引号没有闭合")
            has = quoted = True
            i = j + 1
        elif c == "`":
            k = _close_backtick(cmd, i)
            toks.append(("s", _backtick_text(cmd, i, k)))
            put(cmd[i:k + 1], "$")
            i = k + 1
        elif cmd.startswith("$(", i):
            # 命令替换或算术 $(( ))：整段算进当前这个词（"$(brew --prefix)/bin/x" 是一个词），里面的命令另外查
            k = _match_paren(cmd, i + 1)
            toks += [("s", t) for t in _paren_scripts(cmd, i + 1, k)]
            put(cmd[i:k + 1], "$")
            i = k + 1
        elif arith is not None:
            # 算术 (( ))：里面的 > < 不是重定向，整段当成一个词；里面套着的 $( ) 照样要查
            toks += [("s", t) for t in _subs(cmd[i + 2:arith - 1])]
            put(cmd[i:arith + 1], "$")
            i = arith + 1
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
                delim, literal, strip_tabs, in_parens = heredocs.pop(0)
                body = []
                while i < n:
                    j = cmd.find("\n", i)
                    line = cmd[i:n if j < 0 else j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
                    body.append(line)
                text = "\n".join(body)
                toks.append(("h", text))
                if in_parens:
                    # 在括号里开的 heredoc：bash 把后面几行当正文，zsh 里 $(cat <<EOF) 后面几行会被当命令执行。
                    # 两种都算：既当正文，也当命令查
                    toks.append(("s", text))
                elif not literal:  # 结束符没加引号：正文里的 $( ) 和反引号会被执行
                    toks += [("s", t) for t in _subs(text)]
        elif c in "()":
            flush()
            toks.append(("o", c))
            parens = max(parens + (1 if c == "(" else -1), 0)
            i += 1
        elif c in ";|&<>":
            op = next(o for o in OPERATORS if cmd.startswith(o, i))
            if op[0] in "<>" and has and not quoted and "".join(buf).isdigit():
                buf, mask, has = [], [], False  # 2>&1 里的 2 是文件描述符，不是词
            flush()
            toks.append(("o", op))
            i += len(op)
            if op in ("<<", "<<-"):
                want = (op == "<<-", parens > 0)
        elif c == "{" and not has and not _brace_word(cmd, i):
            toks.append(("w", "{"))  # zsh 里 {git push} 不用空格：{ 单独算一个词
            i += 1
        elif c == "}" and has and not braces:
            flush()  # push} 拆成 push 和 }
            toks.append(("w", "}"))
            i += 1
        else:
            braces += (c == "{") - (c == "}" and braces > 0)
            put(c, c)  # 引号外的字：$ * ? [ ] { } 都可能被展开
            i += 1
    flush()
    return toks


def parse(cmd, depth=0):
    """按引号外的操作符拆成一段段命令。
    返回 (段列表, 有没有写文件的重定向)；每段是 (词列表, 喂给它的 heredoc / here-string 正文列表)。
    套得太深抛 ValueError（不能悄悄跳过里面的命令）。"""
    if depth > MAX_DEPTH:
        raise ValueError("命令套得太深")
    toks = tokenize(cmd)
    segs, nested, writes, cond, i = [([], [])], [], False, False, 0
    pending = []  # 还没读到正文的 heredoc 属于哪一段
    while i < len(toks):
        kind, text = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ("o", "")
        if kind == "w":
            if text == "[[" and all(w in KEYWORDS for w in segs[-1][0]):
                cond = True  # 只有在命令开头的 [[ 才是条件；echo [[ && xxx 里的不是
            elif text == "]]":
                cond = False
            segs[-1][0].append(text)
            i += 1
        elif kind == "s":
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
            cond = False
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
    """剥掉命令前面不算数的东西，剥到真正的命令为止，返回命令的词列表。
    包括：VAR=值；do / then / ! / { / time 这类关键字；env、sudo、rtk（含 rtk proxy）、command、exec、
    nohup、timeout、nice、xargs 等包装器和它们的选项。"""
    words = list(words)
    while words:
        w = words[0]
        if ASSIGN_RE.match(w):
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
                if ASSIGN_RE.match(a):
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
    return words


def cmd_name(word):
    """一个词当命令名时指的是哪个程序：去掉路径，转小写（macOS 的文件名不分大小写，GIT 也是 git），
    去掉 zsh 的 = 前缀（=git 是 git 的完整路径）。
    路径是命令替换拼出来的："$(brew --prefix)/bin/git" 看替换后面那段，是 git；
    "$(echo /usr/bin/git)" 整个都是替换出来的，原样返回（调用的地方会当成看不懂的命令名）。"""
    if "$(" in word or "`" in word:
        tail = re.split(r"[)`]", word)[-1]
        if "/" not in tail:
            return word
        word = tail
    return os.path.basename(word).lower().lstrip("=")


def scripts_in(words, bodies):
    """这段命令里交给 shell 再跑一遍的文字：bash / sh -c "..."、eval "..."、trap "..." 信号、alias 名字="..."、
    喂给 shell 的 heredoc。这几个词不管出现在第几个词都认（前面可能有守卫不认识的包装器）。
    返回 (脚本文字列表, 有没有 shell 从标准输入读命令而守卫看不到内容)。"""
    out, blind, seen_eval = [], False, False
    for i, w in enumerate(words):
        base = cmd_name(w)
        if base == "eval" and not seen_eval:
            # 只取第一个 eval：它的文字已经包住后面的了（eval eval eval ... 每个都取会越拆越多）
            out.append(" ".join(words[i + 1:]))
            seen_eval = True
        elif base == "trap":
            out += [a for a in words[i + 1:] if not a.startswith("-")][:1]
        elif base == "alias":
            out += [a.split("=", 1)[1] for a in words[i + 1:] if "=" in a]
        if base not in SHELLS:
            continue
        args, want_c = words[i + 1:], False
        while args:
            a = args[0]
            if a in ("-o", "+o", "-O", "+O"):
                args = args[2:]
            elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
                want_c = want_c or "c" in a[1:]
                args = args[1:]
            elif a.startswith("--") or a.startswith("+"):
                args = args[1:]
            else:
                break
        if want_c:
            out += args[:1]
        elif not args or args[0] in ("-", "/dev/stdin"):  # 没给脚本文件：从标准输入读命令
            out += bodies
            blind = blind or not bodies
    return out, blind


def segments(cmd, depth=0):
    """返回 (段列表, 有没有写文件的重定向, 有没有守卫看不到内容的 shell)。
    每段是原样的词列表（没剥包装器）。$( )、反引号、括号、bash -c、eval、喂给 shell 的 heredoc
    里的命令也在里面。拆不了或套得太深抛 ValueError。"""
    segs, writes = parse(cmd, depth)
    out, blind = [], False
    for words, bodies in segs:
        out.append(words)
        scripts, b = scripts_in(words, bodies)
        blind = blind or b
        for script in scripts:
            sub, sub_writes, sub_blind = segments(script, depth + 1)
            out += sub
            writes = writes or sub_writes
            blind = blind or sub_blind
    return out, writes, blind


def commands(cmd):
    """返回命令里每一段的词列表（已剥掉包装器），和有没有写文件的重定向。"""
    segs, writes, _ = segments(cmd)
    return [w for w in map(unwrap, segs) if w], writes


def git_parts(words):
    """words[0] 是 git。跳过 git 自己的选项，返回 (子命令, 子命令后面的参数)。"""
    rest = words[1:]
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in GIT_VALUE_OPTS else rest[1:]
    return (rest[0] if rest else ""), rest[1:]


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
    first = cmd_name(words[0])
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
        return CANT_PARSE
    if writes:
        return "负责人不能用命令行写文件（检测到重定向）。把这个改动派给子代理；只想丢掉输出可以写到 /dev/null。"
    for words in cmds:
        reason = check_words(words)
        if reason:
            return reason
    return None


# ---------- 上传动作：第一步，只判断「有没有可能上传」 ----------

# 命令拆不开、或有看不懂的结构时，命令文字里出现这些词就按「有上传动作」算
SHIP_HINT = re.compile(r"push|merge|send-pack|\bpr\s+create")
PLAIN_RE = re.compile(r"^[a-z][a-z-]*$")  # 写明了的子命令长这样
# 展开之后才知道是什么的词：变量、命令替换、通配符、花括号展开（bash 里 {git,push} 就是 git push）、find / xargs 的 {}
DYN_RE = re.compile(r"[$`*?]|\[[^\]]+\]|\{[^{}]*(,|\.\.)[^{}]*\}|\{\}")
# git 自己的选项里确定不带值的。别的选项都当成「后面那个词可能是它的值」（见 git_ship）
GIT_FLAGS = {"--no-pager", "-p", "-P", "--paginate", "--no-replace-objects", "--bare", "--literal-pathspecs",
             "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs", "--no-optional-locks",
             "--no-lazy-fetch", "--no-advice"}
GIT_SHIP_SUBS = {"push": "push", "merge": "merge", "send-pack": "push"}
# 命令的参数由别的程序拼出来：后面的 git / gh 实际跑什么，守卫看不出
ARG_FEEDERS = {"xargs", "parallel", "-exec", "-execdir", "-ok", "-okdir"}
GH_API_FLAGS = {"--paginate", "--slurp", "-i", "--include", "--silent", "--verbose"}
GH_API_VALUES = {"-q", "--jq", "-t", "--template", "-H", "--header", "--hostname", "--cache", "-p", "--preview"}
GH_API_FIELDS = {"-f", "--raw-field", "-F", "--field"}


def _dyn(word):
    """这个词 shell 还会再展开吗。拆命令时拆出来的词自己记着（见 Word）；别处拼出来的字符串按字面猜。"""
    return word.dyn if isinstance(word, Word) else bool(DYN_RE.search(word))


def git_ship(words):
    """把 words[0] 当成 git。返回 push / merge；"?" = 子命令看不懂（是变量，或带着临时别名）；
    "" = 后面没有子命令；None = 是别的子命令。
    不去记 git 的哪些选项带值：选项后面那个词可能是选项的值，也可能就是子命令，两种都算
    （git -C push status 会被多算成推送，换来的是 git --没见过的选项 值 push 不会漏）。"""
    subs, i = [], 1
    while i < len(words):
        w, prev = words[i], words[i - 1]
        if not w.startswith("-"):
            subs.append((w, words[i + 1:]))
            if not (i > 1 and prev.startswith("-") and "=" not in prev and prev not in GIT_FLAGS):
                break  # 前面不是「可能带值的选项」：w 确实是子命令
        i += 1
    if any(o.startswith("alias.") or o.startswith("--config-env") for o in words[1:i + 1]):
        return "?"  # git -c alias.x=push x
    for sub, args in subs:
        if sub in GIT_SHIP_SUBS:
            return GIT_SHIP_SUBS[sub]
        if sub in ("subtree", "lfs") and "push" in args:
            return "push"
    if not subs:
        return ""
    return "?" if _dyn(subs[-1][0]) else None


def gh_api_writes(args):
    """gh api 会不会改远端。只有明显只读的写法才算读：没指定方法或指定 GET，加上 --jq 这类只影响输出的选项；
    带字段（-f / -F）而没明说 GET、别的方法、graphql、看不懂的选项，都按写算。"""
    method, fields, paths, i = "", False, 0, 0
    while i < len(args):
        a = args[i]
        name, eq, val = a.partition("=")
        if a in ("-X", "--method") and i + 1 < len(args):
            method, i = args[i + 1].upper(), i + 2
        elif name == "--method" and eq:
            method, i = val.upper(), i + 1
        elif a in GH_API_FIELDS and i + 1 < len(args):
            fields, i = True, i + 2
        elif name in GH_API_FIELDS and eq and name.startswith("--"):
            fields, i = True, i + 1
        elif a in GH_API_VALUES and i + 1 < len(args):
            i += 2
        elif a in GH_API_FLAGS or (name in GH_API_VALUES and eq and name.startswith("--")):
            i += 1
        elif a.startswith("-") or a == "graphql" or _dyn(a) or paths:
            return True
        else:
            paths, i = 1, i + 1
    return method != "GET" and bool(method or fields)


def gh_ship(words):
    """把 words[0] 当成 gh。返回 pr-create / pr-merge / api（gh api 的写操作）/ "?"（子命令是变量）/ None。"""
    rest = words[1:]
    for i, w in enumerate(rest[:-1]):
        if w == "pr" and rest[i + 1] in ("create", "merge"):
            return "pr-" + rest[i + 1]
    first = next((i for i, w in enumerate(rest) if not w.startswith("-")), len(rest))
    if rest[first:first + 1] == ["api"]:
        return "api" if gh_api_writes(rest[:first] + rest[first + 1:]) else None
    if any(_dyn(w) for w in rest[:2]):
        return "?"
    return None


def scan_ship(cmd):
    """这条命令里有没有上传动作。返回 (种类的集合, 有没有看不懂的地方)。
    git / gh 不管出现在一段命令的第几个词都认，所以不靠包装器名单（timeout 30 git push、
    没见过的包装器 git push 都认得出）；代价是 echo git push 这种也算。
    变量、花括号展开当命令名时，把它当成 git 和 gh 各试一次（$GIT push）。
    守卫自己拆不开、出错，都算看不懂。"""
    try:
        segs, _, unknown = segments(cmd)
        kinds = set()
        for raw in segs:
            bare = unwrap(raw)
            if bare and _dyn(bare[0]) and _dyn(cmd_name(bare[0])):
                unknown = True  # 命令名要展开之后才知道，看不出跑的是什么
            for words in (raw, bare):  # bare：剥掉包装器再看一遍（env -S "git push" 这类）
                fed = bool(ARG_FEEDERS & set(words))
                for i, w in enumerate(words):
                    base = cmd_name(w)
                    guess = _dyn(w) and _dyn(base) and not ASSIGN_RE.match(w)  # 命令名那一段是展开出来的
                    if base.startswith("git-") and base[4:] in GIT_SHIP_SUBS:
                        kinds.add(GIT_SHIP_SUBS[base[4:]])  # <git 的程序目录>/git-push
                    if guess:  # 把这个词当成 git / gh 试一次；只认紧跟着的子命令，不然随便一个变量后面有 merge 都算
                        sub = git_parts(words[i:])[0]
                        kinds |= {k for k in (GIT_SHIP_SUBS.get(sub), gh_ship(words[i:])) if k and k != "?"}
                    for name, ship in (("git", git_ship), ("gh", gh_ship)):
                        if base != name:
                            continue
                        kind = ship(words[i:])
                        if kind:
                            kinds.add(kind)
                        # xargs git、xargs -I% git %：子命令是别的程序拼出来的。
                        # git branch --merged | xargs git branch -d 这种子命令写明了的不算
                        subs = [git_parts(words[i:])[0]] if name == "git" else words[i + 1:i + 3]
                        if kind == "" and i > 0 or fed and not all(PLAIN_RE.match(s) for s in subs):
                            unknown = True
        return kinds, unknown
    except Exception:
        return set(), True


def may_ship(cmd):
    """子代理、对接窗口用：命令里可能有上传动作。"""
    kinds, unknown = scan_ship(cmd)
    return bool(kinds) or (unknown and bool(SHIP_HINT.search(cmd)))


# ---------- 上传动作：第二步，负责人的上传关（只认最简单的写法，其余一律拦） ----------

REF_RE = re.compile(r"^[\w./@-]+$")
MERGE_FLAGS = {"--no-ff", "--ff", "--ff-only", "--no-edit", "-q", "--quiet", "--stat", "--no-stat"}
PUSH_FLAGS = {"-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose", "--progress", "-n", "--dry-run"}
PUSH_CONFIG = r"^(push\.default|remote\.pushdefault|remote\..*\.(push|mirror)|branch\..*\.pushremote)$"
GH_PR_VALUES = {"-a", "--assignee", "-B", "--base", "-b", "--body", "-F", "--body-file", "-H", "--head",
                "-l", "--label", "-m", "--milestone", "-p", "--project", "-r", "--reviewer",
                "-T", "--template", "-t", "--title"}
GH_PR_FLAGS = {"-d", "--draft", "--dry-run", "-f", "--fill", "--fill-first", "--fill-verbose",
               "--no-maintainer-edit"}
GH_PR_SHORT = {"-H": "--head", "-B": "--base"}

UNKNOWN = ("守卫看不懂这条命令（命令名是变量或展开、命令从标准输入喂给 shell、引号括号没闭合等），"
           "而命令里又出现了 push / merge 这类词，不能放行。把命令写直白些；"
           "要上传就单独运行最简单的写法，比如 `git push origin <分支>`。")
STANDALONE = ("上传命令（合并 / 推送 / 开 PR）请单独运行，不要和 cd、别的命令、管道、变量、包装器写在一起。"
              "守卫只认这几种写法：`git merge <分支>`、`git push`、`git push <远端> <分支>`、"
              "`gh pr create --head <分支>`；要指定仓库就在 git 后面加 `-C <路径>`。"
              "合并说明、PR 标题和正文直接写在引号里（可以换行；有 $ 或反引号就用单引号），"
              "不要用 $(cat <<EOF ...) 和变量。")
MERGE_HELP = "守卫只认 `git merge [--no-ff] [--no-edit] <分支> [-m <说明>]` 和 `git merge --abort`。"
PUSH_HELP = ("守卫只认这几种写法：`git push`、`git push <远端>`、`git push <远端> <分支>`、"
             "`git push <远端> <本地分支>:<远端分支>`，前面可以加 -u。")
PR_HELP = "守卫只认 `gh pr create` 加 --head / --base / --title / --body / --fill / --draft 这类选项，不能用 -R 指定别的仓库。"
SEMI_PR = "半自动模式：PR 由用户自己点合并，负责人不合并 PR、不直接改远端分支。用户说「全自动」可以切回去。"
SEMI_HOW = "开 PR 等用户点合并。做法：推任务分支，再 gh pr create。用户说「全自动」可以切回去。"
CANT_CHECK = "守卫查不出这次要{what}哪些提交，不能放行。"


def _ref_ok(name):
    """分支名只认最普通的写法：字母、数字、. / @ - _，不以 - 开头。带 ^ ~ : + * 空格的一律看不懂。"""
    return bool(REF_RE.match(name)) and not name.startswith("-") and ".." not in name


def rev(cwd, ref):
    return git(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}")


def current_branch(cwd):
    """当前分支名。游离的 HEAD 返回 "HEAD"。"""
    return git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or ""


def remotes(cwd):
    names = (git(cwd, "remote") or "").split()
    return sorted(names, key=lambda r: r != "origin")  # origin 排最前


def remote_default(cwd, remote):
    """远端的主分支（完整的引用名，如 refs/remotes/origin/main）：远端的 HEAD，没有就找 main / master。
    都没有返回 None。用完整的名字：本地再建一个叫 origin/main 的分支也不会认错。"""
    head = git(cwd, "symbolic-ref", f"refs/remotes/{remote}/HEAD")
    if head and head.startswith(f"refs/remotes/{remote}/"):
        return head
    for b in ("main", "master"):
        if rev(cwd, f"refs/remotes/{remote}/{b}"):
            return f"refs/remotes/{remote}/{b}"
    return None


def default_branch(cwd):
    for r in remotes(cwd):
        head = git(cwd, "symbolic-ref", "--short", f"refs/remotes/{r}/HEAD")
        if head and "/" in head:
            return head.split("/", 1)[1]
    for b in ("main", "master"):
        if git(cwd, "rev-parse", "--verify", "--quiet", "refs/heads/" + b):
            return b
    return None


def simple_upload(cmd):
    """整条命令是不是单独一条、写法最简单的上传命令：
    [rtk [proxy]] git [-C <路径>] merge|push <参数>，或 [rtk [proxy]] gh pr create <参数>。
    是就返回 (种类, 参数, -C 的路径或 None)，不是返回 None。
    这样守卫不用去猜 cd、子 shell、环境变量、前一条命令会把仓库变成什么样：目录就是钩子给的 cwd。"""
    try:
        toks = tokenize(cmd.strip())
    except ValueError:
        return None
    words, i = [], 0
    while i < len(toks):
        kind, text = toks[i]
        if kind == "w":
            words.append(text)
            i += 1
        elif (kind == "o" and (text in WRITE_OPS or text in ("<", "<&"))
              and i + 1 < len(toks) and toks[i + 1][0] == "w"):
            i += 2  # 重定向（2>&1、>/dev/null）不影响上传什么；写到哪里由黑名单管
        else:
            return None  # ; && | & 括号、换行、$( )、heredoc：不是单独一条命令
    if any(_dyn(w) for w in words):
        return None
    if words[:1] == ["rtk"]:
        words = words[2:] if words[1:2] == ["proxy"] else words[1:]
    if words[:1] == ["git"]:
        rest, cdir = words[1:], None
        if rest[:1] == ["-C"] and len(rest) > 1:
            cdir, rest = rest[1], rest[2:]
        if rest[:1] in (["merge"], ["push"]):
            return rest[0], rest[1:], cdir
    elif words[:3] == ["gh", "pr", "create"]:
        return "pr-create", words[3:], None
    return None


def merge_plan(cwd, args):
    """git merge 的参数。返回 None（--abort，没有要合并的提交）、拦截原因（字符串）、或这次合并的计划。"""
    if args in (["--abort"], ["--quit"]):
        return None
    if "--continue" in args:
        return ("负责人不解决合并冲突，也不接着做别人解到一半的合并。先 `git merge --abort`，"
                "派子代理在任务分支上用 git rebase 解决，重新验收、审核后再合并。")
    names, i = [], 0
    while i < len(args):
        a = args[i]
        if a in ("-m", "--message") and i + 1 < len(args):
            i += 2
        elif a in MERGE_FLAGS or a.startswith("--message="):
            i += 1
        elif a.startswith("-"):
            return f"守卫看不懂 git merge 的选项 `{a}`。" + MERGE_HELP
        else:
            names.append(a)
            i += 1
    if len(names) != 1 or not _ref_ok(names[0]):
        return "一次合并一个分支，分支名写最普通的形式。" + MERGE_HELP
    tip = rev(cwd, names[0])
    if not tip or not rev(cwd, "HEAD"):
        return f"找不到分支 `{names[0]}`（或者当前分支还没有提交），守卫查不了要合并什么，不能放行。"
    cur = current_branch(cwd)
    return {"what": "合并", "name": names[0], "base": "HEAD", "review_base": cur or "HEAD",
            "tip": tip, "target": cur}


def push_config_problem(cwd):
    """没明确写「本地:远端」时，推什么、推到哪要看仓库配置。配置了这些，守卫就看不懂了：
    push.default 不是 simple / current、remote.<名>.push、remote.<名>.mirror、pushRemote、pushDefault。
    返回有问题的配置名，没有返回 None。"""
    try:
        r = subprocess.run(["git", "config", "--get-regexp", PUSH_CONFIG], cwd=cwd,
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return "（git config 查不了）"
    if r.returncode not in (0, 1):  # 1 = 一条都没配
        return "（git config 查不了）"
    for line in r.stdout.splitlines():
        key, _, val = line.partition(" ")
        if key == "push.default" and val.strip() in ("simple", "current"):
            continue
        return key
    return None


def push_plan(cwd, args):
    """git push 的参数只认几种明确的写法。返回拦截原因（字符串）或这次推送的计划。"""
    pos = []
    for a in args:
        if a.startswith("--force") or (a.startswith("-") and not a.startswith("--") and "f" in a):
            return ("负责人不能强推（--force / --force-with-lease / -f）。任务分支 rebase 之后要重新推，"
                    "就换一个新的远端分支名推上去，重新开 PR。")
        if not a.startswith("-"):
            pos.append(a)
        elif a not in PUSH_FLAGS:
            return f"守卫看不懂 git push 的选项 `{a}`（--all、--mirror、--tags、--delete 这类都不认）。" + PUSH_HELP
    if len(pos) > 2:
        return "一次只推一个分支。" + PUSH_HELP
    names = remotes(cwd)
    if not names:
        return "这个仓库没有配置远端，推不了。"
    cur = current_branch(cwd)
    if len(pos) == 2 and ":" in pos[1]:
        remote, (src, dst) = pos[0], pos[1].split(":", 1)  # 两边都写明了，不受仓库配置影响
    else:
        bad = push_config_problem(cwd)
        if bad:
            return (f"仓库配置了 {bad}，守卫看不出这条命令会推到哪里。"
                    "改用两边都写明的写法：`git push <远端> <本地分支>:<远端分支>`。")
        if len(pos) == 2:
            remote, src = pos
            dst = cur if src == "HEAD" else src
        else:
            # 不带分支：只认「当前分支的上游就是同名的远端分支」
            up = git(cwd, "config", "--get", f"branch.{cur}.remote") if cur not in ("", "HEAD") else None
            same_name = up and git(cwd, "config", "--get", f"branch.{cur}.merge") == "refs/heads/" + cur
            if not same_name or (pos and pos[0] != up):
                return ("当前分支没有同名的上游分支，守卫看不出会推到哪里。"
                        "改用 `git push -u <远端> <分支>`。")
            remote, src, dst = up, "HEAD", cur
    if remote not in names:
        return f"`{remote}` 不是这个仓库配置的远端（不能直接推到网址或路径）。" + PUSH_HELP
    src = src[len("refs/heads/"):] if src.startswith("refs/heads/") else src
    dst = dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst
    if not _ref_ok(src) or not _ref_ok(dst) or dst == "HEAD" or dst.startswith("refs/"):
        return ("守卫看不懂要推的分支（`:`、`+`、通配符、删除远端分支、推标签这类都不认；"
                "游离的 HEAD 要写明推到哪个远端分支）。" + PUSH_HELP)
    tip = rev(cwd, "HEAD" if src == "HEAD" else "refs/heads/" + src)
    if not tip:
        return f"本地没有分支 `{src}`，守卫查不了要推什么，不能放行。"
    base = f"refs/remotes/{remote}/{dst}"
    if not rev(cwd, base):
        base = remote_default(cwd, remote)  # 远端还没有这个分支：从远端的主分支算起
    if not base:
        return (f"远端 {remote} 上还没有任何分支（第一次推送），守卫查不出哪些提交是新的，不能放行。"
                "第一次推送请用户自己运行，或者用户先说「普通对话」。")
    name = src if src != "HEAD" else (cur if cur not in ("", "HEAD") else "HEAD")
    return {"what": "推送", "name": name, "base": base, "review_base": base, "tip": tip, "target": dst,
            "remote": remote}


def pr_plan(cwd, args):
    """gh pr create 的参数。返回拦截原因（字符串）或这次开 PR 的计划。查的是本地的 head 分支。"""
    opts, i = {}, 0
    while i < len(args):
        a = args[i]
        name, eq, val = a.partition("=")
        if a in GH_PR_VALUES and i + 1 < len(args):
            opts[GH_PR_SHORT.get(a, a)], i = args[i + 1], i + 2  # 写了两次的，和 gh 一样用后面那个
        elif eq and name.startswith("--") and name in GH_PR_VALUES:
            opts[name], i = val, i + 1
        elif a in GH_PR_FLAGS:
            i += 1
        else:
            return f"守卫看不懂 gh pr create 的参数 `{a}`。" + PR_HELP
    head = opts.get("--head")
    base = opts.get("--base") or default_branch(cwd)
    if head is None:
        head = current_branch(cwd)
    if not head or head == "HEAD" or not _ref_ok(head):
        return "守卫看不出这个 PR 的 head 分支（要是本地的一个分支，不带「用户名:」）。" + PR_HELP
    tip = rev(cwd, "refs/heads/" + head)
    if not tip:
        return f"本地没有分支 `{head}`，守卫查不了这个 PR 里有什么，不能放行。"
    if not base or not _ref_ok(base):
        return "守卫看不出这个 PR 要合并到哪个分支，用 --base <分支> 写明。"
    base_ref = next((f"refs/remotes/{r}/{base}" for r in remotes(cwd) if rev(cwd, f"refs/remotes/{r}/{base}")),
                    None)
    if not base_ref:
        return f"本地没有远端分支 `{base}` 的记录（没有远端，或者还没 git fetch），守卫查不出这个 PR 带了哪些提交，不能放行。"
    return {"what": "开 PR", "name": head, "base": base_ref, "review_base": base_ref, "tip": tip, "target": None}


def clean_merge(cwd, sha):
    """合并提交的内容是不是两边自动合出来的（没夹带别的改动）。判断不了（章鱼合并、git 太旧）按不是处理。"""
    parents = (git(cwd, "rev-list", "--parents", "-n", "1", sha) or "").split()[1:]
    if len(parents) != 2:
        return False
    try:
        r = subprocess.run(["git", "merge-tree", "--write-tree", *parents], cwd=cwd,
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return False
    tree = git(cwd, "rev-parse", sha + "^{tree}")
    return r.returncode == 0 and bool(tree) and r.stdout.split()[:1] == [tree]


def uncovered(cwd, rng):
    """范围里还不能上传的提交。git 查不出来时返回 None（调用的地方要拦，不能当成通过）。

    一个审核单元 = 一次通过的审核 = {顶端提交, 这一段的全部提交}，只证明「整段一起上」没问题。
    所以单元的顶端提交也在这次上传里，它的提交才算数；只带了某个单元的一部分（比如只推「引入问题」
    的提交，不推后面「撤销」的提交），那部分不算。
    另外，两边自动合出来、没夹带改动的合并提交不用审。"""
    revs = git(cwd, "rev-list", rng, "--")
    if revs is None:
        return None
    todo = revs.split()
    uploading, ok = set(todo), set()
    for u in records.units(cwd):
        if u["tip"] in uploading:
            ok |= u["commits"]
    rest = [s for s in todo if s not in ok]
    if not rest:
        return []
    merges = git(cwd, "rev-list", "--merges", rng, "--")
    if merges is None:
        return None
    merges = set(merges.split())
    return [s for s in rest if not (s in merges and clean_merge(cwd, s))]


def review_msg(cwd, plan, missing):
    msg = (
        f"有 {len(missing)} 个提交还没通过 Codex 审核，不能{plan['what']}。先跑审核（放后台跑，只看结论）：\n"
        f"python3 {shlex.quote(REVIEW_PY)} --branch {shlex.quote(plan['name'])}"
        f" --base {shlex.quote(plan['review_base'])} --cwd {shlex.quote(project_root(cwd))}\n"
        "审核通过后再重试这条命令；不通过就把问题清单派回子代理修，修完重审。"
    )
    reviewed = set().union(*(u["commits"] for u in records.units(cwd)))
    if any(s in reviewed for s in missing):
        msg += ("\n注意：其中有提交审核过，但那次审核的最后一个提交不在这次上传里。"
                "审核看的是整段的最终结果，不能只上传其中一部分。")
    if records.has_legacy(cwd):
        msg += "\n旧格式的审核记录（passed 文件）已经作废，以前通过的提交也要重新审核。"
    return msg


def check_upload(cwd, cmd, kinds, semi, codex_off):
    """负责人的一条上传命令能不能放行。返回拦截原因或 None。"""
    if not kinds:
        return UNKNOWN
    if kinds & {"pr-merge", "api"}:
        if semi:
            return SEMI_PR
        if "pr-merge" in kinds:
            return ("守卫查不出 PR 里有哪些提交，负责人不用 gh pr merge。"
                    "全自动：在主分支上 git merge <分支>，再 git push。")
        return "守卫看不懂 gh api 的写操作会改远端的什么，不能放行。上传改用 git push，开 PR 用 gh pr create。"
    up = simple_upload(cmd)
    if not up:
        return STANDALONE
    kind, args, cdir = up
    if cdir:
        cwd = os.path.abspath(os.path.join(cwd, os.path.expanduser(cdir)))
    if not os.path.isdir(cwd) or git(cwd, "rev-parse", "--git-dir") is None:
        return f"{cwd} 不是 git 仓库，守卫查不了这条上传命令，不能放行。"
    plan = {"merge": merge_plan, "push": push_plan, "pr-create": pr_plan}[kind](cwd, args)
    if plan is None or isinstance(plan, str):
        return plan
    if semi and kind != "pr-create" and remotes(cwd):
        default = default_branch(cwd)
        if not default:
            return ("半自动模式：守卫查不出这个仓库的主分支是哪个，不能放行。"
                    "先运行 `git remote set-head origin -a`。")
        if plan["target"] == default:
            return f"半自动模式：不直接{'合并进' if kind == 'merge' else '推'} {default}，{SEMI_HOW}"
    if codex_off:
        return None
    missing = uncovered(cwd, f"{plan['base']}..{plan['tip']}")
    if missing is None:
        return CANT_CHECK.format(what=plan["what"])
    return review_msg(cwd, plan, missing) if missing else None


def check_ship(cwd, cmd, auto):
    """负责人的上传关和半自动限制。返回拦截原因或 None。"""
    kinds, unknown = scan_ship(cmd)
    if not kinds and not (unknown and SHIP_HINT.search(cmd)):
        return None  # 不是上传命令
    semi = auto == "semi"
    codex_off = os.environ.get("AGENT_DEV_TEAM_CODEX", "").lower() == "off"
    if codex_off and not semi:
        return None  # 审核关关了，又不是半自动：没什么要查的
    try:
        return check_upload(cwd, cmd, kinds, semi, codex_off)
    except Exception as e:  # 确实是上传命令时，守卫自己出错也拦
        return f"守卫检查这条上传命令时自己出错了（{e!r}），不能放行。把上传命令单独写成最简单的形式再试。"


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


NO_SHIP = {
    "subagent": "子代理不能 push / 合并 / 开 PR。提交到自己的分支，把结果交回负责人。"
                "要跟上主分支或解决合并冲突，用 git rebase。",
    "kuaixiu": "对接窗口不能 push / 合并 / 开 PR。提交到当前分支后交回负责人，由负责人验收、过 Codex 审核再合并。"
               "要跟上主分支，用 git rebase。",
}
NO_SHIP_HINT = ("\n（守卫是保守判断的：命令里只要可能有上传动作就拦，包括 `echo git push` 这种、"
                "变量当命令名、拆不开的命令里出现 push / merge。确实不是上传，就把命令写直白些再试。）")


# 秒。钩子自己的超时是 30 秒，超时会被当成放行，所以守卫要赶在它前面自己收尾。
# 环境变量 AGENT_DEV_TEAM_GUARD_TIMEOUT 只能把它调小（测试用）。
TIME_LIMIT = 20
TIMEOUT_HINT = re.compile(SHIP_HINT.pattern + r"|\bgh\b")  # 超时的时候没法细看，gh 的命令也算
WATCH = {}  # 正在检查的命令，超时的时候用


def on_timeout(signum, frame):
    """守卫跑得太久：正在查的命令可能是上传命令就拦，其余放行。"""
    if TIMEOUT_HINT.search(WATCH.get("cmd", "")):
        print(PREFIX + "守卫检查这条命令超时了，而命令里出现了 push / merge 这类词，不能放行。"
              "把上传命令单独写成最简单的形式再试。", file=sys.stderr)
        os._exit(2)
    os._exit(0)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(data, dict):
        sys.exit(0)
    if hasattr(signal, "SIGALRM"):
        try:
            limit = min(float(os.environ.get("AGENT_DEV_TEAM_GUARD_TIMEOUT") or TIME_LIMIT), TIME_LIMIT)
        except ValueError:
            limit = TIME_LIMIT
        signal.signal(signal.SIGALRM, on_timeout)
        signal.setitimer(signal.ITIMER_REAL, max(limit, 0.1))
    r = role(data)
    if r in ("none", "normal"):
        sys.exit(0)
    tool = data.get("tool_name", "")
    inp = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()

    if tool == "Bash":
        cmd = inp.get("command", "") or ""
        if not isinstance(cmd, str):
            sys.exit(0)
        WATCH["cmd"] = cmd
        if r in NO_SHIP:
            if may_ship(cmd):
                block(NO_SHIP[r] + NO_SHIP_HINT)
        else:
            try:
                reason = check_lead_bash(cmd)
            except Exception:
                reason = None  # 黑名单自己出错放行，但后面的上传关照样要查
            reason = reason or check_ship(cwd, cmd, auto_mode(data))
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
        sys.exit(0)  # 不是上传命令时，守卫自己出错一律放行（上传命令的出错在 scan_ship / check_ship 里已经按拦处理）
