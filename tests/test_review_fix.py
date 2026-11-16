#!/usr/bin/env python3
"""review.py 0.2.2 第 1 轮审核的返工：复用和报告绑定完整的审核条件、复审时「已修好」的确认不挡通过。
全部用临时 git 仓库和假的审核命令，不调真实的 Codex。搭法照 test_review_022。
运行：python3 -m unittest tests.test_review_fix
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_review_022 import Base, verdict  # noqa: E402

import records  # noqa: E402
import review  # noqa: E402


def report_of(out):
    return next(ln for ln in out.splitlines() if ln.startswith("完整报告："))[len("完整报告："):]


def incr_verdict(v, previous, *issues, summary="复审总评"):
    """复审的 JSON 结论：previous 是 [(上一轮的严重程度, 标题, 已修好 / 没修好)]。"""
    d = json.loads(verdict(v, *issues, summary=summary))
    d["previous"] = [{"title": t, "severity": s, "status": st, "detail": t + " 的确认说明"} for s, t, st in previous]
    return json.dumps(d, ensure_ascii=False)


class 复用绑定审核条件(Base):
    def test_增量通过后再用full_真的跑一次全量(self):
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        self.rv()
        self.feat_commit("v3\n")
        self.ctl(incr_verdict("PASS", [("严重", "坏了", "已修好")]))
        code, out = self.rv()
        self.assertEqual(code, 0, out)
        self.assertIn("这是复审", self.calls()[-1]["args"][-1])
        self.ctl(verdict("PASS"))
        code, out = self.rv("--full")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls()), 3, out)  # 没有复用增量的结论
        self.assertNotIn("复用", out.splitlines()[0])
        prompt = self.calls()[-1]["args"][-1]
        self.assertNotIn("这是复审", prompt)
        self.assertIn("不准找到几条就停", prompt)
        self.assertIn("model_reasoning_effort=max", self.calls()[-1]["args"])
        # 全量通过之后再跑 --full，复用的是这次全量的结论
        code, out = self.rv("--full")
        self.assertEqual(out.splitlines()[0], "审核：通过（复用之前的结论）", out)
        self.assertEqual(len(self.calls()), 3)

    def test_增量不通过后再用full_不复用增量的结论(self):
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        self.rv()
        self.feat_commit("v3\n")
        self.ctl(incr_verdict("FAIL", [("严重", "坏了", "没修好")]))
        self.rv()
        self.ctl(verdict("PASS"))
        code, out = self.rv("--full")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls()), 3)

    def test_增量不通过后没加提交再跑_复用这次增量的结论(self):
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        self.rv()
        self.feat_commit("v3\n")
        self.ctl(incr_verdict("FAIL", [("严重", "坏了", "没修好")]))
        self.rv()
        code, out = self.rv()
        self.assertEqual(code, 1, out)
        self.assertIn("复用之前的结论", out.splitlines()[0])
        self.assertIn("没修好", out)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(records.fails(self.repo, "feat"), 2)

    def test_不通过后没加提交_换了审核命令_重新审(self):
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        self.rv()
        self.env["AGENT_DEV_TEAM_CODEX_CMD"] += " -m other"
        self.rv()
        self.assertEqual(len(self.calls()), 2)

    def test_上一轮不通过后改了brief_改做全量(self):
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        self.rv("--brief", "旧的完成标准")
        self.feat_commit("v3\n")
        self.ctl(verdict("PASS"))
        code, out = self.rv("--brief", "新的完成标准：还要覆盖没改的代码")
        self.assertEqual(code, 0, out)
        prompt = self.calls()[-1]["args"][-1]
        self.assertNotIn("这是复审", prompt)
        self.assertIn("不准找到几条就停", prompt)
        self.assertIn("新的完成标准", prompt)
        self.assertIn("任务说明变了", out)
        self.assertIn("model_reasoning_effort=max", self.calls()[-1]["args"])

    def test_brief没变_照样增量复审(self):
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        self.rv("--brief", "同一份说明")
        self.feat_commit("v3\n")
        self.ctl(verdict("PASS"))
        self.rv("--brief", "同一份说明")
        self.assertIn("这是复审", self.calls()[-1]["args"][-1])

    def test_last记录存brief的哈希_不存原文(self):
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        self.rv("--brief", "保密的说明")
        with open(records._last_path(self.repo, "feat"), encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("保密的说明", raw)
        self.assertIn("brief", json.loads(raw))

    def test_旧版last记录没有brief哈希_改做全量(self):
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        self.rv()
        p = records._last_path(self.repo, "feat")
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d.pop("brief", None)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f)
        self.feat_commit("v3\n")
        self.ctl(verdict("PASS"))
        code, out = self.rv()
        self.assertNotIn("这是复审", self.calls()[-1]["args"][-1])
        self.assertIn("任务说明", out)


class 报告不重复(Base):
    def test_失败计数清零后_新报告不覆盖旧报告(self):
        self.ctl(verdict("FAIL", ("严重", "第一次的问题")))
        _, out1 = self.rv()
        self.feat_commit("v3\n")
        self.ctl(verdict("PASS"))
        _, out2 = self.rv()
        self.assertEqual(records.fails(self.repo, "feat"), 0)
        self.ctl(verdict("FAIL", ("严重", "第三次的问题")))
        _, out3 = self.rv("--brief", "新说明")
        paths = [report_of(o) for o in (out1, out2, out3)]
        self.assertEqual(len(set(paths)), 3, paths)
        with open(paths[0], encoding="utf-8") as f:
            self.assertIn("第一次的问题", f.read())
        with open(paths[2], encoding="utf-8") as f:
            self.assertIn("第三次的问题", f.read())
        # 第一次的缓存还指向第一次的报告
        _, again = self.rv("--brief", "新说明")
        self.assertIn("复用之前的结论", again.splitlines()[0])
        self.assertEqual(report_of(again), paths[2])

    def test_不复用的命令连跑两次_报告也不撞(self):
        fake = os.path.join(self.tmp, "plain.py")
        with open(fake, "w") as f:
            f.write("print('严重：x')\nprint('VERDICT: FAIL')\n")
        env = dict(self.env, AGENT_DEV_TEAM_CODEX_CMD=f"{sys.executable} {fake}")
        records.add_fail(self.repo, "feat")
        _, out1 = self.rv(env=env)
        records.clear_fails(self.repo, "feat")
        records.add_fail(self.repo, "feat")
        _, out2 = self.rv(env=env)  # 同一秒、同一轮次、同一组条件
        self.assertNotEqual(report_of(out1), report_of(out2))
        self.assertTrue(os.path.exists(report_of(out1)))

    def test_缓存里存的是这次的报告路径(self):
        self.ctl(verdict("FAIL", ("严重", "坏了")))
        _, out = self.rv()
        d = records.state_dir(self.repo)
        caches = [os.path.join(d, "cache", n) for n in os.listdir(os.path.join(d, "cache"))]
        self.assertEqual(len(caches), 1)
        with open(caches[0], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["report"], report_of(out))


class 复审确认已修好(Base):
    def first_fail(self):
        self.ctl(verdict("FAIL", ("严重", "坏了"), ("一般", "小毛病")))
        self.rv()
        self.feat_commit("v3\n")

    def test_复审的结构把确认单独放一个字段(self):
        self.first_fail()
        self.ctl(incr_verdict("PASS", [("严重", "坏了", "已修好")]))
        self.rv()
        call = self.calls()[-1]
        schema = call["schema"]
        self.assertIn("previous", schema["required"])
        item = schema["properties"]["previous"]["items"]
        self.assertEqual(item["properties"]["status"]["enum"], ["已修好", "没修好"])
        self.assertEqual(set(item["required"]), set(item["properties"]))
        prompt = call["args"][-1]
        self.assertIn("previous", prompt)
        self.assertIn("issues 只写还没解决的问题", prompt)
        # 第一轮（全量）不带这个字段，结构不变
        self.assertNotIn("previous", self.calls()[0]["schema"]["properties"])

    def test_已修好的严重问题写在确认里_PASS就算通过(self):
        self.first_fail()
        self.ctl(incr_verdict("PASS", [("严重", "坏了", "已修好"), ("一般", "小毛病", "已修好")]))
        code, out = self.rv()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "审核：通过")
        with open(report_of(out), encoding="utf-8") as f:
            full = f.read()
        self.assertIn("上一轮问题确认", full)
        self.assertIn("已修好", full)

    def test_上一轮的严重问题没修好_PASS也改成不通过(self):
        self.first_fail()
        self.ctl(incr_verdict("PASS", [("严重", "坏了", "没修好")]))
        code, out = self.rv()
        self.assertEqual(code, 1, out)
        self.assertIn("坏了", out)
        self.assertIn("没修好", out)

    def test_上一轮的一般问题没修好_不挡通过(self):
        self.first_fail()
        self.ctl(incr_verdict("PASS", [("严重", "坏了", "已修好"), ("一般", "小毛病", "没修好")]))
        code, out = self.rv()
        self.assertEqual(code, 0, out)
        with open(report_of(out), encoding="utf-8") as f:
            self.assertIn("【没修好】小毛病（上一轮：一般）", f.read())  # 没修好的照样记下来

    def test_issues里的严重问题照样挡(self):
        self.first_fail()
        self.ctl(incr_verdict("PASS", [("严重", "坏了", "已修好")], ("严重", "新改出来的问题")))
        code, out = self.rv()
        self.assertEqual(code, 1, out)
        self.assertIn("新改出来的问题", out)

    def test_复审提示词写清约定_文字结论也一样(self):
        log = os.path.join(self.tmp, "prompt.txt")
        fake = os.path.join(self.tmp, "plain.py")
        with open(fake, "w") as f:
            f.write(f"import sys\nopen({log!r}, 'w').write(sys.argv[-1])\nprint('VERDICT: FAIL')\n")
        env = dict(self.env, AGENT_DEV_TEAM_CODEX_CMD=f"{sys.executable} {fake}")
        self.rv(env=env)
        self.feat_commit("v3\n")
        self.rv(env=env)
        with open(log, encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn("这是复审", prompt)
        self.assertIn("「上一轮问题确认」", prompt)
        self.assertIn("已修好的问题不要再列进严重 / 一般 / 建议", prompt)
        self.assertNotIn("previous", prompt)  # 文字结论不提 JSON 字段

    def test_文字报告很长_保留上一轮问题确认这一节(self):
        text = (["上一轮问题确认：", "1. 严重：坏了 —— 没修好", "   还是会崩", "2. 一般：小毛病 —— 已修好", "",
                 "严重：", "1. 新的严重问题 src/a.py:1", "", "一般："]
                + [f"- 一般问题{i:02d} src/a.py:{i}" for i in range(80)] + ["", "VERDICT: FAIL"])
        shown = review.pick_text("\n".join(text))
        for words in ("上一轮问题确认", "坏了 —— 没修好", "还是会崩", "新的严重问题", "VERDICT: FAIL", "另有 70 条"):
            self.assertIn(words, shown)
        self.assertEqual(sum(f"一般问题{i:02d} " in shown for i in range(80)), 10)


if __name__ == "__main__":
    unittest.main()
