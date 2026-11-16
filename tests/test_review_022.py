#!/usr/bin/env python3
"""review.py 0.2.2 的改进：读对代码、一次扫全、复审收口、档位、任务说明、结果不丢、复用、记录名、用量、设计关。
全部用临时 git 仓库和假的审核命令，不调真实的 Codex。
运行：python3 -m unittest tests.test_review_022
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks", "scripts"))
sys.path.insert(0, SCRIPTS)
import records  # noqa: E402
import review  # noqa: E402

# 假的 codex：名字就叫 codex，review.py 会当成 codex exec，加 --json / -o / --output-schema。
# 行为由同目录的 ctl.json 决定，每次调用记到 calls.jsonl。
FAKE_CODEX = r'''#!{python}
import json, os, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
schema = None
if "--output-schema" in args:
    with open(args[args.index("--output-schema") + 1], encoding="utf-8") as f:
        schema = json.load(f)
head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
with open(os.path.join(here, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({{"cwd": os.getcwd(), "args": args, "head": head, "schema": schema}}, ensure_ascii=False) + "\n")
with open(os.path.join(here, "ctl.json"), encoding="utf-8") as f:
    ctl = json.load(f)
if "-o" in args and ctl.get("write_o", True):
    with open(args[args.index("-o") + 1], "w", encoding="utf-8") as f:
        f.write(ctl["message"])
if "--json" in args:
    print(json.dumps({{"type": "thread.started", "thread_id": "x"}}))
    print(json.dumps({{"type": "item.completed", "item": {{"id": "i", "type": "agent_message", "text": ctl["message"]}}}}))
    if ctl.get("usage"):
        print(json.dumps({{"type": "turn.completed", "usage": ctl["usage"]}}))
else:
    print(ctl["message"])
'''


def sh(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr}")
    return r.stdout.strip()


def verdict(v, *issues, summary="总评"):
    return json.dumps({"verdict": v, "summary": summary, "issues": [
        {"severity": s, "title": t, "file": "src/a.py", "line": n, "detail": t + " 的说明", "root_cause": t + " 的根因"}
        for n, (s, t) in enumerate(issues, 1)]}, ensure_ascii=False)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="adt-r022-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", self.tmp])
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("AGENT_DEV_TEAM") and not k.startswith("GIT_")}
        self.env["AGENT_DEV_TEAM_HOME"] = os.path.join(self.tmp, "home")
        sh(self.repo, "init", "-q", "-b", "main")
        sh(self.repo, "config", "user.name", "t")
        sh(self.repo, "config", "user.email", "t@t")
        sh(self.repo, "config", "commit.gpgsign", "false")
        self.write("src/a.py", "v1\n")
        self.commit("init")
        sh(self.repo, "checkout", "-q", "-b", "feat")
        self.write("src/a.py", "v2\n")
        self.commit("feat 1")
        sh(self.repo, "checkout", "-q", "main")
        # 假 codex
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.codex = os.path.join(self.bin, "codex")
        with open(self.codex, "w") as f:
            f.write(FAKE_CODEX.format(python=sys.executable))
        os.chmod(self.codex, 0o755)
        self.env["AGENT_DEV_TEAM_CODEX_CMD"] = f"{self.codex} exec --sandbox read-only"
        self.ctl(verdict("PASS"))

    def write(self, rel, text, cwd=None):
        p = os.path.join(cwd or self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def commit(self, msg, cwd=None):
        sh(cwd or self.repo, "add", "-A")
        sh(cwd or self.repo, "commit", "-q", "-m", msg)
        return sh(cwd or self.repo, "rev-parse", "HEAD")

    def feat_commit(self, text):
        sh(self.repo, "checkout", "-q", "feat")
        self.write("src/a.py", text)
        sha = self.commit("修")
        sh(self.repo, "checkout", "-q", "main")
        return sha

    def ctl(self, message, **kw):
        with open(os.path.join(self.bin, "ctl.json"), "w", encoding="utf-8") as f:
            json.dump(dict(kw, message=message), f, ensure_ascii=False)

    def calls(self):
        p = os.path.join(self.bin, "calls.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f]

    def rv(self, *args, branch="feat", cwd=None, env=None):
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "review.py"), "--branch", branch,
                            "--cwd", cwd or self.repo, *args], cwd=self.repo, capture_output=True, text=True,
                           env=env or self.env, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def state(self):
        return records.state_dir(self.repo)


class 读对代码(Base):
    def test_分支检出在任务工作副本里_在那里跑(self):
        wt = os.path.join(self.tmp, "wt")
        sh(self.repo, "worktree", "add", "-q", wt, "feat")
        code, out = self.rv()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "审核：通过")
        self.assertIn(f"--cwd {wt}", out)
        call = self.calls()[-1]
        self.assertEqual(os.path.realpath(call["cwd"]), wt)
        self.assertEqual(call["head"], sh(self.repo, "rev-parse", "feat"))

    def test_哪里都没检出_在临时工作副本里跑_跑完删掉(self):
        code, out = self.rv()
        self.assertEqual(code, 0, out)
        call = self.calls()[-1]
        self.assertEqual(call["head"], sh(self.repo, "rev-parse", "feat"))  # 读到的是分支顶端，不是 main
        self.assertNotEqual(os.path.realpath(call["cwd"]), self.repo)
        self.assertFalse(os.path.exists(call["cwd"]))
        self.assertEqual(len(sh(self.repo, "worktree", "list").splitlines()), 1)

    def test_cwd正好在顶端_就在那里跑(self):
        wt = os.path.join(self.tmp, "wt")
        sh(self.repo, "worktree", "add", "-q", wt, "feat")
        code, out = self.rv(cwd=wt)
        self.assertEqual(code, 0, out)
        self.assertEqual(os.path.realpath(self.calls()[-1]["cwd"]), wt)
        self.assertNotIn("不在", out)

    def test_有没提交的改动_出错_提示正确的cwd(self):
        wt = os.path.join(self.tmp, "wt")
        sh(self.repo, "worktree", "add", "-q", wt, "feat")
        self.write("src/a.py", "还没提交\n", cwd=wt)
        for cwd in (wt, self.repo):  # 在工作副本里跑、在主目录跑，都出错
            code, out = self.rv(cwd=cwd)
            self.assertEqual(code, 2, out)
            self.assertTrue(out.startswith("审核：出错（不计入失败轮数）"), out)
            self.assertIn("没提交的改动", out)
            self.assertIn(f"--cwd {wt}", out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(records.fails(self.repo, "feat"), 0)


class 提示词和档位(Base):
    def test_第一轮要求扫全_档位max_默认命令不含档位(self):
        self.assertNotIn("model_reasoning_effort", review.BASE_CMD)
        self.assertIn("-c service_tier=priority", review.BASE_CMD)
        self.rv()
        args = self.calls()[-1]["args"]
        prompt = args[-1]
        for words in ("不准找到几条就停", "同一类问题合成一条", "根本原因", "判定方法", "已知的例子"):
            self.assertIn(words, prompt)
        i = args.index("model_reasoning_effort=max")
        self.assertEqual(args[i - 1], "-c")
        self.assertEqual(args[:3], ["exec", "--sandbox", "read-only"])  # 配的命令在前，档位追加在后
        self.assertNotIn("model_reasoning_effort=high", args)

    def test_full也用max(self):
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        self.rv()
        self.feat_commit("v3\n")
        self.rv("--full")
        self.assertIn("model_reasoning_effort=max", self.calls()[-1]["args"])
        self.assertIn("不准找到几条就停", self.calls()[-1]["args"][-1])

    def test_增量复审_收口规则_档位high(self):
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        self.rv()
        self.feat_commit("v3\n")
        self.ctl(verdict("PASS"))
        code, out = self.rv()
        self.assertEqual(code, 0, out)
        args = self.calls()[-1]["args"]
        prompt = args[-1]
        self.assertIn("这是复审", prompt)
        self.assertIn("复审只因为两种问题不通过：上一轮的问题没修好；这次新改的代码", prompt)
        self.assertIn("上一轮已经审过、这次没动的代码里新挑出来的问题，一律算「一般」", prompt)
        self.assertIn("坏了", prompt)  # 上一轮的问题清单
        self.assertNotIn("不准找到几条就停", prompt)
        self.assertIn("model_reasoning_effort=high", args)
        self.assertNotIn("model_reasoning_effort=max", args)

    def test_自己配的命令也追加档位(self):
        log = os.path.join(self.tmp, "args.json")
        fake = os.path.join(self.tmp, "plain.py")
        with open(fake, "w") as f:
            f.write(f"import json, sys\njson.dump(sys.argv[1:], open({log!r}, 'w'))\nprint('VERDICT: PASS')\n")
        cmd = f"{sys.executable} {fake} -c model_reasoning_effort=low"
        self.write(".claude/agent-dev-team.json", json.dumps({"codex_cmd": cmd}))
        code, out = self.rv()
        self.assertEqual(code, 0, out)
        with open(log) as f:
            args = json.load(f)
        self.assertEqual(args[:-1], ["-c", "model_reasoning_effort=low"])  # 不是 codex exec：原样调用，只多一个提示词
        self.assertNotIn("--json", args)  # 不是 codex exec，不加 JSON 参数

    def test_brief原样放进提示词(self):
        brief = "目标：审核脚本读对代码。\n取舍：不防刻意绕过。"
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        self.rv("--brief", brief)
        self.assertIn(brief, self.calls()[-1]["args"][-1])
        self.feat_commit("v3\n")
        self.rv("--brief", brief)
        self.assertIn("这是复审", self.calls()[-1]["args"][-1])
        self.assertIn(brief, self.calls()[-1]["args"][-1])


class 结果不丢(Base):
    def test_JSON结构_严重全列_一般最多10条_完整报告存下(self):
        issues = [("严重", f"严重问题{i}") for i in range(3)] + [("一般", f"一般问题{i:02d}") for i in range(15)]
        self.ctl(verdict("FAIL", *issues))
        code, out = self.rv()
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "审核：不通过（第 1 轮）")
        call = self.calls()[-1]
        self.assertIn("--json", call["args"])
        self.assertIn("-o", call["args"])
        item = call["schema"]["properties"]["issues"]["items"]
        self.assertEqual(set(item["required"]), {"severity", "title", "file", "line", "detail", "root_cause"})
        for i in range(3):
            self.assertIn(f"严重问题{i}", out)
            self.assertIn(f"严重问题{i} 的说明", out)
            self.assertIn(f"严重问题{i} 的根因", out)
        self.assertEqual(sum(f"一般问题{i:02d}（" in out for i in range(15)), 10)
        self.assertIn("另有 5 条", out)
        line = next(ln for ln in out.splitlines() if ln.startswith("完整报告："))
        path = line[len("完整报告："):]
        self.assertTrue(path.startswith(self.state()))
        with open(path, encoding="utf-8") as f:
            full = f.read()
        for i in range(15):
            self.assertIn(f"一般问题{i:02d}", full)
        # 下一轮复审拿到的是完整清单
        self.assertIn("一般问题14", records.load_last(self.repo, "feat")["issues"])

    def test_报告按分支和轮次命名(self):
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        _, out1 = self.rv()
        self.feat_commit("v3\n")
        _, out2 = self.rv()
        p1, p2 = [next(ln for ln in o.splitlines() if ln.startswith("完整报告："))[5:] for o in (out1, out2)]
        self.assertNotEqual(p1, p2)
        self.assertIn(records.branch_key("feat"), os.path.basename(p1))
        self.assertTrue(p1.endswith("-r1.md") and p2.endswith("-r2.md"), (p1, p2))

    def test_严重问题标成通过_也算不通过(self):
        self.ctl(verdict("PASS", ("严重", "其实坏了")))
        code, out = self.rv()
        self.assertEqual(code, 1, out)
        self.assertIn("其实坏了", out)

    def test_文字报告很长_开头的严重小节完整保留(self):
        fake = os.path.join(self.tmp, "long.py")
        text = (["## 严重", "1. 开头的严重问题 src/a.py:1", "   详细说明第一行", "   详细说明第二行", "", "## 一般"]
                + [f"- 一般问题{i:02d} src/a.py:{i}" for i in range(80)] + ["", "VERDICT: FAIL"])
        with open(fake, "w", encoding="utf-8") as f:
            f.write("".join(f"print({ln!r})\n" for ln in text))  # 一行一个 print：老版本 Python 读很长的中文行会出错
        env = dict(self.env, AGENT_DEV_TEAM_CODEX_CMD=f"{sys.executable} {fake}")
        code, out = self.rv(env=env)
        self.assertEqual(code, 1, out)
        for words in ("开头的严重问题", "详细说明第一行", "详细说明第二行", "VERDICT: FAIL", "另有 70 条"):
            self.assertIn(words, out)
        self.assertEqual(sum(f"一般问题{i:02d} " in out for i in range(80)), 10)
        path = next(ln for ln in out.splitlines() if ln.startswith("完整报告："))[5:]
        with open(path, encoding="utf-8") as f:
            self.assertIn("一般问题79", f.read())

    def test_没写o文件_从事件流里拿最后的回复(self):
        self.ctl(verdict("FAIL", ("严重", "事件流里的问题")), write_o=False)
        code, out = self.rv()
        self.assertEqual(code, 1, out)
        self.assertIn("事件流里的问题", out)


class 复用(Base):
    def test_通过过_不再调Codex(self):
        code, out = self.rv()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls()), 1)
        code, out = self.rv()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "审核：通过（复用之前的结论）")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(len(records.units(self.repo)), 1)

    def test_不通过过_输出上次的问题_不加轮数(self):
        self.ctl(verdict("FAIL", ("严重", "上次的问题")))
        self.rv()
        code, out = self.rv()
        self.assertEqual(code, 1, out)
        self.assertTrue(out.splitlines()[0].startswith("审核：不通过（第 1 轮"), out)
        self.assertIn("复用之前的结论", out.splitlines()[0])
        self.assertIn("上次的问题", out)
        self.assertIn("完整报告：", out)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(records.fails(self.repo, "feat"), 1)

    def test_改了代码_任务说明_审核命令_都重新审(self):
        self.rv()
        self.rv("--brief", "新的说明")
        self.assertEqual(len(self.calls()), 2)
        self.env["AGENT_DEV_TEAM_CODEX_CMD"] += " -m other"
        self.rv("--brief", "新的说明")
        self.assertEqual(len(self.calls()), 3)
        self.feat_commit("v3\n")
        self.rv("--brief", "新的说明")
        self.assertEqual(len(self.calls()), 4)

    def test_没有结论不记_下次照样调(self):
        self.ctl("我看完了")
        code, out = self.rv()
        self.assertEqual(code, 1, out)
        self.assertIn("没有给出结论", out)
        self.rv()
        self.assertEqual(len(self.calls()), 2)


class 记录名(unittest.TestCase):
    def test_斜杠和下划线的分支不撞(self):
        with tempfile.TemporaryDirectory() as d:
            sh(d, "init", "-q")
            records.add_fail(d, "feat/a")
            records.add_fail(d, "feat/a")
            records.add_fail(d, "feat_a")
            self.assertEqual(records.fails(d, "feat/a"), 2)
            self.assertEqual(records.fails(d, "feat_a"), 1)
            sha = "a" * 40
            records.save_last(d, "feat/a", sha, sha, "斜杠的")
            records.save_last(d, "feat_a", sha, sha, "下划线的")
            self.assertEqual(records.load_last(d, "feat/a")["issues"], "斜杠的")
            self.assertEqual(records.load_last(d, "feat_a")["issues"], "下划线的")
            self.assertNotEqual(records.branch_key("feat/a"), records.branch_key("feat_a"))
            self.assertTrue(records.branch_key("feat/a").startswith("feat_a-"))  # 保留可读前缀


class 用量(Base):
    def test_拿到用量_打印并记到usage(self):
        u = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200, "reasoning_output_tokens": 150}
        self.ctl(verdict("PASS"), usage=u)
        code, out = self.rv()
        self.assertEqual(code, 0, out)
        line = next(ln for ln in out.splitlines() if ln.startswith("Codex 用量："))
        self.assertTrue(line.startswith("Codex 用量：约 1200 token（"), line)
        for words in ("输入 1000", "其中缓存 400", "输出 200", "其中推理 150"):
            self.assertIn(words, line)
        with open(os.path.join(self.state(), "usage.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["branch"], row["round"], row["tip"], row["effort"]),
                         ("feat", 1, sh(self.repo, "rev-parse", "feat"), "max"))
        self.assertEqual(row["output_tokens"], 200)
        self.assertIn("at", row)

    def test_拿不到用量_不打印不报错(self):
        code, out = self.rv()
        self.assertEqual(code, 0, out)
        self.assertNotIn("Codex 用量", out)
        self.assertFalse(os.path.exists(os.path.join(self.state(), "usage.jsonl")))


class 设计关(Base):
    def plan(self, *args):
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "review.py"), "--plan", "用正则解析 shell 命令判断是不是 push",
                            "--cwd", self.repo, *args], cwd=self.repo, capture_output=True, text=True,
                           env=self.env, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_可以做(self):
        self.ctl("能做成。\nDESIGN: OK\n")
        code, out = self.plan()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "设计：可以做")
        args = self.calls()[-1]["args"]
        self.assertIn("model_reasoning_effort=medium", args)
        self.assertIn("--sandbox", args)
        self.assertNotIn("--output-schema", args)
        prompt = args[-1]
        self.assertIn("用正则解析 shell 命令判断是不是 push", prompt)
        self.assertIn("猜", prompt)
        self.assertIn("只读", prompt)

    def test_建议换做法_不写审核记录(self):
        self.ctl("靠猜，换做法。\nDESIGN: CHANGE\n", usage={"input_tokens": 10, "output_tokens": 5})
        code, out = self.plan()
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[0], "设计：建议换做法")
        self.assertIn("Codex 用量：约 15 token", out)
        self.assertIn("靠猜", out)
        self.assertEqual(os.listdir(self.state()), [])  # 一条记录都没写

    def test_没有结论算出错(self):
        self.ctl("说不清\n")
        code, out = self.plan()
        self.assertEqual(code, 2, out)
        self.assertTrue(out.startswith("设计：出错"), out)

    def test_review不带branch也不带plan_出错(self):
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "review.py"), "--cwd", self.repo],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stdout.startswith("审核：出错"), r.stdout)


if __name__ == "__main__":
    unittest.main()
